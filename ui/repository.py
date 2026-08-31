"""Read-only SQLite access for ChatMemory schema v4.

The repository intentionally does not import ChatMemory internals. AstrBot does not
guarantee cross-plugin module imports, and a UI query plugin should remain usable
while the writer plugin is temporarily disabled.

Schema v4 adds the ``chat_memory_tool_records`` table for LLM tool calls; it is
optional at read time so databases migrated from v3 and earlier remain readable.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo


TABLE_NAME = "chat_memory_records"
TOOL_TABLE_NAME = "chat_memory_tool_records"
MEDIA_ARCHIVE_TABLE_NAME = "chat_memory_media_archive"
REQUIRED_COLUMNS = {
    "id",
    "umo",
    "conversation_id",
    "user_id",
    "role",
    "content",
    "llm_status",
    "content_kind",
    "created_at",
}
VALID_ROLES = {"user", "assistant"}
VALID_STATUSES = {"", "llm_pending", "llm_success", "proactive", "orphan"}
VALID_KINDS = {
    "text",
    "image",
    "video",
    "voice",
    "file",
    # face 已更名为 emoji(1.2.5 数据迁移);保留 face 以兼容未迁移的旧库
    "face",
    "emoji",
    "forward",
    "system_event",
    "poke",
}
AT_TOKEN_RE = re.compile(r"⟦CM_AT:(\d+)⟧")
# 与 chat_memory.relation_codec 对称：用户正文 ⟦/⟧ 的哨兵字符，展示侧还原
_SENTINEL_LEFT = "⦑"
_SENTINEL_RIGHT = "⦒"
# 媒体/动作位置 token(relation v1 新增 media 数组),UI 渲染为可读占位
MEDIA_TOKEN_RE = re.compile(
    r"⟦CM_(?:IMAGE|VIDEO|VOICE|FILE|EMOJI|FORWARD|POKE):(\d+)⟧"
)
# 展示占位符用 ⟦⟧：[] 可能是用户原文（如真的输入"[图片]"），⟦⟧ 用户几乎不会打
MEDIA_KIND_LABELS = {
    "image": "⟦图片⟧",
    "video": "⟦视频⟧",
    "voice": "⟦语音⟧",
    "file": "⟦文件⟧",
    "emoji": "⟦表情⟧",
    "forward": "⟦转发消息⟧",
    "poke": "⟦戳一戳⟧",
}
# 戳一戳类型编号 → 标签（只映射社区确认的四个，未知显示原始编号）
POKE_TYPE_LABELS = {
    "126": "点赞",
    "666": "比心",
    "2011": "抱抱",
    "2009": "亲亲",
}
# 参与归档的 kind：media 条目 id 为 32 位 hex 归档 id；其余 kind 的 id 语义不同
_ARCHIVE_KIND_SET = {"image", "video", "voice", "file"}
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

_PAIRED_CTE = f"""
WITH paired(user_record_id, assistant_record_id) AS (
    SELECT user_record.id, assistant_record.id
    FROM {TABLE_NAME} AS user_record
    JOIN {TABLE_NAME} AS assistant_record
      ON assistant_record.turn_id = user_record.turn_id
     AND assistant_record.role = 'assistant'
     AND assistant_record.umo = user_record.umo
     AND assistant_record.conversation_id = user_record.conversation_id
    WHERE user_record.role = 'user'
      AND user_record.turn_id IS NOT NULL
      AND user_record.turn_id <> ''

    UNION ALL

    SELECT user_record.id, MIN(assistant_record.id)
    FROM {TABLE_NAME} AS user_record
    JOIN {TABLE_NAME} AS assistant_record __LEGACY_PAIR_INDEX__
      ON assistant_record.pair_id = user_record.message_id
     AND assistant_record.role = 'assistant'
     AND assistant_record.umo = user_record.umo
     AND assistant_record.conversation_id = user_record.conversation_id
    WHERE user_record.role = 'user'
      AND (user_record.turn_id IS NULL OR user_record.turn_id = '')
      AND user_record.message_id IS NOT NULL
      AND user_record.message_id <> ''
    GROUP BY user_record.id
), matched(record_id) AS (
    SELECT user_record_id FROM paired
    UNION ALL
    SELECT assistant_record_id FROM paired
)
"""

T = TypeVar("T")


class DatabaseUnavailableError(RuntimeError):
    """A safe-to-display database access error."""


@dataclass(frozen=True)
class DatabaseAccessInfo:
    path: str
    mode: str
    warning: str
    user_version: int
    journal_mode: str
    main_size: int
    wal_present: bool
    wal_size: int
    shm_present: bool
    modified_at: str
    tool_table_present: bool
    media_table_present: bool


class ChatMemoryRepository:
    """Parameterized, read-only queries against a ChatMemory SQLite database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        timezone: tzinfo | None = None,
        default_page_size: int = 50,
        max_page_size: int = 200,
        allow_immutable_fallback: bool = True,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        self.timezone = timezone or _default_timezone()
        self.default_page_size = max(10, min(200, int(default_page_size)))
        self.max_page_size = max(20, min(500, int(max_page_size)))
        self.default_page_size = min(self.default_page_size, self.max_page_size)
        self.allow_immutable_fallback = allow_immutable_fallback

    async def health(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_read, self._health_query)

    async def overview(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_read, self._overview_query)

    async def facets(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = self._normalize_filters(filters or {}, allow_query_fields=True)
        return await asyncio.to_thread(self._run_read, self._facets_query, normalized)

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_filters(payload, allow_query_fields=True)
        return await asyncio.to_thread(self._run_read, self._records_query, normalized)

    async def record(self, record_id: int) -> dict[str, Any]:
        try:
            normalized_id = int(record_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("记录 ID 必须是整数") from exc
        if normalized_id <= 0:
            raise ValueError("记录 ID 必须大于 0")
        return await asyncio.to_thread(self._run_read, self._record_query, normalized_id)

    async def tool_records(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_tool_filters(payload)
        return await asyncio.to_thread(self._run_read, self._tool_records_query, normalized)

    def _run_read(self, operation: Callable[..., T], *args: Any) -> T:
        if not self.db_path.is_file():
            raise DatabaseUnavailableError(f"未找到 ChatMemory 数据库：{self.db_path}")

        try:
            with closing(self._connect(immutable=False)) as connection:
                self._validate_schema(connection)
                payload = operation(connection, *args)
                access = self._access_info(connection, immutable=False)
        except sqlite3.DatabaseError as exc:
            if not self.allow_immutable_fallback or not _is_corruption_error(exc):
                raise DatabaseUnavailableError(f"ChatMemory 数据库不可读：{exc}") from exc
            try:
                with closing(self._connect(immutable=True)) as connection:
                    self._validate_schema(connection)
                    payload = operation(connection, *args)
                    access = self._access_info(connection, immutable=True)
            except sqlite3.DatabaseError as fallback_exc:
                raise DatabaseUnavailableError(
                    f"ChatMemory 主库快照也不可读：{fallback_exc}"
                ) from fallback_exc

        if isinstance(payload, dict):
            result = dict(payload)
            result["database"] = asdict(access)
            return result  # type: ignore[return-value]
        return payload

    def _connect(self, *, immutable: bool) -> sqlite3.Connection:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        connection = sqlite3.connect(
            f"{self.db_path.as_uri()}?{query}",
            uri=True,
            timeout=3,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 3000")
        return connection

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TABLE_NAME,),
        ).fetchone()
        if table is None:
            raise DatabaseUnavailableError("数据库中不存在 chat_memory_records 表")
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{TABLE_NAME}")')
        }
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise DatabaseUnavailableError(
                "ChatMemory 数据库缺少必要字段：" + ", ".join(missing)
            )

    def _access_info(
        self, connection: sqlite3.Connection, *, immutable: bool
    ) -> DatabaseAccessInfo:
        main_stat = self.db_path.stat()
        wal_path = Path(str(self.db_path) + "-wal")
        shm_path = Path(str(self.db_path) + "-shm")
        warning = ""
        if immutable:
            warning = (
                "检测到 WAL/SHM 与主库不一致；当前已忽略旁路文件，只读展示主库快照。"
                "页面可能缺少尚未 checkpoint 的最新记录，请停止 AstrBot 后使用 SQLite "
                "backup() API 重新迁移数据。"
            )
        try:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        except sqlite3.DatabaseError:
            journal_mode = "unknown"
        tool_table_present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (TOOL_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        media_table_present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (MEDIA_ARCHIVE_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        return DatabaseAccessInfo(
            path=str(self.db_path),
            mode="main_snapshot" if immutable else "wal_aware",
            warning=warning,
            user_version=int(connection.execute("PRAGMA user_version").fetchone()[0]),
            journal_mode=journal_mode,
            main_size=int(main_stat.st_size),
            wal_present=wal_path.is_file(),
            wal_size=int(wal_path.stat().st_size) if wal_path.is_file() else 0,
            shm_present=shm_path.is_file(),
            modified_at=datetime.fromtimestamp(
                main_stat.st_mtime, tz=dt_timezone.utc
            ).isoformat(),
            tool_table_present=tool_table_present,
            media_table_present=media_table_present,
        )

    def _health_query(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            f"SELECT COUNT(*) AS total, MIN(created_at) AS oldest, "
            f"MAX(created_at) AS newest FROM {TABLE_NAME}"
        ).fetchone()
        return {
            "healthy": True,
            "total_records": int(row["total"] or 0),
            "oldest_at": self._local_time(row["oldest"]),
            "newest_at": self._local_time(row["newest"]),
        }

    def _overview_query(self, connection: sqlite3.Connection) -> dict[str, Any]:
        summary = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_records,
                COUNT(DISTINCT umo) AS total_umos,
                COUNT(DISTINCT conversation_id) AS total_conversations,
                COUNT(DISTINCT CASE WHEN role = 'user' THEN user_id END) AS total_users,
                MIN(created_at) AS oldest_at,
                MAX(created_at) AS newest_at
            FROM {TABLE_NAME}
            """
        ).fetchone()
        roles = _count_map(
            connection.execute(
                f"SELECT role AS value, COUNT(*) AS amount FROM {TABLE_NAME} "
                "GROUP BY role ORDER BY amount DESC"
            )
        )
        statuses = _count_map(
            connection.execute(
                f"SELECT llm_status AS value, COUNT(*) AS amount FROM {TABLE_NAME} "
                "GROUP BY llm_status ORDER BY amount DESC"
            ),
            empty_label="no_llm",
        )
        kinds = _count_map(
            connection.execute(
                f"""
                SELECT kinds.value AS value, COUNT(*) AS amount
                FROM {TABLE_NAME},
                     json_each(CASE WHEN json_valid(content_kind)
                                    THEN content_kind ELSE '[]' END) AS kinds
                GROUP BY kinds.value
                ORDER BY amount DESC
                """
            )
        )
        daily_rows = connection.execute(
            f"""
            SELECT date(created_at) AS day, COUNT(*) AS amount
            FROM {TABLE_NAME}
            WHERE created_at >= datetime('now', '-29 days')
            GROUP BY date(created_at)
            ORDER BY day ASC
            """
        ).fetchall()
        tools = self._tools_overview(connection)
        media = self._media_overview(connection)
        return {
            "summary": {
                "total_records": int(summary["total_records"] or 0),
                "total_umos": int(summary["total_umos"] or 0),
                "total_conversations": int(summary["total_conversations"] or 0),
                "total_users": int(summary["total_users"] or 0),
                "oldest_at": self._local_time(summary["oldest_at"]),
                "newest_at": self._local_time(summary["newest_at"]),
                "timezone": str(self.timezone),
            },
            "roles": roles,
            "statuses": statuses,
            "kinds": kinds,
            "daily": [
                {"day": str(row["day"]), "count": int(row["amount"])}
                for row in daily_rows
            ],
            "tools": tools,
            "media": media,
        }

    @staticmethod
    def _media_overview(connection: sqlite3.Connection) -> dict[str, Any]:
        present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (MEDIA_ARCHIVE_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        if not present:
            return {"present": False, "total_files": 0, "total_bytes": 0}
        row = connection.execute(
            f"SELECT COUNT(*) AS total_files, "
            f"COALESCE(SUM(size_bytes), 0) AS total_bytes "
            f"FROM {MEDIA_ARCHIVE_TABLE_NAME}"
        ).fetchone()
        return {
            "present": True,
            "total_files": int(row["total_files"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
        }

    @staticmethod
    def _tools_overview(connection: sqlite3.Connection) -> dict[str, Any]:
        present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (TOOL_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        if not present:
            return {"present": False, "total_calls": 0, "tool_names": {}}
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {TOOL_TABLE_NAME}"
            ).fetchone()[0]
            or 0
        )
        names = _count_map(
            connection.execute(
                f"SELECT tool_name AS value, COUNT(*) AS amount "
                f"FROM {TOOL_TABLE_NAME} GROUP BY tool_name ORDER BY amount DESC"
            ),
            empty_label="(empty)",
        )
        return {"present": True, "total_calls": total, "tool_names": names}

    def _facets_query(
        self, connection: sqlite3.Connection, filters: dict[str, Any]
    ) -> dict[str, Any]:
        def scope(excluded: str) -> tuple[str, dict[str, Any]]:
            scoped_filters = {
                key: value for key, value in filters.items() if key != excluded
            }
            conditions, scoped_params = self._where_clause(
                scoped_filters, alias="r"
            )
            return (" AND ".join(conditions) if conditions else "1 = 1"), scoped_params

        umo_where, umo_params = scope("umo")
        umo_rows = connection.execute(
            f"""
            SELECT r.umo, MAX(r.created_at) AS latest_at, COUNT(*) AS amount,
                   MAX(r.platform_name) AS platform_name,
                   MAX(r.message_type) AS message_type,
                   MAX(r.session_id) AS session_id,
                   MAX(r.group_id) AS group_id
            FROM {TABLE_NAME} AS r
            WHERE {umo_where}
            GROUP BY r.umo
            ORDER BY latest_at DESC
            LIMIT 300
            """,
            umo_params,
        ).fetchall()

        conversation_where, conversation_params = scope("conversation_id")
        conversation_rows = connection.execute(
            f"""
            SELECT r.conversation_id, MAX(r.created_at) AS latest_at,
                   COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {conversation_where}
            GROUP BY r.conversation_id
            ORDER BY latest_at DESC
            LIMIT 300
            """,
            conversation_params,
        ).fetchall()

        user_where, user_params = scope("user_id")
        user_rows = connection.execute(
            f"""
            WITH ranked_users AS (
                SELECT r.user_id, r.sender_nickname AS nickname,
                       MAX(r.created_at) OVER (
                           PARTITION BY r.user_id
                       ) AS latest_at,
                       COUNT(*) OVER (
                           PARTITION BY r.user_id
                       ) AS amount,
                       ROW_NUMBER() OVER (
                           PARTITION BY r.user_id
                           ORDER BY
                               CASE
                                   WHEN r.sender_nickname IS NULL
                                     OR TRIM(r.sender_nickname) = ''
                                   THEN 1 ELSE 0
                               END,
                               r.created_at DESC,
                               r.id DESC
                       ) AS nickname_rank
                FROM {TABLE_NAME} AS r
                WHERE {user_where}
            )
            SELECT user_id, nickname, latest_at, amount
            FROM ranked_users
            WHERE nickname_rank = 1
            ORDER BY latest_at DESC
            LIMIT 300
            """,
            user_params,
        ).fetchall()

        persona_where, persona_params = scope("persona_id")
        persona_rows = connection.execute(
            f"""
            SELECT COALESCE(r.persona_id, '') AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {persona_where}
            GROUP BY COALESCE(r.persona_id, '')
            ORDER BY amount DESC
            LIMIT 200
            """,
            persona_params,
        ).fetchall()

        platform_where, platform_params = scope("platform_name")
        platform_rows = connection.execute(
            f"""
            SELECT COALESCE(r.platform_name, '') AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {platform_where}
            GROUP BY COALESCE(r.platform_name, '')
            ORDER BY amount DESC
            """,
            platform_params,
        ).fetchall()

        message_type_where, message_type_params = scope("message_type")
        message_type_rows = connection.execute(
            f"""
            SELECT COALESCE(r.message_type, '') AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {message_type_where}
            GROUP BY COALESCE(r.message_type, '')
            ORDER BY amount DESC
            """,
            message_type_params,
        ).fetchall()

        role_where, role_params = scope("role")
        role_rows = connection.execute(
            f"""
            SELECT r.role AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {role_where}
            GROUP BY r.role
            ORDER BY amount DESC
            """,
            role_params,
        ).fetchall()

        status_where, status_params = scope("llm_status")
        status_rows = connection.execute(
            f"""
            SELECT COALESCE(r.llm_status, '') AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            WHERE {status_where}
            GROUP BY COALESCE(r.llm_status, '')
            ORDER BY amount DESC
            """,
            status_params,
        ).fetchall()

        kind_where, kind_params = scope("content_kind")
        kind_rows = connection.execute(
            f"""
            SELECT kinds.value AS value, COUNT(*) AS amount
            FROM {TABLE_NAME} AS r
            JOIN json_each(
                CASE WHEN json_valid(r.content_kind)
                     THEN r.content_kind ELSE '[]' END
            ) AS kinds
            WHERE {kind_where}
            GROUP BY kinds.value
            ORDER BY amount DESC
            """,
            kind_params,
        ).fetchall()
        return {
            "umos": [
                {
                    "value": str(row["umo"]),
                    "count": int(row["amount"]),
                    "latest_at": self._local_time(row["latest_at"]),
                    "platform_name": str(row["platform_name"] or ""),
                    "message_type": str(row["message_type"] or ""),
                    "session_id": str(row["session_id"] or ""),
                    "group_id": str(row["group_id"] or ""),
                }
                for row in umo_rows
            ],
            "conversations": [
                {
                    "value": str(row["conversation_id"]),
                    "count": int(row["amount"]),
                    "latest_at": self._local_time(row["latest_at"]),
                }
                for row in conversation_rows
            ],
            "users": [
                {
                    "value": str(row["user_id"]),
                    "nickname": str(row["nickname"] or ""),
                    "count": int(row["amount"]),
                    "latest_at": self._local_time(row["latest_at"]),
                }
                for row in user_rows
            ],
            "personas": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in persona_rows
            ],
            "platforms": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in platform_rows
                if row["value"]
            ],
            "message_types": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in message_type_rows
                if row["value"]
            ],
            "roles": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in role_rows
                if row["value"] in VALID_ROLES
            ],
            "statuses": [
                {"value": str(row["value"] or ""), "count": int(row["amount"])}
                for row in status_rows
                if str(row["value"] or "") in VALID_STATUSES
            ],
            "kinds": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in kind_rows
                if row["value"] in VALID_KINDS
            ],
        }

    def _records_query(
        self, connection: sqlite3.Connection, filters: dict[str, Any]
    ) -> dict[str, Any]:
        conditions, params = self._where_clause(filters, alias="r")
        where = " AND ".join(conditions) if conditions else "1 = 1"
        cte = self._paired_cte(connection) if filters.get("paired_only") else ""
        matched_join = (
            "JOIN matched ON matched.record_id = r.id"
            if filters.get("paired_only")
            else ""
        )
        total = int(
            connection.execute(
                f"{cte} SELECT COUNT(*) FROM {TABLE_NAME} AS r "
                f"{matched_join} WHERE {where}",
                params,
            ).fetchone()[0]
        )
        page = filters["page"]
        page_size = filters["page_size"]
        offset = (page - 1) * page_size
        rows = connection.execute(
            f"""
            {cte}
            SELECT r.*
            FROM {TABLE_NAME} AS r
            {matched_join}
            WHERE {where}
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT :page_size OFFSET :offset
            """,
            {**params, "page_size": page_size, "offset": offset},
        ).fetchall()
        reply_targets = self._fetch_reply_targets(connection, rows)
        media_archive = self._fetch_media_archive(connection, rows)
        return {
            "items": [
                self._serialize_record(
                    row,
                    preview_limit=4000,
                    reply_targets=reply_targets,
                    media_archive=media_archive,
                )
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": (total + page_size - 1) // page_size if total else 0,
            "filters": filters,
        }

    def _fetch_media_archive(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> dict[str, dict[str, Any]]:
        """批量读取 media 条目的归档行（media_id → 归档信息），避免 N+1。

        只查 32 位 hex 的归档 id（emoji/poke/forward 的 id 语义不同，不可能是 hex32）。
        """
        wanted: set[str] = set()
        for row in rows:
            relation = _parse_relation_data(_row_value(row, "relation_data"))
            media = relation.get("media") if isinstance(relation, dict) else None
            if not isinstance(media, list):
                continue
            for item in media:
                if not isinstance(item, dict):
                    continue
                if str(item.get("kind") or "") not in _ARCHIVE_KIND_SET:
                    continue
                media_id = str(item.get("id") or "")
                if _HEX32_RE.fullmatch(media_id):
                    wanted.add(media_id)
        if not wanted:
            return {}
        present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (MEDIA_ARCHIVE_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        if not present:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for chunk_start in range(0, len(wanted), 400):
            chunk = sorted(wanted)[chunk_start : chunk_start + 400]
            placeholders = ", ".join("?" * len(chunk))
            try:
                archive_rows = connection.execute(
                    f"SELECT media_id, kind, file_name, ext, mime_type, "
                    f"size_bytes, created_at FROM {MEDIA_ARCHIVE_TABLE_NAME} "
                    f"WHERE media_id IN ({placeholders})",
                    chunk,
                ).fetchall()
            except sqlite3.DatabaseError:
                archive_rows = []
            for archive_row in archive_rows:
                result[str(archive_row["media_id"])] = {
                    "media_id": str(archive_row["media_id"]),
                    "kind": str(archive_row["kind"] or ""),
                    "file_name": str(archive_row["file_name"] or ""),
                    "ext": str(archive_row["ext"] or ""),
                    "mime_type": str(archive_row["mime_type"] or ""),
                    "size_bytes": int(archive_row["size_bytes"] or 0),
                    "created_at": str(archive_row["created_at"] or ""),
                }
        return result

    async def media_file(self, media_id: str) -> dict[str, Any]:
        """按 media_id 读取归档行（媒体 API 用）；不存在抛 ValueError。"""
        normalized = str(media_id or "").strip()
        if not _HEX32_RE.fullmatch(normalized):
            raise ValueError("媒体 ID 格式不正确")

        def _query(connection: sqlite3.Connection) -> dict[str, Any]:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (MEDIA_ARCHIVE_TABLE_NAME,),
            ).fetchone()
            if table_exists is None:
                raise ValueError("媒体未归档或已被清理")
            row = connection.execute(
                f"SELECT media_id, kind, file_name, ext, mime_type, "
                f"size_bytes, created_at FROM {MEDIA_ARCHIVE_TABLE_NAME} "
                "WHERE media_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise ValueError("媒体未归档或已被清理")
            return {
                "media_id": str(row["media_id"]),
                "kind": str(row["kind"] or ""),
                "file_name": str(row["file_name"] or ""),
                "ext": str(row["ext"] or ""),
                "mime_type": str(row["mime_type"] or ""),
                "size_bytes": int(row["size_bytes"] or 0),
                "created_at": str(row["created_at"] or ""),
            }

        return await asyncio.to_thread(self._run_read, _query)

    def _fetch_reply_targets(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """批量读取 turn 解析 Reply 的目标记录（同 umo + turn_id 的 user 行）。

        避免逐条查询目标的 N+1；目标内容用于展示"回复了谁、引用了什么"。
        """
        needed: dict[str, set[str]] = {}
        for row in rows:
            relation = _parse_relation_data(_row_value(row, "relation_data"))
            reply = relation.get("reply") if isinstance(relation, dict) else None
            if isinstance(reply, dict) and reply.get("resolution") == "turn":
                turn_id = str(reply.get("target_turn_id") or "").strip()
                if turn_id:
                    needed.setdefault(str(row["umo"]), set()).add(turn_id)
        targets: dict[tuple[str, str], dict[str, Any]] = {}
        for umo, turn_ids in needed.items():
            placeholders = ", ".join("?" * len(turn_ids))
            try:
                target_rows = connection.execute(
                    f"SELECT * FROM {TABLE_NAME} "
                    f"WHERE umo = ? AND role = 'user' AND turn_id IN ({placeholders}) "
                    "ORDER BY created_at ASC, id ASC",
                    (umo, *sorted(turn_ids)),
                ).fetchall()
            except sqlite3.DatabaseError:
                target_rows = []
            for target in target_rows:
                raw = str(target["content"] or "")
                rel = _parse_relation_data(_row_value(target, "relation_data"))
                kinds = _parse_content_kind(_row_value(target, "content_kind"))
                targets[(umo, str(target["turn_id"]))] = {
                    "sender_nickname": str(target["sender_nickname"] or ""),
                    "content": _render_content(raw, rel, kinds),
                }
        return targets

    @staticmethod
    def _paired_cte(connection: sqlite3.Connection) -> str:
        indexes = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA index_list("{TABLE_NAME}")')
        }
        hint = "INDEXED BY ix_cm_pair_id" if "ix_cm_pair_id" in indexes else ""
        return _PAIRED_CTE.replace("__LEGACY_PAIR_INDEX__", hint)

    def _record_query(
        self, connection: sqlite3.Connection, record_id: int
    ) -> dict[str, Any]:
        row = connection.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"记录 #{record_id} 不存在")
        reply_targets = self._fetch_reply_targets(connection, [row])
        media_archive = self._fetch_media_archive(connection, [row])
        return {
            "record": self._serialize_record(
                row,
                preview_limit=200_000,
                reply_targets=reply_targets,
                media_archive=media_archive,
            )
        }

    def _tool_records_query(
        self, connection: sqlite3.Connection, filters: dict[str, Any]
    ) -> dict[str, Any]:
        present = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (TOOL_TABLE_NAME,),
            ).fetchone()
            is not None
        )
        if not present:
            return {
                "items": [],
                "total": 0,
                "page": filters["page"],
                "page_size": filters["page_size"],
                "page_count": 0,
                "filters": filters,
            }
        conditions: list[str] = []
        params: dict[str, Any] = {}
        for key, column in (
            ("umo", "umo"),
            ("conversation_id", "conversation_id"),
            ("turn_id", "turn_id"),
        ):
            value = filters.get(key)
            if value:
                conditions.append(f"{column} = :{key}")
                params[key] = value
        if filters.get("tool_name"):
            conditions.append("tool_name LIKE :tool_name ESCAPE '\\'")
            params["tool_name"] = f"%{_escape_like(filters['tool_name'])}%"
        if filters.get("since"):
            conditions.append("created_at >= :since")
            params["since"] = filters["since"]
        if filters.get("until"):
            conditions.append("created_at <= :until")
            params["until"] = filters["until"]
        where = " AND ".join(conditions) if conditions else "1 = 1"
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {TOOL_TABLE_NAME} WHERE {where}",
                params,
            ).fetchone()[0]
        )
        page = filters["page"]
        page_size = filters["page_size"]
        offset = (page - 1) * page_size
        rows = connection.execute(
            f"""
            SELECT * FROM {TOOL_TABLE_NAME}
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT :page_size OFFSET :offset
            """,
            {**params, "page_size": page_size, "offset": offset},
        ).fetchall()
        return {
            "items": [self._serialize_tool_record(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": (total + page_size - 1) // page_size if total else 0,
            "filters": filters,
        }

    def _serialize_tool_record(self, row: sqlite3.Row) -> dict[str, Any]:
        created_at_utc, created_at_local = self._time_pair(row["created_at"])
        return {
            "tool_id": int(row["id"]),
            "umo": str(row["umo"]),
            "conversation_id": str(row["conversation_id"]),
            "turn_id": str(row["turn_id"] or ""),
            "call_index": int(row["call_index"]),
            "tool_name": str(row["tool_name"] or ""),
            "tool_args": str(row["tool_args"] or ""),
            "tool_result": str(row["tool_result"] or ""),
            "created_at": created_at_local,
            "created_at_utc": created_at_utc,
        }

    def _normalize_tool_filters(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("查询参数必须是对象")
        normalized: dict[str, Any] = {}
        for key, limit in (
            ("umo", 1024),
            ("conversation_id", 256),
            ("turn_id", 256),
        ):
            value = str(raw.get(key, "") or "").strip()
            if len(value) > limit:
                raise ValueError(f"{key} 过长")
            normalized[key] = value
        tool_name = str(raw.get("tool_name", "") or "").strip()
        if len(tool_name) > 200:
            raise ValueError("工具名不能超过 200 个字符")
        normalized["tool_name"] = tool_name
        normalized["since"] = self._normalize_local_datetime(raw.get("since"), "起始时间")
        normalized["until"] = self._normalize_local_datetime(raw.get("until"), "结束时间")
        if normalized["since"] and normalized["until"]:
            if normalized["since"] > normalized["until"]:
                raise ValueError("起始时间不能晚于结束时间")
        normalized["page"] = _bounded_int(raw.get("page", 1), 1, 1_000_000, 1)
        normalized["page_size"] = _bounded_int(
            raw.get("page_size", self.default_page_size),
            1,
            self.max_page_size,
            self.default_page_size,
        )
        return normalized

    def _where_clause(
        self, filters: dict[str, Any], *, alias: str
    ) -> tuple[list[str], dict[str, Any]]:
        conditions: list[str] = []
        params: dict[str, Any] = {}
        column_filters = {
            "umo": "umo",
            "conversation_id": "conversation_id",
            "user_id": "user_id",
            "role": "role",
            "platform_name": "platform_name",
            "message_type": "message_type",
        }
        for key, column in column_filters.items():
            value = filters.get(key)
            if value:
                conditions.append(f"{alias}.{column} = :{key}")
                params[key] = value

        if filters.get("llm_status") is not None:
            conditions.append(f"{alias}.llm_status = :llm_status")
            params["llm_status"] = filters["llm_status"]
        if filters.get("persona_id") is not None:
            if filters["persona_id"]:
                conditions.append(f"{alias}.persona_id = :persona_id")
                params["persona_id"] = filters["persona_id"]
            else:
                conditions.append(
                    f"({alias}.persona_id IS NULL OR {alias}.persona_id = '')"
                )
        if filters.get("content_kind"):
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each("
                f"CASE WHEN json_valid({alias}.content_kind) "
                f"THEN {alias}.content_kind ELSE '[]' END) WHERE value = :content_kind)"
            )
            params["content_kind"] = filters["content_kind"]
        if filters.get("keyword"):
            conditions.append(
                "("
                f"{alias}.content LIKE :keyword ESCAPE '\\' OR "
                f"COALESCE({alias}.relation_data, '') LIKE :keyword ESCAPE '\\' OR "
                f"COALESCE({alias}.sender_nickname, '') LIKE :keyword ESCAPE '\\' OR "
                f"{alias}.user_id LIKE :keyword ESCAPE '\\' OR "
                f"COALESCE({alias}.message_id, '') LIKE :keyword ESCAPE '\\' OR "
                f"COALESCE({alias}.turn_id, '') LIKE :keyword ESCAPE '\\'"
                ")"
            )
            params["keyword"] = f"%{_escape_like(filters['keyword'])}%"
        if filters.get("since"):
            conditions.append(f"{alias}.created_at >= :since")
            params["since"] = filters["since"]
        if filters.get("until"):
            conditions.append(f"{alias}.created_at <= :until")
            params["until"] = filters["until"]
        return conditions, params

    def _normalize_filters(
        self, raw: dict[str, Any], *, allow_query_fields: bool
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("查询参数必须是对象")
        normalized: dict[str, Any] = {}
        length_limits = {
            "umo": 1024,
            "conversation_id": 256,
            "user_id": 256,
            "platform_name": 128,
            "message_type": 128,
        }
        for key, limit in length_limits.items():
            value = str(raw.get(key, "") or "").strip()
            if len(value) > limit:
                raise ValueError(f"{key} 过长")
            normalized[key] = value

        if not allow_query_fields:
            return normalized

        keyword = str(raw.get("keyword", "") or "").strip()
        if len(keyword) > 200:
            raise ValueError("关键词不能超过 200 个字符")
        normalized["keyword"] = keyword

        role = str(raw.get("role", "") or "").strip()
        if role and role not in VALID_ROLES:
            raise ValueError("未知 role")
        normalized["role"] = role

        status_raw = raw.get("llm_status", None)
        if status_raw in (None, "__all__"):
            normalized["llm_status"] = None
        else:
            status = "" if status_raw in ("__empty__", "no_llm") else str(status_raw)
            if status not in VALID_STATUSES:
                raise ValueError("未知 llm_status")
            normalized["llm_status"] = status

        kind = str(raw.get("content_kind", "") or "").strip()
        if kind and kind not in VALID_KINDS:
            raise ValueError("未知 content_kind")
        normalized["content_kind"] = kind

        persona_raw = raw.get("persona_id", None)
        if persona_raw in (None, "__all__"):
            normalized["persona_id"] = None
        elif persona_raw == "__empty__":
            normalized["persona_id"] = ""
        else:
            persona = str(persona_raw or "").strip()
            if len(persona) > 256:
                raise ValueError("persona_id 过长")
            normalized["persona_id"] = persona

        normalized["since"] = self._normalize_local_datetime(raw.get("since"), "起始时间")
        normalized["until"] = self._normalize_local_datetime(raw.get("until"), "结束时间")
        if normalized["since"] and normalized["until"]:
            if normalized["since"] > normalized["until"]:
                raise ValueError("起始时间不能晚于结束时间")
        normalized["paired_only"] = bool(raw.get("paired_only", False))
        normalized["page"] = _bounded_int(raw.get("page", 1), 1, 1_000_000, 1)
        normalized["page_size"] = _bounded_int(
            raw.get("page_size", self.default_page_size),
            1,
            self.max_page_size,
            self.default_page_size,
        )
        return normalized

    def _normalize_local_datetime(self, value: Any, label: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if len(raw) > 40:
            raise ValueError(f"{label}格式错误")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label}格式错误") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return (
            parsed.astimezone(dt_timezone.utc)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S")
        )

    def _serialize_record(
        self,
        row: sqlite3.Row,
        *,
        preview_limit: int,
        reply_targets: dict[tuple[str, str], dict[str, Any]] | None = None,
        media_archive: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raw_content = str(row["content"] or "")
        relation_data = _parse_relation_data(_row_value(row, "relation_data"))
        content_kind = _parse_content_kind(row["content_kind"])
        rendered_content = _render_content(raw_content, relation_data, content_kind)
        truncated = len(rendered_content) > preview_limit
        if truncated:
            rendered_content = rendered_content[:preview_limit].rstrip() + "…"
        created_at_utc, created_at_local = self._time_pair(row["created_at"])
        reply_view = None
        if isinstance(relation_data, dict):
            reply = relation_data.get("reply")
            if isinstance(reply, dict):
                if reply.get("resolution") == "turn":
                    key = (str(row["umo"]), str(reply.get("target_turn_id") or ""))
                    target = (reply_targets or {}).get(key)
                    reply_view = {
                        "resolution": "turn",
                        "target": str(
                            (target or {}).get("sender_nickname") or ""
                        ).strip()
                        or "未知成员",
                        "text": str((target or {}).get("content") or "")[:300],
                    }
                else:
                    reply_view = {
                        "resolution": "snapshot",
                        "target": str(reply.get("target_nickname") or "").strip()
                        or "未知成员",
                        "text": str(reply.get("fallback_text") or "")[:300],
                    }
        media_view = self._media_view(relation_data, media_archive)
        return {
            "record_id": int(row["id"]),
            "role": str(row["role"]),
            "content": rendered_content,
            "content_template": raw_content,
            "content_truncated": truncated,
            "umo": str(row["umo"]),
            "conversation_id": str(row["conversation_id"]),
            "user_id": str(row["user_id"]),
            "message_id": _optional_str(_row_value(row, "message_id")),
            "pair_id": _optional_str(_row_value(row, "pair_id")),
            "turn_id": _optional_str(_row_value(row, "turn_id")),
            "llm_status": str(row["llm_status"] or ""),
            "content_kind": content_kind,
            "platform_id": _optional_str(_row_value(row, "platform_id")),
            "platform_name": _optional_str(_row_value(row, "platform_name")),
            "message_type": _optional_str(_row_value(row, "message_type")),
            "session_id": _optional_str(_row_value(row, "session_id")),
            "self_id": _optional_str(_row_value(row, "self_id")),
            "group_id": _optional_str(_row_value(row, "group_id")),
            "sender_nickname": _optional_str(_row_value(row, "sender_nickname")),
            "raw_timestamp": _row_value(row, "raw_timestamp"),
            "at_id": _optional_str(_row_value(row, "at_id")),
            "reply_id": _optional_str(_row_value(row, "reply_id")),
            "forward_id": _optional_str(_row_value(row, "forward_id")),
            "persona_id": _optional_str(_row_value(row, "persona_id")),
            "send_status": _optional_str(_row_value(row, "send_status")),
            "relation_data": relation_data,
            "reply_view": reply_view,
            "media_view": media_view,
            "created_at": created_at_local,
            "created_at_utc": created_at_utc,
        }

    @staticmethod
    def _media_view(
        relation_data: dict[str, Any] | None,
        media_archive: dict[str, dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """把 media 条目渲染成前端可直接展示的列表（含归档状态）。"""
        media = (
            relation_data.get("media")
            if isinstance(relation_data, dict)
            else None
        )
        if not isinstance(media, list):
            return []
        view: list[dict[str, Any]] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            media_id = str(item.get("id") or "")
            entry: dict[str, Any] = {
                "kind": kind,
                "id": media_id or None,
                "label": MEDIA_KIND_LABELS.get(kind, "⟦媒体⟧"),
                "name": str(item.get("name") or ""),
                "poke_type": str(item.get("type") or ""),
                "poke_label": POKE_TYPE_LABELS.get(str(item.get("type") or ""), ""),
                "archived": False,
            }
            archive_row = (media_archive or {}).get(media_id)
            if isinstance(archive_row, dict):
                entry["archived"] = True
                entry["media_id"] = media_id
                entry["mime"] = str(archive_row.get("mime_type") or "")
                entry["size"] = int(archive_row.get("size_bytes") or 0)
                entry["file"] = str(archive_row.get("file_name") or "")
                entry["created_at"] = str(archive_row.get("created_at") or "")
            view.append(entry)
        return view

    def _local_time(self, value: Any) -> str:
        return self._time_pair(value)[1]

    def _time_pair(self, value: Any) -> tuple[str, str]:
        if value is None or value == "":
            return "", ""
        raw = str(value)
        try:
            parsed = datetime.fromisoformat(raw.replace("T", " ").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_timezone.utc)
            else:
                parsed = parsed.astimezone(dt_timezone.utc)
            utc_value = parsed.isoformat().replace("+00:00", "Z")
            local_value = parsed.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            return utc_value, local_value
        except (TypeError, ValueError):
            return raw, raw


def _is_corruption_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "malformed" in message or "corrupt" in message or "disk image" in message


def _count_map(
    rows: Any, *, empty_label: str = "(empty)"
) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        value = str(row["value"] or empty_label)
        result[value] = int(row["amount"])
    return result


def _parse_content_kind(value: Any) -> list[str]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _parse_relation_data(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, dict):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, dict) or parsed.get("v") != 1:
        return None
    mentions = parsed.get("mentions")
    if not isinstance(mentions, list):
        mentions = []
    media = parsed.get("media")
    if not isinstance(media, list):
        media = []
    reply = parsed.get("reply")
    if reply is not None and not isinstance(reply, dict):
        reply = None
    return {"v": 1, "mentions": mentions, "reply": reply, "media": media}


def _render_content(
    template: str,
    relation_data: dict[str, Any] | None,
    content_kind: list[str] | None = None,
) -> str:
    kinds = set(content_kind or [])
    if not relation_data:
        value = template.replace(_SENTINEL_LEFT, "⟦").replace(_SENTINEL_RIGHT, "⟧")
        return _localize_pure_media_placeholder(value, kinds)
    mentions = relation_data.get("mentions") or []
    media = relation_data.get("media") or []

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(mentions) or not isinstance(mentions[index], dict):
            return "@未知成员"
        mention = mentions[index]
        if mention.get("all"):
            return "@全体成员"
        return "@" + str(mention.get("nickname") or "未知成员").strip()

    def replace_media(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(media) or not isinstance(media[index], dict):
            return "⟦媒体⟧"
        return MEDIA_KIND_LABELS.get(str(media[index].get("kind") or ""), "⟦媒体⟧")

    value = AT_TOKEN_RE.sub(replace, template)
    value = MEDIA_TOKEN_RE.sub(replace_media, value)
    value = value.replace(_SENTINEL_LEFT, "⟦").replace(_SENTINEL_RIGHT, "⟧")
    if not value.strip() and media:
        labels = [
            MEDIA_KIND_LABELS.get(str(item.get("kind") or ""), "")
            for item in media
            if isinstance(item, dict)
        ]
        value = " ".join(label for label in labels if label)
    # 仅当占位符对应的 kind 确实存在（media 条目或 content_kind）才本地化，
    # 避免把用户真的输入 "[image]" 四字误翻。
    media_kinds = {
        str(item.get("kind") or "")
        for item in media
        if isinstance(item, dict)
    }
    return _localize_pure_media_placeholder(value, media_kinds)


# 纯媒体正文存的英文占位（CM content_placeholder）：整串相等且对应 kind 存在时
# 本地化为中文标签，与混合消息 token 渲染出的 MEDIA_KIND_LABELS 保持一致。
_PURE_MEDIA_EN_PLACEHOLDERS = {
    "[image]": "⟦图片⟧",
    "[video]": "⟦视频⟧",
    "[voice]": "⟦语音⟧",
    "[file]": "⟦文件⟧",
    "[emoji]": "⟦表情⟧",
    "[forward]": "⟦转发消息⟧",
    "[poke]": "⟦戳一戳⟧",
}


def _localize_pure_media_placeholder(value: str, kinds: set[str]) -> str:
    stripped = value.strip()
    if stripped not in _PURE_MEDIA_EN_PLACEHOLDERS:
        return value
    kind = stripped[1:-1]  # "[image]" -> "image"
    if kind not in kinds:
        return value
    return _PURE_MEDIA_EN_PLACEHOLDERS[stripped]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _default_timezone() -> tzinfo:
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return dt_timezone(timedelta(hours=8), name="Asia/Shanghai")
