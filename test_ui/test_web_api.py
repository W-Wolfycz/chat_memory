from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


if "astrbot.api" not in sys.modules:
    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        info = debug
        warning = debug
        error = debug
        exception = debug

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot.api"] = api


def load_module(*, body=None, args=None, headers=None):
    quart = types.ModuleType("quart")

    class Request:
        def __init__(self):
            self.args = args or {}
            self.headers = headers or {}

        async def get_json(self):
            return body

    quart.request = Request()
    sys.modules["quart"] = quart
    sys.modules.pop("chat_memory.ui.web_api", None)
    return importlib.import_module("chat_memory.ui.web_api")


class Repository:
    default_page_size = 50
    max_page_size = 200
    timezone = "Asia/Shanghai"

    def __init__(self, path="C:/AstrBot/chat_memory.db", fallback=True):
        self.db_path = Path(path)
        self.allow_immutable_fallback = fallback

    async def health(self):
        return {"healthy": True}

    async def overview(self):
        return {"summary": {"total_records": 2}}

    async def facets(self, filters):
        return {"filters": filters}

    async def query(self, payload):
        return {"payload": payload}

    async def tool_records(self, payload):
        return {"tool_payload": payload}

    async def record(self, record_id):
        return {"record": {"record_id": int(record_id)}}


class Context:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, description):
        self.routes.append((path, handler, methods, description))


class WebApiTestCase(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("chat_memory.ui.web_api", None)

    def test_registers_only_query_and_settings_routes(self):
        module = load_module()
        context = Context()
        controller = module.ChatMemoryUiWebApi(context, Repository())
        controller.register()
        self.assertEqual(
            {route[0] for route in context.routes},
            {
                "/chat_memory/about",
                "/chat_memory/settings",
                "/chat_memory/health",
                "/chat_memory/overview",
                "/chat_memory/facets",
                "/chat_memory/query",
                "/chat_memory/record",
                "/chat_memory/tools",
                "/chat_memory/media/<media_id>",
                "/chat_memory/export",
                "/chat_memory/export/<file_name>",
            },
        )
        self.assertTrue(all(set(route[2]) <= {"GET", "POST"} for route in context.routes))
        self.assertFalse(
            any("delete" in route[0] or "update" in route[0] for route in context.routes)
        )

    def test_query_tools_and_facets_contract(self):
        # 小节：非对象 JSON → 400
        module = load_module(body=[])
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        response = asyncio.run(controller.query_records())
        self.assertEqual(response[1], 400)
        self.assertFalse(response[0]["success"])

        response = asyncio.run(controller.query_tools())
        self.assertEqual(response[1], 400)
        self.assertFalse(response[0]["success"])

        # 小节：tools 查询转发参数
        module = load_module(body={"tool_name": "draw", "page": 2})
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        response = asyncio.run(controller.query_tools())
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["tool_payload"]["tool_name"], "draw")
        self.assertEqual(response["data"]["tool_payload"]["page"], 2)

        # 小节：facets 与 record 转发 query 参数
        module = load_module(
            args={
                "umo": "bot:FriendMessage:u1",
                "conversation_id": "cid-a",
                "role": "assistant",
                "llm_status": "llm_success",
                "content_kind": "text",
                "id": "9",
            },
        )
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        facets = asyncio.run(controller.facets())
        self.assertEqual(facets["data"]["filters"]["conversation_id"], "cid-a")
        self.assertEqual(facets["data"]["filters"]["role"], "assistant")
        self.assertEqual(facets["data"]["filters"]["llm_status"], "llm_success")
        self.assertEqual(facets["data"]["filters"]["content_kind"], "text")
        record = asyncio.run(controller.record_detail())
        self.assertEqual(record["data"]["record"]["record_id"], 9)

    def test_settings_read_update_and_validate(self):
        # 小节：读取默认路径 + 更新设置
        module = load_module(
            body={"database_path": "D:/Memory/new.db", "immutable_fallback": False}
        )
        updates = []

        async def updater(path, fallback):
            updates.append((path, fallback))
            repository = Repository(path, fallback)
            return repository, {"database": {"path": path, "mode": "wal_aware"}}

        controller = module.ChatMemoryUiWebApi(
            Context(), Repository(),
            default_database_path="C:/AstrBot/chat_memory.db",
            repository_updater=updater,
        )
        before = asyncio.run(controller.settings())
        self.assertTrue(before["data"]["using_default_path"])

        updated = asyncio.run(controller.update_settings())
        self.assertEqual(updates, [("D:/Memory/new.db", False)])
        self.assertEqual(
            updated["data"]["database_path"],
            str(Path("D:/Memory/new.db").resolve(strict=False)),
        )
        self.assertFalse(updated["data"]["immutable_fallback"])
        self.assertFalse(updated["data"]["using_default_path"])

        # 小节：非布尔 fallback 拒绝
        module = load_module(
            body={"database_path": "D:/Memory/new.db", "immutable_fallback": "false"}
        )

        async def rejecting_updater(path, fallback):
            return Repository(path, fallback), {}

        controller = module.ChatMemoryUiWebApi(
            Context(), Repository(), repository_updater=rejecting_updater
        )
        response = asyncio.run(controller.update_settings())
        self.assertEqual(response[1], 400)

    def test_media_endpoint(self):
        import base64 as b64
        import tempfile

        # 小节：base64 / thumb / raw 三种返回
        media_id = "ab" * 16
        payload = b"\x89PNG\r\nfake"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "202608").mkdir()
            (root / "202608" / f"{media_id}.png").write_bytes(payload)

            class MediaRepository(Repository):
                async def media_file(self, media_id):
                    return {
                        "media_id": media_id,
                        "kind": "image",
                        "file_name": f"{media_id}.png",
                        "ext": "png",
                        "mime_type": "image/png",
                        "size_bytes": len(payload),
                        "created_at": "2026-08-11 02:00:00",
                    }

            module = load_module(args={"as": "base64"})
            controller = module.ChatMemoryUiWebApi(
                Context(), MediaRepository(), media_root=root
            )
            response = asyncio.run(controller.media(media_id))
            self.assertTrue(response["success"])
            self.assertEqual(response["data"]["mime"], "image/png")
            self.assertEqual(b64.b64decode(response["data"]["data"]), payload)

            # as=thumb：无 PIL 时回退原图（成功返回，不报错）
            module_thumb = load_module(args={"as": "thumb"})
            controller_thumb = module_thumb.ChatMemoryUiWebApi(
                Context(), MediaRepository(), media_root=root
            )
            thumb = asyncio.run(controller_thumb.media(media_id))
            self.assertTrue(thumb["success"])
            self.assertEqual(thumb["data"]["kind"], "image")
            self.assertTrue(len(thumb["data"]["data"]) > 0)

            module2 = load_module(args={})
            controller2 = module2.ChatMemoryUiWebApi(
                Context(), MediaRepository(), media_root=root
            )
            response2 = asyncio.run(controller2.media(media_id))
            self.assertEqual(response2[1], 200)
            self.assertEqual(response2[0], payload)
            self.assertEqual(response2[2]["Content-Type"], "image/png")

        # 小节：video/voice 的 Range 分段请求（206）
        video_id = "cd" * 16
        video_payload = bytes(range(100))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "202608").mkdir()
            (root / "202608" / f"{video_id}.mp4").write_bytes(video_payload)

            class VideoRepository(Repository):
                async def media_file(self, media_id):
                    return {
                        "media_id": media_id,
                        "kind": "video",
                        "file_name": f"{media_id}.mp4",
                        "ext": "mp4",
                        "mime_type": "video/mp4",
                        "size_bytes": len(video_payload),
                        "created_at": "2026-08-11 02:00:00",
                    }

            module = load_module(headers={"Range": "bytes=10-29"})
            controller = module.ChatMemoryUiWebApi(
                Context(), VideoRepository(), media_root=root
            )
            # 大文件卡死根因回归：Range 分支必须只读请求区间，不能整读文件
            with patch.object(Path, "read_bytes",
                              side_effect=AssertionError("Range 不得整读文件")):
                response = asyncio.run(controller.media(video_id))
            self.assertEqual(response[1], 206)
            self.assertEqual(response[0], video_payload[10:30])
            self.assertEqual(response[2]["Content-Range"], "bytes 10-29/100")
            self.assertEqual(response[2]["Accept-Ranges"], "bytes")

            # 后缀范围 bytes=-N：取末尾 N 字节
            module_suffix = load_module(headers={"Range": "bytes=-20"})
            controller_suffix = module_suffix.ChatMemoryUiWebApi(
                Context(), VideoRepository(), media_root=root
            )
            suffix = asyncio.run(controller_suffix.media(video_id))
            self.assertEqual(suffix[1], 206)
            self.assertEqual(suffix[0], video_payload[-20:])
            self.assertEqual(suffix[2]["Content-Range"], "bytes 80-99/100")

            module_bad = load_module(headers={"Range": "bytes=999-"})
            controller_bad = module_bad.ChatMemoryUiWebApi(
                Context(), VideoRepository(), media_root=root
            )
            bad = asyncio.run(controller_bad.media(video_id))
            self.assertEqual(bad[1], 416)
            self.assertFalse(bad[0]["success"])
            self.assertEqual(bad[2]["Content-Range"], "bytes */100")

        # 小节：as=file_token（免鉴权单次令牌，不受 base64 16MB 上限约束）
        video_id = "cd" * 16
        video_payload = bytes(range(100))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "202608").mkdir()
            (root / "202608" / f"{video_id}.mp4").write_bytes(video_payload)

            class VideoRepository(Repository):
                async def media_file(self, media_id):
                    return {
                        "media_id": media_id,
                        "kind": "video",
                        "file_name": f"{media_id}.mp4",
                        "ext": "mp4",
                        "mime_type": "video/mp4",
                        "size_bytes": len(video_payload),
                        "created_at": "2026-08-11 02:00:00",
                    }

            # 无 astrbot.core（老版本）：501 降级
            sys.modules.pop("astrbot.core", None)
            module_none = load_module(args={"as": "file_token"})
            controller_none = module_none.ChatMemoryUiWebApi(
                Context(), VideoRepository(), media_root=root
            )
            response = asyncio.run(controller_none.media(video_id))
            self.assertEqual(response[1], 501)
            self.assertFalse(response[0]["success"])

            # 正常路径：注册令牌成功，返回免鉴权 file_url，且绝不整读文件
            core = types.ModuleType("astrbot.core")

            class FileTokenService:
                def __init__(self):
                    self.registered = []

                async def register_file(self, path, timeout=None):
                    self.registered.append((path, timeout))
                    return "tok-1234"

            core.file_token_service = FileTokenService()
            sys.modules["astrbot.core"] = core
            try:
                module = load_module(args={"as": "file_token"})
                controller = module.ChatMemoryUiWebApi(
                    Context(), VideoRepository(), media_root=root
                )
                # 大视频卡死根因回归：file_token 分支只嗅探编码，不能全量 read_bytes
                with patch.object(Path, "read_bytes",
                                  side_effect=AssertionError("file_token 不得整读文件")):
                    response = asyncio.run(controller.media(video_id))
                self.assertTrue(response["success"])
                self.assertEqual(response["data"]["file_url"], "/api/file/tok-1234")
                self.assertEqual(response["data"]["kind"], "video")
                self.assertEqual(response["data"]["size"], len(video_payload))
                self.assertEqual(response["data"]["codec"], "")
                self.assertEqual(len(core.file_token_service.registered), 1)
            finally:
                sys.modules.pop("astrbot.core", None)

            # 小节：_sniff_video_codec（只读前 512KB；moov 在尾部时返回空串）
            hvc = root / "202608" / "hvc.mp4"
            hvc.write_bytes(b"\x00" * 8 + b"ftyp" + b"\x00" * 16 + b"hvc1" + b"\x00" * 8)
            avc = root / "202608" / "avc.mp4"
            avc.write_bytes(b"\x00" * 8 + b"ftyp" + b"\x00" * 16 + b"avc1" + b"\x00" * 8)
            plain = root / "202608" / "plain.mp4"
            plain.write_bytes(bytes(range(64)))
            self.assertEqual(module._sniff_video_codec(hvc), "hevc")
            self.assertEqual(module._sniff_video_codec(avc), "avc1")
            self.assertEqual(module._sniff_video_codec(plain), "")

        # 小节：file 类 raw 下载带 attachment + filename*
        media_id = "ef" * 16
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "202608").mkdir()
            (root / "202608" / f"{media_id}.pdf").write_bytes(b"%PDF-fake")

            class FileRepository(Repository):
                async def media_file(self, media_id):
                    return {
                        "media_id": media_id,
                        "kind": "file",
                        "file_name": f"{media_id}.pdf",
                        "ext": "pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": 9,
                        "created_at": "2026-08-11 02:00:00",
                    }

            module = load_module(args={})
            controller = module.ChatMemoryUiWebApi(
                Context(), FileRepository(), media_root=root
            )
            response = asyncio.run(controller.media(media_id))
            disposition = response[2]["Content-Disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn(f"filename*=UTF-8''{media_id}.pdf", disposition)

        # 小节：未归档 → 400；无 media_root → 503
        media_id = "cd" * 16

        class EmptyRepository(Repository):
            async def media_file(self, media_id):
                raise ValueError("媒体未归档或已被清理")

        module = load_module(args={})
        controller = module.ChatMemoryUiWebApi(Context(), EmptyRepository())
        response = asyncio.run(controller.media(media_id))
        self.assertEqual(response[1], 400)
        self.assertFalse(response[0]["success"])

        class RowRepository(Repository):
            async def media_file(self, media_id):
                return {"media_id": media_id, "file_name": f"{media_id}.png",
                        "kind": "image", "mime_type": "image/png",
                        "size_bytes": 4, "created_at": "2026-08-11 02:00:00"}

        module2 = load_module(args={})
        controller2 = module2.ChatMemoryUiWebApi(Context(), RowRepository())
        response2 = asyncio.run(controller2.media(media_id))
        self.assertEqual(response2[1], 503)
        self.assertFalse(response2[0]["success"])

    def test_export_snapshot_flow(self):
        """导出快照：生成→下载→非法名→不存在，四段行为。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            export_root = Path(tmp)

            class ExportRepository(Repository):
                def export_snapshot(self, dest):
                    dest.write_bytes(b"fake-snapshot")
                    return len(b"fake-snapshot")

            module = load_module(body={})
            controller = module.ChatMemoryUiWebApi(
                Context(), ExportRepository(), export_dir=export_root
            )
            response = asyncio.run(controller.export())
            self.assertTrue(response["success"])
            name = response["data"]["file_name"]
            self.assertRegex(name, r"^chat_memory_\d{8}_\d{6}_[a-f0-9]{6}\.db$")
            self.assertTrue((export_root / name).is_file())

            controller2 = module.ChatMemoryUiWebApi(
                Context(), ExportRepository(), export_dir=export_root
            )
            resp2 = asyncio.run(controller2.export_file(name))
            self.assertEqual(resp2[1], 200)
            self.assertEqual(resp2[0], b"fake-snapshot")
            self.assertIn("attachment", resp2[2]["Content-Disposition"])

            resp3 = asyncio.run(controller2.export_file("../../etc/passwd"))
            self.assertEqual(resp3[1], 400)
            resp4 = asyncio.run(
                controller2.export_file("chat_memory_20200101_000000_abcdef.db")
            )
            self.assertEqual(resp4[1], 404)


if __name__ == "__main__":
    unittest.main()
