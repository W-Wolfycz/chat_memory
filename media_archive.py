"""ChatMemory 媒体归档：本地源同步接管 + 远程源异步下载，尽力而为。

设计原则（见 docs/media_archive_design.md）：
- 管线零阻塞：捕获同步段只做本地文件拷贝（毫秒级）与队列入队；
- 失败不影响主存档：下载失败/超时/队列满均静默，不落归档行；
- 归档行只在文件真正落盘后写入；文件路径不落库（由 file_name + created_at
  推导为 media/<YYYYMM>/<media_id>.<ext>），文件名随机化、无隐私标识；
- 不存原始 URL、绝对路径；日志脱敏。
"""

import asyncio
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger("ChatMemory.MediaArchive")

# 参与归档的 kind（emoji/poke/forward 只有元信息，不落盘）
ARCHIVE_KINDS = ("image", "video", "voice", "file")

_QUEUE_MAX = 300
_WORKER_COUNT = 2
_DEFAULT_TIMEOUT_SEC = 15
_VIDEO_TIMEOUT_SEC = 45
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_VIDEO_MAX_BYTES = 200 * 1024 * 1024

# 常见扩展名白名单（URL path 后缀或文件名后缀）；未知则回退 magic 嗅探，再不行用 .bin
_EXT_ALLOWLIST = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "ico"},
    "video": {"mp4", "mov", "mkv", "webm", "avi"},
    "voice": {"amr", "silk", "mp3", "m4a", "wav", "ogg", "aac", "flac"},
    "file": None,  # 不限，sanitize 后取后缀
}

_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "ico": "image/x-icon",
    "mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
    "webm": "video/webm", "avi": "video/x-msvideo",
    "amr": "audio/amr", "silk": "audio/silk", "mp3": "audio/mpeg",
    "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg",
    "aac": "audio/aac", "flac": "audio/flac",
    "pdf": "application/pdf",
}

_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _month_key(created_at: datetime) -> str:
    return created_at.strftime("%Y%m")


def _parse_created_at(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _sanitize_ext(ext: str) -> str:
    value = (ext or "").lower().lstrip(".")
    return value if _SAFE_EXT_RE.fullmatch(value) else ""


def _guess_ext_from_name(name: str, kind: str) -> str:
    ext = _sanitize_ext(Path(name or "").suffix)
    if not ext:
        return ""
    allow = _EXT_ALLOWLIST.get(kind)
    if allow is not None and ext not in allow:
        return ""
    return ext


def _sniff_ext(path: Path, kind: str) -> str:
    """按 magic bytes 嗅探常见媒体类型；失败返回空串。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""
    if not head:
        return ""
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb"):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"ftyp"):
        return "mp4"
    if head.startswith(b"fLaC"):
        return "flac"
    return ""


def _file_uri_to_path(uri: str) -> str:
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return ""
        path = unquote(parsed.path)
        # file:///C:/... 在 posix 端解析为 /C:/...，剥掉前导斜杠
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return path
    except Exception:
        return ""


class MediaArchiver:
    """媒体归档器：负责落盘与登记，不参与消息正文逻辑。"""

    def __init__(
        self,
        db,
        data_dir: Path,
        enabled: bool,
        include_video: bool,
        retention_days: int,
        max_total_mb: int,
    ):
        self.db = db
        self.data_dir = Path(data_dir)
        self.enabled = bool(enabled)
        self.include_video = bool(include_video)
        self.retention_days = max(1, int(retention_days or 30))
        self.max_total_bytes = max(64, int(max_total_mb or 2048)) * 1024 * 1024
        self.media_dir = self.data_dir / "media"
        self.tmp_dir = self.media_dir / ".tmp"
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._workers: list[asyncio.Task] = []
        self._started = False

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(_WORKER_COUNT):
            self._workers.append(asyncio.create_task(self._worker()))
        logger.info("[ChatMemory] 媒体归档已启用（后台下载 worker=%d）", _WORKER_COUNT)

    async def stop(self) -> None:
        self._started = False
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        await self._cleanup_tmp()

    # ── 捕获同步段（毫秒级，尽力而为）──────────────────

    def allowed_kind(self, kind: str) -> bool:
        if not self.enabled or kind not in ARCHIVE_KINDS:
            return False
        if kind == "video" and not self.include_video:
            return False
        return True

    def _cap_bytes(self, kind: str) -> int:
        return _VIDEO_MAX_BYTES if kind == "video" else _DEFAULT_MAX_BYTES

    def _timeout_sec(self, kind: str) -> int:
        return _VIDEO_TIMEOUT_SEC if kind == "video" else _DEFAULT_TIMEOUT_SEC

    def _dest(self, file_name: str, created_at: datetime) -> Path:
        return self.media_dir / _month_key(created_at) / file_name

    async def archive_local(
        self,
        media_id: str,
        src: str,
        umo: str,
        conversation_id: str,
        turn_id: str,
        kind: str,
        name: str = "",
    ) -> bool:
        """同步接管本地文件（事件级临时路径必须在事件结束前拷贝）。"""
        if not self.allowed_kind(kind):
            return False
        try:
            src_path = Path(src)
            size = src_path.stat().st_size
            if size <= 0 or size > self._cap_bytes(kind):
                return False
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.tmp_dir / f"{media_id}.part"
            try:
                await asyncio.to_thread(shutil.copyfile, src_path, tmp)
            except Exception:
                tmp.unlink(missing_ok=True)
                return False
            return await self._finalize(
                media_id, tmp, umo, conversation_id, turn_id, kind, name
            )
        except Exception as exc:
            logger.debug("[ChatMemory] 本地媒体接管失败 kind=%s: %s", kind, exc)
            return False

    async def archive_bytes(
        self,
        media_id: str,
        data: bytes,
        umo: str,
        conversation_id: str,
        turn_id: str,
        kind: str,
        name: str = "",
    ) -> bool:
        """同步写入已解码字节（base64/data URI 等罕见来源）。"""
        if not self.allowed_kind(kind) or not data:
            return False
        if len(data) > self._cap_bytes(kind):
            return False
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"{media_id}.part"
        try:
            await asyncio.to_thread(tmp.write_bytes, data)
        except Exception:
            tmp.unlink(missing_ok=True)
            return False
        return await self._finalize(
            media_id, tmp, umo, conversation_id, turn_id, kind, name
        )

    async def _finalize(
        self,
        media_id: str,
        tmp: Path,
        umo: str,
        conversation_id: str,
        turn_id: str,
        kind: str,
        name: str,
    ) -> bool:
        """定扩展名、原子落位、登记归档行。"""
        try:
            ext = _guess_ext_from_name(name, kind) or _sniff_ext(tmp, kind) or "bin"
            if kind == "file" and not name:
                name = f"file.{ext}"
            now = _utc_now()
            dest = self._dest(f"{media_id}.{ext}", now)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
            size = dest.stat().st_size
            await self.db.insert_media_archive(
                media_id=media_id,
                umo=umo,
                conversation_id=conversation_id,
                turn_id=turn_id,
                kind=kind,
                file_name=dest.name,
                ext=ext,
                mime_type=_MIME_BY_EXT.get(ext),
                size_bytes=size,
                created_at=now,
            )
            return True
        except Exception as exc:
            logger.debug("[ChatMemory] 媒体归档登记失败 %s: %s", media_id[:8], exc)
            tmp.unlink(missing_ok=True)
            return False

    # ── 远程下载（后台，尽力而为）──────────────────────

    def enqueue(self, item: dict) -> bool:
        """入队远程下载任务；队列满直接丢弃（不丢旧任务）。"""
        if not self.enabled or not self.allowed_kind(item.get("kind") or ""):
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                timeout = self._timeout_sec(item.get("kind") or "")
                await asyncio.wait_for(self._download(item), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "[ChatMemory] 媒体下载失败 kind=%s id=%s: %s",
                    item.get("kind"), str(item.get("media_id") or "")[:8], exc,
                )
            finally:
                self._queue.task_done()

    async def _download(self, item: dict) -> None:
        media_id = str(item["media_id"])
        url = item["url"]
        kind = str(item["kind"])
        name = str(item.get("name") or "")
        cap = self._cap_bytes(kind)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"{media_id}.part"
        try:
            import aiohttp  # 懒加载：本地 mock 测试环境不依赖 aiohttp

            total = 0
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec(kind) + 5)
            ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"http {resp.status}")
                    content_length = resp.content_length
                    if content_length is not None and content_length > cap:
                        raise RuntimeError("content-length 超上限")
                    with open(tmp, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > cap:
                                raise RuntimeError("下载超上限")
                            fh.write(chunk)
            if total <= 0:
                raise RuntimeError("空文件")
            await self._finalize(
                media_id,
                tmp,
                item["umo"],
                item["conversation_id"],
                item["turn_id"],
                kind,
                name,
            )
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # ── 清理 ─────────────────────────────────────────

    async def delete_files(self, rows) -> int:
        """删除 ``rows``（file_name + created_at）对应的媒体文件。"""
        deleted = 0
        for row in rows or []:
            file_name = row.get("file_name")
            created_at = _parse_created_at(row.get("created_at"))
            if not file_name or created_at is None:
                continue
            path = self._dest(file_name, created_at)
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                pass
        return deleted

    async def cleanup_cycle(self) -> dict:
        """一轮清理：保留期 → 总量上限 → 孤儿文件。返回统计（日志用）。"""
        stats = {"retention": 0, "quota": 0, "orphan": 0}
        if not self.enabled:
            return stats
        cutoff = _utc_now() - timedelta(days=self.retention_days)
        try:
            rows = await self.db.query_media_archive_for_cleanup(limit=1000)
            expired_ids = [
                r["media_id"]
                for r in rows
                if _parse_created_at(r.get("created_at")) is not None
                and _parse_created_at(r["created_at"]) < cutoff
            ]
            if expired_ids:
                await self.db.delete_media_archive_by_ids(expired_ids)
                stats["retention"] = len(expired_ids)
                await self._delete_by_ids(expired_ids)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体保留期清理失败: %s", exc)
        try:
            total = await self.db.media_archive_total_size()
            if total > self.max_total_bytes:
                rows = await self.db.query_media_archive_for_cleanup(limit=1000)
                to_delete: list[str] = []
                for row in rows:
                    total -= int(row.get("size_bytes") or 0)
                    to_delete.append(row["media_id"])
                    if total <= self.max_total_bytes:
                        break
                if to_delete:
                    await self.db.delete_media_archive_by_ids(to_delete)
                    stats["quota"] = await self._delete_by_ids(to_delete)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体总量清理失败: %s", exc)
        try:
            stats["orphan"] = await self._orphan_scan(cutoff)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体孤儿文件清理失败: %s", exc)
        return stats

    async def _delete_by_ids(self, media_ids: list[str]) -> int:
        deleted = 0
        # 按 id 找文件：file_name 为 <media_id>.<ext>，逐月目录查找成本可控，
        # 优先直接扫目录而不是逐 id 猜扩展名。
        wanted = set(media_ids)
        for month_dir in self.media_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9]"):
            if not month_dir.is_dir():
                continue
            for path in month_dir.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                stem = path.name.split(".", 1)[0]
                if stem in wanted:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError:
                        pass
        return deleted

    async def _orphan_scan(self, cutoff: datetime) -> int:
        """删除 mtime 早于保留期且无归档行的文件（兜底，防 DB 已删而文件残留）。"""
        removed = 0
        stale_files: dict[str, Path] = {}
        for month_dir in self.media_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9]"):
            if not month_dir.is_dir():
                continue
            for path in month_dir.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime) >= cutoff:
                        continue
                except OSError:
                    continue
                stale_files[path.name.split(".", 1)[0]] = path
        if not stale_files:
            return 0
        alive = await self.db.query_media_archive_by_ids(list(stale_files.keys()))
        for media_id, path in stale_files.items():
            if media_id in alive:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    async def _cleanup_tmp(self) -> None:
        try:
            for path in self.tmp_dir.glob("*.part"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except Exception:
            pass


def mint_media_id() -> str:
    """媒体条目 id：32 位随机 hex，无任何隐私信息。"""
    return uuid.uuid4().hex
