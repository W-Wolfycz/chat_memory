"""Authenticated Plugin Page API for ChatMemory UI."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from quart import request

from .repository import ChatMemoryRepository, DatabaseUnavailableError


PLUGIN_NAME = "chat_memory"
RepositoryUpdater = Callable[
    [str, bool], Awaitable[tuple[ChatMemoryRepository, dict[str, Any]]]
]

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

    async def about(self):
        return _ok(
            {
                "name": PLUGIN_NAME,
                "display_name": "ChatMemory 查询台",
                "version": "1.4.0",
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
        path = self.media_root / month / str(row.get("file_name") or "")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return _error("媒体文件已缺失", 404)
        if len(data) != int(row.get("size_bytes") or 0):
            logger.warning(
                "[ChatMemoryUI] 媒体文件大小与归档记录不一致：%s", media_id[:8]
            )
        mime = str(row.get("mime_type") or "") or "application/octet-stream"
        want_mode = str(request.args.get("as", "")).strip().lower()
        if want_mode in ("base64", "thumb"):
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
        disposition = "attachment" if str(row.get("kind") or "") == "file" else "inline"
        if str(row.get("kind") or "") == "file":
            from urllib.parse import quote

            disposition += f"; filename*=UTF-8''{quote(str(row.get('file_name') or ''))}"
        # 元组返回：AstrBot 的 _coerce_view_result 会把它包成 Starlette Response
        # （content-type 等头原样透传），测试环境无需安装 starlette。
        return (
            data,
            200,
            {
                "Content-Type": mime,
                "Content-Disposition": disposition,
                "Content-Length": str(len(data)),
                "Cache-Control": "private, max-age=86400",
            },
        )

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


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int):
    return {"success": False, "error": str(message)}, status
