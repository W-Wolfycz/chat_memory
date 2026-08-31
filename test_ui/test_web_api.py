from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path


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


def load_module(*, body=None, args=None):
    quart = types.ModuleType("quart")

    class Request:
        def __init__(self):
            self.args = args or {}

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
            },
        )
        self.assertTrue(all(set(route[2]) <= {"GET", "POST"} for route in context.routes))
        self.assertFalse(
            any("delete" in route[0] or "update" in route[0] for route in context.routes)
        )

    def test_query_rejects_non_object_json(self):
        module = load_module(body=[])
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        response = asyncio.run(controller.query_records())
        self.assertEqual(response[1], 400)
        self.assertFalse(response[0]["success"])

    def test_tools_query_rejects_non_object_json_and_forwards(self):
        module = load_module(body=[])
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        response = asyncio.run(controller.query_tools())
        self.assertEqual(response[1], 400)
        self.assertFalse(response[0]["success"])

        module = load_module(body={"tool_name": "draw", "page": 2})
        controller = module.ChatMemoryUiWebApi(Context(), Repository())
        response = asyncio.run(controller.query_tools())
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["tool_payload"]["tool_name"], "draw")
        self.assertEqual(response["data"]["tool_payload"]["page"], 2)

    def test_facets_and_record_forward_parameters(self):
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

    def test_settings_are_read_and_updated_in_ui(self):
        module = load_module(
            body={
                "database_path": "D:/Memory/new.db",
                "immutable_fallback": False,
            }
        )
        updates = []

        async def updater(path, fallback):
            updates.append((path, fallback))
            repository = Repository(path, fallback)
            return repository, {"database": {"path": path, "mode": "wal_aware"}}

        controller = module.ChatMemoryUiWebApi(
            Context(),
            Repository(),
            default_database_path="C:/AstrBot/chat_memory.db",
            repository_updater=updater,
        )
        before = asyncio.run(controller.settings())
        self.assertTrue(before["data"]["using_default_path"])

        updated = asyncio.run(controller.update_settings())
        self.assertEqual(updates, [("D:/Memory/new.db", False)])
        # 平台无关：Windows 上 resolve 为 D:\Memory\new.db，Linux 上为 cwd 相对路径
        self.assertEqual(
            updated["data"]["database_path"],
            str(Path("D:/Memory/new.db").resolve(strict=False)),
        )
        self.assertFalse(updated["data"]["immutable_fallback"])
        self.assertFalse(updated["data"]["using_default_path"])

    def test_settings_reject_non_boolean_fallback(self):
        module = load_module(
            body={"database_path": "D:/Memory/new.db", "immutable_fallback": "false"}
        )
        async def updater(path, fallback):
            return Repository(path, fallback), {}

        controller = module.ChatMemoryUiWebApi(
            Context(), Repository(), repository_updater=updater
        )
        response = asyncio.run(controller.update_settings())
        self.assertEqual(response[1], 400)

    def test_media_endpoint_base64_and_raw(self):
        import base64 as b64
        import tempfile

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
            # 元组返回（AstrBot 运行时会包成 Response）：(bytes, status, headers)
            self.assertEqual(response2[1], 200)
            self.assertEqual(response2[0], payload)
            self.assertEqual(response2[2]["Content-Type"], "image/png")

    def test_media_raw_file_disposition_filename(self):
        """file 类 raw 下载：Content-Disposition 为 attachment 且带 filename*。"""
        import tempfile

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

    def test_media_endpoint_missing_archive_and_missing_root(self):
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


if __name__ == "__main__":
    unittest.main()
