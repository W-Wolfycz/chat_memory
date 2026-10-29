"""ChatMemory 媒体归档：本地源同步接管 + 远程源异步下载，尽力而为。

设计原则（见 docs/media_archive_design.md）：
- 管线零阻塞：捕获同步段只做本地文件拷贝（毫秒级）与队列入队；
- 失败不影响主存档：下载失败/超时/队列满均静默，不落归档行；
- 归档行只在文件真正落盘后写入；文件路径不落库（由 file_name + created_at
  推导为 media/<YYYYMM>/<内容哈希>.<ext>），文件名不含隐私标识；
- 内容寻址：同字节文件跨实例/跨行只留一份，删除按引用计数（还有行在用就不删）；
- 不存原始 URL、绝对路径；日志脱敏。
"""

import asyncio
import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger

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


def _parse_month_dir(name: str) -> Optional[datetime]:
    """月份目录名（YYYYMM）→ 该月 1 号的 UTC naive datetime；不合法返回 None。"""
    try:
        return datetime.strptime(f"{name}01", "%Y%m%d")
    except (TypeError, ValueError):
        return None


def _hash_file(path: Path) -> str:
    """文件内容哈希（sha256 前 32 位十六进制）：内容寻址的文件名与去重键。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:32]


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
    # MP4：box 头 4 字节是 size，'ftyp' 在偏移 4
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"fLaC"):
        return "flac"
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
        # 归档/下载失败计数：前几次 WARNING，之后 DEBUG（不静默也不刷屏）
        self._failures = 0

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
            # 只记异常类型：异常串可能含源/目标绝对路径，日志不落路径
            logger.debug(
                "[ChatMemory] 本地媒体接管失败 kind=%s: %s", kind, type(exc).__name__
            )
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
            # 内容寻址：文件名＝内容哈希（不是 media_id）。多 BOT 各自归档同一张图时，
            # 平台 file id 不同但字节相同 → 落到同一个文件；同月内天然去重，
            # 也让人一眼看出"这两个 media_id 是同一张图"。
            content_hash = await asyncio.to_thread(_hash_file, tmp)
            dest = self._dest(f"{content_hash}.{ext}", now)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 只有**本次真正落位**的文件才允许在登记失败时回删：dest 已存在说明它正被
            # 别的归档行引用（多实例同图共用），删掉就是不可逆地毁掉别人的媒体。
            created_here = not dest.is_file()
            if created_here:
                os.replace(tmp, dest)
            else:
                tmp.unlink(missing_ok=True)      # 已有同内容文件：复用，不重写
            try:
                size = dest.stat().st_size
                stored = await self.db.insert_media_archive(
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
                    content_hash=content_hash,
                )
                if not stored:
                    # media_id 冲突（行早已存在）也会走到这里：INSERT OR IGNORE 是空操作。
                    # 复用共用文件时不能回删，只把这次当失败上报。
                    raise RuntimeError("media archive row not inserted")
            except Exception:
                if created_here:
                    dest.unlink(missing_ok=True)
                raise
            logger.debug(
                "[ChatMemory] 媒体归档完成 kind=%s ext=%s size=%d",
                kind, ext, size,
            )
            return True
        except Exception as exc:
            self._failures += 1
            log_fn = logger.warning if self._failures <= 3 else logger.debug
            log_fn(
                "[ChatMemory] 媒体归档登记失败 %s（累计 %d 次）: %s",
                media_id, self._failures, type(exc).__name__,
            )
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
                self._failures += 1
                log_fn = logger.warning if self._failures <= 3 else logger.debug
                log_fn(
                    "[ChatMemory] 媒体下载失败 kind=%s id=%s（累计 %d 次）: %s",
                    item.get("kind"), str(item.get("media_id") or ""),
                    self._failures, type(exc).__name__,
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
        # finally 清理 .part：超时取消（CancelledError 是 BaseException）也要删
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
        finally:
            tmp.unlink(missing_ok=True)

    # ── 清理 ─────────────────────────────────────────

    async def delete_files(self, rows) -> int:
        """删除 ``rows``（file_name + created_at）对应的媒体文件。

        内容寻址后多个归档行可能指向同一个文件——**还有别的行引用时保留**（引用计数），
        返回真正删掉的文件数。
        """
        deleted = 0
        kept = 0
        for row in rows or []:
            file_name = row.get("file_name")
            created_at = _parse_created_at(row.get("created_at"))
            if not file_name or created_at is None:
                continue
            path = self._dest(file_name, created_at)
            try:
                if not path.is_file():
                    continue
                # 内容寻址后一个文件可能被多个 media_id 共用（多实例各存一份同一张图）：
                # 还有别的行引用它就不能删，否则删掉自己的会把别人的图删没。
                if await self.db.media_file_referenced(
                    file_name=file_name, created_at=created_at
                ):
                    kept += 1
                    continue
                path.unlink()
                deleted += 1
            except OSError:
                pass
        if kept:
            logger.debug("[ChatMemory] 媒体文件被其它归档行共用，保留 %d 个", kept)
        return deleted

    async def dedup_existing(
        self, limit: int = 1000, *, max_bytes: int = 1 << 30
    ) -> int:
        """把老库里的媒体文件改成内容寻址并顺手去重，返回处理行数（断点续跑）。

        老库文件名是 ``<media_id>.<ext>``（每个实例一份），这里逐批换成
        ``<内容哈希>.<ext>``：同内容的第二份直接删掉重复文件、把行指过去。
        按 ``content_hash IS NULL`` 取批，天然可断点续跑；全部处理完就是空转。
        ``max_bytes`` 限制单轮读取量（几十 GB 的视频库也不会一小时搬完），
        本轮没轮到的行留到下一轮。文件已不在的行标成"无需迁移"，否则它们会
        永远占着待迁移集合的前排、把后面真正要改名的行堵死。
        """
        if not self.enabled:
            return 0
        rows = await self.db.query_media_without_hash(limit=limit)
        done = 0
        read_bytes = 0
        for row in rows or []:
            media_id = str(row.get("media_id") or "")
            file_name = str(row.get("file_name") or "")
            ext = str(row.get("ext") or "bin")
            created_at = _parse_created_at(row.get("created_at"))
            if not (media_id and file_name and created_at is not None):
                continue
            path = self._dest(file_name, created_at)
            try:
                if not path.is_file():
                    # 文件已被删（保留期/人工清理）：标成"无需迁移"（空哈希）。
                    # 不标的话这些行每轮都被重新取出来，把后面要处理的行永久挡住。
                    await self.db.update_media_content_hash(media_id, content_hash="")
                    done += 1
                    continue
                if read_bytes >= max_bytes:
                    break                       # 本轮读取量已够，剩下的下一轮继续
                read_bytes += int(row.get("size_bytes") or 0)
                content_hash = await asyncio.to_thread(_hash_file, path)
                new_name = f"{content_hash}.{ext}"
                new_path = self._dest(new_name, created_at)
                if new_path != path:
                    if new_path.is_file():
                        path.unlink(missing_ok=True)      # 已有同内容文件：删重复
                    else:
                        os.replace(path, new_path)
                await self.db.update_media_content_hash(
                    media_id, content_hash=content_hash, file_name=new_name
                )
                done += 1
            except Exception as exc:  # noqa: BLE001 - 单个失败不影响整批
                logger.debug(
                    "[ChatMemory] 媒体去重跳过 %s: %s", media_id, type(exc).__name__
                )
        return done

    async def cleanup_cycle(self) -> dict:
        """一轮清理：保留期 → 总量上限 → 孤儿文件。返回统计（日志用）。"""
        stats = {"retention": 0, "quota": 0, "orphan": 0, "dedup": 0}
        if not self.enabled:
            return stats
        cutoff = _utc_now() - timedelta(days=self.retention_days)
        try:
            rows = await self.db.query_media_archive_for_cleanup(limit=1000)
            expired_rows = [
                r
                for r in rows
                if _parse_created_at(r.get("created_at")) is not None
                and _parse_created_at(r["created_at"]) < cutoff
            ]
            if expired_rows:
                await self.db.delete_media_archive_by_ids(
                    [r["media_id"] for r in expired_rows]
                )
                stats["retention"] = await self.delete_files(expired_rows)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体保留期清理失败: %s", exc)
        try:
            total = await self.db.media_archive_total_size()
            if total > self.max_total_bytes:
                rows = await self.db.query_media_archive_for_cleanup(limit=1000)
                # 一个物理文件可能被多行引用（内容寻址）：只有删掉**最后一行**才真的腾出
                # 磁盘，中间那些行不该计入释放量，否则会按"每行都腾空间"的错觉删过头。
                counts = await self.db.media_archive_file_counts()
                freed = 0
                to_delete: list[dict] = []
                for row in rows:
                    created_at = _parse_created_at(row.get("created_at"))
                    key = (
                        str(row.get("file_name") or ""),
                        created_at.strftime("%Y%m") if created_at else "",
                    )
                    counts[key] = counts.get(key, 0) - 1
                    if counts[key] <= 0:
                        freed += int(row.get("size_bytes") or 0)
                    to_delete.append(row)
                    if total - freed <= self.max_total_bytes:
                        break
                if to_delete:
                    await self.db.delete_media_archive_by_ids(
                        [r["media_id"] for r in to_delete]
                    )
                    stats["quota"] = await self.delete_files(to_delete)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体总量清理失败: %s", exc)
        try:
            stats["dedup"] = await self.dedup_existing(limit=1000)
            if stats["dedup"]:
                # 迁移进度写 INFO：回传日志时能看出"内容寻址迁移跑到哪了 / 有没有在跑"。
                logger.info(
                    "[ChatMemory] 媒体内容寻址迁移：本轮处理 %d 行", stats["dedup"]
                )
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体去重失败: %s", exc)
        try:
            stats["orphan"] = await self._orphan_scan(cutoff)
        except Exception as exc:
            logger.warning("[ChatMemory] 媒体孤儿文件清理失败: %s", exc)
        return stats

    async def _orphan_scan(self, cutoff: datetime) -> int:
        """删除 mtime 早于保留期且无归档行的文件（兜底，防 DB 已删而文件残留）。"""
        removed = 0
        # 按"文件"判定，而不是按文件名前缀当 media_id：内容寻址后文件名是内容哈希，
        # 前缀查询永远查不到行，会把仍在使用的文件当孤儿删掉（多实例共用同一个文件时
        # 更危险）。这里直接问 (file_name, 月份) 是否还有归档行在用。
        for month_dir in self.media_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9]"):
            if not month_dir.is_dir():
                continue
            month_dt = _parse_month_dir(month_dir.name)
            if month_dt is None:
                continue
            for path in month_dir.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    mtime_utc = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).replace(tzinfo=None)
                    if mtime_utc >= cutoff:
                        continue
                except OSError:
                    continue
                try:
                    if await self.db.media_file_referenced(
                        file_name=path.name, created_at=month_dt
                    ):
                        continue          # 还有归档行在用（含内容寻址后的共用文件）
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
