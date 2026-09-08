"""Authenticated Plugin Page API for ChatMemory UI."""

from __future__ import annotations

import asyncio
import base64
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from quart import request

from .repository import ChatMemoryRepository, DatabaseUnavailableError


PLUGIN_NAME = "chat_memory"
RepositoryUpdater = Callable[
    [str, bool], Awaitable[tuple[ChatMemoryRepository, dict[str, Any]]]
]

# 导出快照文件名（时间戳 + 6 位随机）：同时用作防路径穿越的校验白名单
_EXPORT_NAME_RE = re.compile(r"^chat_memory_[0-9]{8}_[0-9]{6}_[a-f0-9]{6}\.db$")
_EXPORT_RETENTION_SEC = 24 * 3600

# base64 内联模式的大小上限（避免大视频把 JSON 撑爆；超限引导走下载）
_BASE64_MAX_BYTES = 16 * 1024 * 1024


class ChatMemoryUiWebApi:
    def __init__(
        self,
        context,
        repository: ChatMemoryRepository,
        *,
        default_database_path: str | Path | None = None,
        repository_updater: RepositoryUpdater | None = None,
        media_root: str | Path | None = None,
        export_dir: str | Path | None = None,
    ) -> None:
        self.context = context
        self.repository = repository
        self.default_database_path = Path(
            default_database_path or repository.db_path
        ).resolve(strict=False)
        self.repository_updater = repository_updater
        self.media_root = (
            Path(media_root).resolve(strict=False) if media_root else None
        )
        self.export_dir = (
            Path(export_dir).resolve(strict=False) if export_dir else None
        )

    async def about(self):
        return _ok(
            {
                "name": PLUGIN_NAME,
                "display_name": "ChatMemory 查询台",
                "version": "1.5.2",
                "read_only": True,
                "default_page_size": self.repository.default_page_size,
                "max_page_size": self.repository.max_page_size,
                "timezone": str(self.repository.timezone),
            }
        )

    async def settings(self):
        return _ok(self._settings_payload())

    async def update_settings(self):
        payload = await _json_body()
        if payload is None:
            return _error("请求体必须是 JSON 对象", 400)
        if self.repository_updater is None:
            return _error("当前插件实例不支持保存 UI 设置", 503)

        database_path = str(payload.get("database_path", "") or "").strip()
        if len(database_path) > 4096:
            return _error("数据库路径过长", 400)
        immutable_fallback = payload.get("immutable_fallback", True)
        if not isinstance(immutable_fallback, bool):
            return _error("主库快照回退选项必须是布尔值", 400)
        return await self._call(
            self._apply_settings, database_path, immutable_fallback
        )

    async def health(self):
        return await self._call(self.repository.health)

    async def overview(self):
        return await self._call(self.repository.overview)

    async def facets(self):
        filters = {
            key: request.args.get(key, None)
            for key in (
                "umo",
                "conversation_id",
                "user_id",
                "persona_id",
                "role",
                "llm_status",
                "content_kind",
                "platform_name",
                "message_type",
            )
        }
        return await self._call(self.repository.facets, filters)

    async def query_records(self):
        payload = await _json_body()
        if payload is None:
            return _error("请求体必须是 JSON 对象", 400)
        return await self._call(self.repository.query, payload)

    async def query_tools(self):
        payload = await _json_body()
        if payload is None:
            return _error("请求体必须是 JSON 对象", 400)
        return await self._call(self.repository.tool_records, payload)

    async def record_detail(self):
        raw_id = request.args.get("id", "")
        return await self._call(self.repository.record, raw_id)

    async def media(self, media_id):
        """媒体二进制端点：默认返回原始字节；``?as=base64`` 返回内联 JSON。"""
        try:
            row = await self.repository.media_file(media_id)
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            logger.exception("[ChatMemoryUI] 媒体查询失败")
            return _error(f"查询失败：{exc}", 500)
        if self.media_root is None:
            return _error("媒体目录不可用", 503)
        created_at = str(row.get("created_at") or "")
        month = created_at[:7].replace("-", "")
        if not month.isdigit() or len(month) != 6:
            return _error("归档记录时间异常", 500)
        file_name = str(row.get("file_name") or "")
        # 纵深防御：归档写入侧已限定 <media_id>.<ext>，读侧仍拒绝路径分隔符/穿越
        if not file_name or Path(file_name).name != file_name:
            return _error("归档记录文件名异常", 500)
        path = self.media_root / month / file_name
        mime = str(row.get("mime_type") or "") or "application/octet-stream"
        want_mode = str(request.args.get("as", "")).strip().lower()
        if want_mode not in ("", "base64", "thumb", "file_token"):
            return _error(f"未知的 as 模式：{want_mode}", 400)
        if want_mode in ("base64", "thumb"):
            # 先按归档记录大小拒绝，避免几十 MB 的媒体为一条 413 而整读进内存
            if int(row.get("size_bytes") or 0) > _BASE64_MAX_BYTES:
                return _error("媒体过大，请使用下载方式获取", 413)
        if want_mode == "file_token":
            # 免鉴权单次文件令牌（AstrBot /api/file/<token>，dashboard 白名单）：
            # iframe 直连 /api/plug 媒体端点会被鉴权中间件 401（拿不到 JWT
            # cookie/header），base64 内联又有 16MB 上限；此模式由前端 fetch
            # 令牌地址整段获取为 blob 播放。令牌单次有效、随机不可枚举。
            # 注意：本分支绝不整读文件（大视频 60MB+ 全量 read_bytes 会让
            # dashboard 同进程内存暴涨、UI 卡死），只嗅探编码与登记路径。
            size = int(row.get("size_bytes") or 0)
            try:
                from astrbot.core import file_token_service
            except Exception as exc:
                logger.warning("[ChatMemoryUI] file_token 不可用: %s", exc)
                return _error("当前 AstrBot 版本不支持 file_token", 501)
            codec = ""
            if str(row.get("kind") or "") == "video":
                try:
                    codec = await asyncio.to_thread(_sniff_video_codec, path)
                except OSError:
                    codec = ""
            try:
                token = await file_token_service.register_file(str(path))
            except Exception as exc:
                logger.warning("[ChatMemoryUI] file_token 注册失败: %s", exc)
                return _error("媒体令牌生成失败", 500)
            return _ok({
                "media_id": str(row.get("media_id") or ""),
                "kind": str(row.get("kind") or ""),
                "mime": mime,
                "size": size,
                "codec": codec,
                "file_url": f"/api/file/{token}",
            })
        if want_mode in ("base64", "thumb"):
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                return _error("媒体文件已缺失", 404)
            if len(data) != int(row.get("size_bytes") or 0):
                logger.warning(
                    "[ChatMemoryUI] 媒体文件大小与归档记录不一致：%s", media_id[:8]
                )
            if len(data) > _BASE64_MAX_BYTES:
                return _error("媒体过大，请使用下载方式获取", 413)
            payload = {
                "media_id": str(row.get("media_id") or ""),
                "kind": str(row.get("kind") or ""),
                "mime": mime,
                "size": len(data),
                "data": base64.b64encode(data).decode("ascii"),
            }
            if want_mode == "thumb" and str(row.get("kind") or "") == "image":
                thumb = await asyncio.to_thread(_downscale_image, data)
                if thumb is not None:
                    thumb_data, thumb_mime = thumb
                    payload.update(
                        {"mime": thumb_mime, "size": len(thumb_data),
                         "data": base64.b64encode(thumb_data).decode("ascii")}
                    )
            return _ok(payload)
        kind = str(row.get("kind") or "")
        disposition = "attachment" if kind == "file" else "inline"
        if kind == "file":
            from urllib.parse import quote

            disposition += f"; filename*=UTF-8''{quote(str(row.get('file_name') or ''))}"
        common = {
            "Content-Type": mime,
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=86400",
        }
        # 视频/语音支持 HTTP Range（206）：只读请求区间，不整读文件（几十 MB
        # 视频每次拖动都整读会把 dashboard 同进程内存拉满）。
        total = await asyncio.to_thread(_file_size, path)
        if total <= 0:
            return _error("媒体文件已缺失", 404)
        headers_obj = getattr(request, "headers", None)
        range_header = (
            str(headers_obj.get("Range") or "") if headers_obj is not None else ""
        )
        if range_header.startswith("bytes=") and kind in ("video", "voice"):
            spec = range_header[len("bytes=") :].split(",", 1)[0]
            start_s, _, end_s = spec.partition("-")
            try:
                if not start_s:
                    # 后缀范围 bytes=-N：取末尾 N 字节；N<=0 或缺失视为非法
                    suffix_len = int(end_s) if end_s else 0
                    if suffix_len <= 0:
                        raise ValueError
                    start = max(0, total - suffix_len)
                    end = total - 1
                else:
                    start = int(start_s)
                    end = int(end_s) if end_s else total - 1
                    if start < 0 or start >= total or end < start:
                        raise ValueError
                    end = min(end, total - 1)
            except (TypeError, ValueError):
                return (
                    {"success": False, "error": "Range 不合法"},
                    416,
                    {"Content-Range": f"bytes */{total}"},
                )
            chunk = await asyncio.to_thread(_read_range, path, start, end - start + 1)
            if chunk is None:
                return _error("媒体文件已缺失", 404)
            # 元组返回：AstrBot 的 _coerce_view_result 会把它包成 Starlette Response
            # （content-type 等头原样透传），测试环境无需安装 starlette。
            return (
                chunk,
                206,
                {
                    **common,
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                },
            )
        # 非 Range（整文件下载/直连）：AstrBot 的响应封装要求 bytes，无法流式返回，
        # 因此这里整读一次；大媒体播放请走 Range，下载请走 bridge.download。
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return _error("媒体文件已缺失", 404)
        if len(data) != int(row.get("size_bytes") or 0):
            logger.warning(
                "[ChatMemoryUI] 媒体文件大小与归档记录不一致：%s", media_id[:8]
            )
        return (
            data,
            200,
            {
                **common,
                "Content-Disposition": disposition,
                "Content-Length": str(len(data)),
            },
        )

    async def export(self):
        """生成 SQLite backup 一致快照（点击时刻的时点）。

        备份在后台线程执行，源库读写不受阻塞；返回快照文件名供下载。
        """
        if self.export_dir is None:
            return _error("导出目录不可用", 503)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"chat_memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.db"
        dest = self.export_dir / file_name
        try:
            size = await asyncio.to_thread(self.repository.export_snapshot, dest)
        except DatabaseUnavailableError as exc:
            dest.unlink(missing_ok=True)
            return _error(str(exc), 503)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            logger.exception("[ChatMemoryUI] 导出快照失败")
            return _error(f"导出失败：{exc}", 500)
        try:
            await asyncio.to_thread(self._cleanup_exports)
        except Exception:
            pass
        return _ok({"file_name": file_name, "size_bytes": int(size)})

    async def export_file(self, file_name):
        """下载导出的快照文件（附件）。"""
        if not _EXPORT_NAME_RE.fullmatch(str(file_name or "")):
            return _error("文件名不合法", 400)
        if self.export_dir is None:
            return _error("导出目录不可用", 503)
        path = self.export_dir / str(file_name)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return _error("导出快照已过期或不存在", 404)
        from urllib.parse import quote

        return (
            data,
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(str(file_name))}"
                ),
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
            },
        )

    def _cleanup_exports(self) -> None:
        """清理超期的导出快照（尽力而为，失败静默）。"""
        now = datetime.now().timestamp()
        for path in self.export_dir.glob("chat_memory_*.db"):
            try:
                if now - path.stat().st_mtime > _EXPORT_RETENTION_SEC:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    async def _apply_settings(
        self, database_path: str, immutable_fallback: bool
    ) -> dict[str, Any]:
        assert self.repository_updater is not None
        repository, health = await self.repository_updater(
            database_path, immutable_fallback
        )
        self.repository = repository
        return {
            **self._settings_payload(),
            "database": health.get("database", {}),
        }

    def _settings_payload(self) -> dict[str, Any]:
        current = self.repository.db_path.resolve(strict=False)
        return {
            "database_path": str(current),
            "default_database_path": str(self.default_database_path),
            "using_default_path": current == self.default_database_path,
            "immutable_fallback": self.repository.allow_immutable_fallback,
        }

    async def _call(self, operation, *args):
        try:
            return _ok(await operation(*args))
        except ValueError as exc:
            return _error(str(exc), 400)
        except DatabaseUnavailableError as exc:
            logger.warning("[ChatMemoryUI] %s", exc)
            return _error(str(exc), 503)
        except Exception as exc:
            logger.exception("[ChatMemoryUI] 查询 API 异常")
            return _error(f"查询失败：{exc}", 500)

    def register(self) -> None:
        endpoints = (
            ("about", self.about, ["GET"], "ChatMemory UI plugin info"),
            ("settings", self.settings, ["GET"], "ChatMemory UI settings"),
            (
                "settings",
                self.update_settings,
                ["POST"],
                "Update ChatMemory UI settings",
            ),
            ("health", self.health, ["GET"], "ChatMemory database health"),
            ("overview", self.overview, ["GET"], "ChatMemory overview"),
            ("facets", self.facets, ["GET"], "ChatMemory query facets"),
            ("query", self.query_records, ["POST"], "Query ChatMemory records"),
            ("record", self.record_detail, ["GET"], "ChatMemory record detail"),
            ("tools", self.query_tools, ["POST"], "Query ChatMemory tool calls"),
            (
                "media/<media_id>",
                self.media,
                ["GET"],
                "ChatMemory archived media file",
            ),
            ("export", self.export, ["POST"], "Export a consistent DB snapshot"),
            (
                "export/<file_name>",
                self.export_file,
                ["GET"],
                "Download an exported DB snapshot",
            ),
        )
        for path, handler, methods, description in endpoints:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", handler, methods, description
            )
        logger.info("[ChatMemoryUI] 已注册 %d 个只读 Web API", len(endpoints))


async def _json_body() -> dict[str, Any] | None:
    try:
        payload = await request.get_json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _downscale_image(data: bytes, max_side: int = 512):
    """把图片缩到最长边 ≤ max_side；PIL 不可用或失败返回 None（调用方回退原图）。"""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.thumbnail((max_side, max_side))
            if image.mode in ("RGBA", "LA", "P"):
                fmt = "PNG"
                mime = "image/png"
            else:
                fmt = "JPEG"
                mime = "image/jpeg"
            buffer = BytesIO()
            image.save(buffer, format=fmt)
            return buffer.getvalue(), mime
    except Exception:
        return None


# 视频内联播放的客户端大小闸门：超过此值的视频不再整段 fetch 成 blob
# （几十 MB 的视频会让 dashboard 页面内存暴涨卡死），直接引导下载观看。
_INLINE_PLAYBACK_MAX_BYTES = 32 * 1024 * 1024


def _sniff_video_codec(path) -> str:
    """只读视频前 512KB 嗅探编码：hevc（hvc1/hev1）/ avc1 / av01 / vp09。

    moov 在文件尾部的文件可能嗅不到（返回空串，由大小闸门兜底）。
    """
    try:
        with open(path, "rb") as fh:
            probe = fh.read(512 * 1024)
    except OSError:
        return ""
    if b"hvc1" in probe or b"hev1" in probe:
        return "hevc"
    if b"avc1" in probe:
        return "avc1"
    if b"av01" in probe:
        return "av01"
    if b"vp09" in probe:
        return "vp09"
    return ""


def _file_size(path) -> int:
    """文件字节数；不存在/不可读返回 0。"""
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _read_range(path, start: int, length: int):
    """只读 ``[start, start+length)`` 区间；失败返回 None（不整读文件）。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(length)
    except OSError:
        return None


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int):
    return {"success": False, "error": str(message)}, status
