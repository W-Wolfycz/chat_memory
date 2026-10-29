"""Read-only SQLite access for ChatMemory (legacy user_version 8 / merged store v4).

The repository intentionally avoids AstrBot/core internals; the only cross-module
imports are pure-stdlib siblings shipped in the same plugin — ``..relation_codec``
(shared system-event sentence builder) and ``..qq_face`` (face names for rendering).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from ..relation_codec import build_event_sentence
from ..shared_store import SHADOW_READ_MIN_DAYS
from ..qq_face import face_name


TABLE_NAME = "chat_memory_records"
TOOL_TABLE_NAME = "chat_memory_tool_records"
MEDIA_ARCHIVE_TABLE_NAME = "chat_memory_media_archive"
# 合并存储（影子层）表：软删除要一起写过去，否则切读之后"删掉的记录"会重新出现
SHADOW_MESSAGE_TABLE = "chat_memory_messages"
SHADOW_TOOL_TABLE = "chat_memory_tool_calls"
# 轮次成员表：计划成员行（没有物理消息）只能标在这里
SHADOW_REPLY_TABLE = "chat_memory_reply_messages"
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
# 软删除筛选三态：all=全部 / visible=未删除（默认） / deleted=仅已删除
VALID_DELETED_STATES = {"all", "visible", "deleted"}
VALID_STATUSES = {"", "llm_pending", "llm_success", "proactive", "orphan"}
VALID_KINDS = {
    "text",
    "image",
    "video",
    "voice",
    "file",
    "emoji",
    "forward",
    "event",
    # 1.5.0 前落库的旧 kind 值：仅兼容筛选，新数据不再写入
    "system_event",
    # poke 既是旧值，也是当前值（消息链里带 Poke 组件时 content_kind 就是 ["poke"]）
    "poke",
}

# 事件子类（event.type）——查询台「事件子类」过滤的合法值
VALID_EVENT_TYPES = {
    "ban",
    "member",
    "title",
    "recall",
    "resource",
    "poke",
    "reaction",
    "system",
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

# 合并存储（schema 2）→ 旧表列的只读投影：**一条物理消息一行**（多实例共享同一条）。
# 查询台原本按 chat_memory_records 读——那条表按 (umo, cid) 存，三个 BOT 看同一个群
# 就有三行，于是同一条消息在页面上出现三次。这里改成：
#   ① 双写开始之后的时段读 chat_memory_messages（单例，一条一行）；
#   ② 双写之前的旧历史仍读 chat_memory_records（那时候没有合并存储），但按
#      (会话, 发送者, 正文, 平台时间) 去重——旧历史同样被多实例各存过一份。
# id 空间靠偏移区分（>= _MERGED_ID_OFFSET 的是合并存储行，写路径按这个判定目标表）。
_MERGED_ID_OFFSET = 1_000_000_000

_SHADOW_CTES = """
picked AS (
    SELECT b.*, ROW_NUMBER() OVER (
        PARTITION BY b.msg_uid
        ORDER BY (b.role = 'assistant') DESC,
                 (COALESCE(b.platform_message_id, '') <> '') DESC,
                 b.id ASC
    ) AS rn
    FROM chat_memory_message_bots AS b
), umos AS (
    SELECT msg_uid, GROUP_CONCAT(DISTINCT umo) AS umo_list
    FROM chat_memory_message_bots
    GROUP BY msg_uid
)
"""

_SHADOW_SELECT = """
SELECT
    m.rowid + {offset}                       AS id,
    COALESCE(b.umo, '')                       AS umo,
    COALESCE(u.umo_list, '')                  AS umo_list,
    COALESCE(b.conversation_id, '')           AS conversation_id,
    CASE WHEN COALESCE(b.role, '') <> '' THEN b.role
         WHEN m.sender_id = COALESCE(b.self_id, '') THEN 'assistant'
         ELSE 'user' END                      AS role,
    CASE WHEN COALESCE(b.role, '') = 'assistant'
         THEN COALESCE(b.target_id, '')
         ELSE m.sender_id END                 AS user_id,
    m.sender_id                               AS sender_id,
    m.msg_uid                                 AS msg_uid,
    m.content                                 AS content,
    m.content_kind                            AS content_kind,
    m.relation_data                           AS relation_data,
    m.created_at                              AS created_at,
    m.platform_timestamp                      AS platform_timestamp,
    m.platform_seq                            AS platform_seq,
    b.platform_message_id                     AS message_id,
    NULL                                      AS pair_id,
    b.turn_id                                 AS turn_id,
    COALESCE(b.llm_status, '')                AS llm_status,
    COALESCE(b.send_status, '')               AS send_status,
    b.persona_id                              AS persona_id,
    b.raw_timestamp                           AS raw_timestamp,
    m.recalled_at                             AS recalled_at,
    m.deleted_at                              AS deleted_at,
    m.platform_name                           AS platform_name,
    m.message_type                            AS message_type,
    m.session_id                              AS session_id,
    COALESCE(b.self_id, '')                   AS self_id,
    CASE WHEN m.message_type = 'GroupMessage' THEN m.session_id ELSE '' END AS group_id,
    m.sender_nickname                         AS sender_nickname,
    NULL                                      AS at_id,
    NULL                                      AS reply_id,
    NULL                                      AS forward_id
FROM chat_memory_messages AS m
LEFT JOIN picked AS b ON b.msg_uid = m.msg_uid AND b.rn = 1
LEFT JOIN umos AS u ON u.msg_uid = m.msg_uid
"""

# 旧历史（双写开始之前）按去重键折叠：多实例各存一份 → 只留一行，umo 列表供过滤匹配。
# 只带一个 MIN() 聚合，SQLite 保证其余裸列取自 id 最小的那一行（结果稳定可复核）。
_LEGACY_CACHE_NAME = "_cm_legacy_rows"
# 合并读源的物化副本：把「v2 投影 + 旧历史去重」那次 UNION 一次算好存进 TEMP 表。
# 那个 UNION 里带窗口函数（每实例挑一行），每次查询重算要 200~400ms；
# 物化一次 ~565ms，之后取一页 0.2ms、聚合十几到几十 ms。
_MERGED_CACHE_NAME = "_cm_merged_rows"
# 物化副本最短重建间隔：活跃写入时避免"每条消息都重建一次"（效果等同 2 秒快照）
_MERGED_REBUILD_MIN_SEC = 2.0

_LEGACY_CUT = (
    "(SELECT COALESCE((SELECT value FROM chat_memory_shadow_meta "
    "WHERE key = 'dual_write_started_at'), '9999-12-31'))"
)

_LEGACY_DEDUPE_SELECT = """
SELECT MIN(r.id)                          AS id,
       r.umo                              AS umo,
       GROUP_CONCAT(DISTINCT r.umo)        AS umo_list,
       r.conversation_id                   AS conversation_id,
       r.role                              AS role,
       r.user_id                           AS user_id,
       -- 旧表没有 sender_id：role=user 时 user_id 就是发送者，其余无从得知（留空）
       CASE WHEN r.role = 'user' THEN r.user_id END AS sender_id,
       NULL                                AS msg_uid,
       r.content                           AS content,
       r.content_kind                      AS content_kind,
       r.relation_data                     AS relation_data,
       r.created_at                        AS created_at,
       NULL                                AS platform_timestamp,
       NULL                                AS platform_seq,
       r.message_id                        AS message_id,
       r.pair_id                           AS pair_id,
       r.turn_id                           AS turn_id,
       r.llm_status                        AS llm_status,
       r.send_status                       AS send_status,
       r.persona_id                        AS persona_id,
       r.raw_timestamp                     AS raw_timestamp,
       r.recalled_at                       AS recalled_at,
       r.deleted_at                        AS deleted_at,
       r.platform_name                     AS platform_name,
       r.message_type                      AS message_type,
       r.session_id                        AS session_id,
       r.self_id                           AS self_id,
       CASE WHEN r.message_type = 'GroupMessage' THEN r.session_id ELSE '' END AS group_id,
       r.sender_nickname                   AS sender_nickname,
       r.at_id                             AS at_id,
       r.reply_id                          AS reply_id,
       r.forward_id                        AS forward_id
  FROM chat_memory_records AS r
 WHERE r.created_at < """ + _LEGACY_CUT + """
 GROUP BY r.message_type, r.session_id, COALESCE(r.user_id, ''), r.content,
          COALESCE(r.raw_timestamp, 0)
"""

_LEGACY_CTES = "legacy AS (" + _LEGACY_DEDUPE_SELECT + ")"

_SHADOW_TABLES = ("chat_memory_messages", "chat_memory_message_bots",
                  "chat_memory_shadow_meta")

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
        writable_path: str | Path | None = None,
        keep_open: bool = False,
        expected_shadow_version: str = "",
        shadow_read_enabled: bool = False,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        self.timezone = timezone or _default_timezone()
        self.default_page_size = max(10, min(200, int(default_page_size)))
        self.max_page_size = max(20, min(500, int(max_page_size)))
        self.default_page_size = min(self.default_page_size, self.max_page_size)
        self.allow_immutable_fallback = allow_immutable_fallback
        # 只读连接复用：合并存储的旧历史去重要落一张 TEMP 表（0.4s+），每次请求重建太贵。
        # 只给长期存活的查询台开（keep_open）；一次性/测试用实例仍是一请求一连接，
        # 否则临时库目录在 Windows 上删不掉（文件被持有）。
        self.keep_open = bool(keep_open)
        # keep_open 连接会把旧历史去重结果冻在 TEMP 表里（含 deleted_at），
        # 软删除/恢复后必须重建，否则页面永远读到删除前的快照。
        self._legacy_cache_dirty = False
        # 期望的合并存储结构版本（由写入侧常量传入）。空串＝不校验版本，只校验表结构。
        self._expected_shadow_version = str(expected_shadow_version or "")
        # 插件的切读开关/门槛结论（由 main 传入）：False 时查询台照旧读旧表，
        # 与上下文、公开 API 保持同一个读源。原来只判表结构+7 天，默认配置下
        # 会出现"模型读旧表、页面读合并存储"两套数据。
        self.read_source = "v2" if shadow_read_enabled else "v1"
        # 物化副本的两把钥匙：库指纹（含 WAL）+ 旧历史缓存代数
        self._legacy_generation = 0
        self._merged_key: tuple | None = None
        self._merged_ready = False
        self._merged_built_at = 0.0
        # 锁定快照：进 UI 时锁住当时的库状态，之后不再随写入重建（点刷新才重来）。
        # 这正是查询台的语义——"我正在看这一刻的数据"。
        self._snapshot_locked = False
        # 概览结果缓存：(库指纹, 取数时刻, 结果)。库或 WAL 一变指纹就变 → 立刻重算
        self._overview_cache: tuple[tuple, float, dict[str, Any]] | None = None
        self._cache_conn: sqlite3.Connection | None = None
        self._cache_lock = threading.Lock()
        # 唯一允许写入（恢复软删除）的库：插件实时库。查询台指向别处时只读。
        self.writable_path = (
            Path(writable_path).expanduser().resolve(strict=False)
            if writable_path
            else None
        )

    def snapshot(self) -> dict[str, Any]:
        """锁定当前库状态：重建物化副本与概览，之后不再随写入重建。

        查询台语义是"我在看这一刻的数据"，所以进页面锁一次、点刷新再锁一次就够了；
        锁定期内所有查询都打在这份快照上（76ms 级），不会因为群里在聊天而反复重建。
        """
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            # 不强制重建：缓存已经是最新（指纹一致，插件加载时预热过就直接命中），
            # 只在真的过期时才重建——否则每次进页面都白付 1 秒。
            self._snapshot_locked = False
            # 绕过 2 秒节流：进页面/点刷新要的是"此刻"，不能被防抖动逻辑挡住
            self._merged_built_at = 0.0
            self._records_source(connection)      # 指纹一致则复用，否则重建
            self._overview_query(connection)      # 命中缓存则 0 成本
            self._snapshot_locked = True
            rows = 0
            try:
                rows = int(connection.execute(
                    f"SELECT COUNT(*) FROM temp.{_MERGED_CACHE_NAME}"
                ).fetchone()[0])
            except sqlite3.DatabaseError:
                rows = 0
            return {"locked": True, "rows": rows}

        return self._run_read(operation)

    def release_snapshot(self) -> None:
        """解除锁定：之后按库指纹自动重建（数据源切换等场景用）。"""
        self._snapshot_locked = False
        self._overview_cache = None

    def warm(self) -> None:
        """同步预热：建好旧历史缓存与物化副本（插件加载后在后台线程调一次）。

        为什么：两条缓存合计约 0.9 秒，放在"用户第一次点开查询台"里就是纯等待；
        放到加载后台跑，用户点进来时已经在跑 76ms 的快路径。
        """
        try:
            # 三样都先算好：旧历史缓存、物化副本（_records_source 内部负责）、概览结果
            self._run_read(lambda connection: self._records_source(connection))
            self._run_read(self._overview_query)
        except Exception:  # noqa: BLE001 - 预热失败不影响后续按需重建
            pass

    async def health(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_read, self._health_query)

    async def overview(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_read, self._overview_query)

    async def facets(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = self._normalize_filters(filters or {})
        return await asyncio.to_thread(self._run_read, self._facets_query, normalized)

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_filters(payload)
        return await asyncio.to_thread(self._run_read, self._records_query, normalized)

    async def record(self, record_id: int) -> dict[str, Any]:
        try:
            normalized_id = int(record_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("记录 ID 必须是整数") from exc
        if normalized_id <= 0:
            raise ValueError("记录 ID 必须大于 0")
        return await asyncio.to_thread(self._run_read, self._record_query, normalized_id)

    @property
    def writable(self) -> bool:
        """当前查询的数据源是否就是插件实时库（决定能否恢复软删除记录）。"""
        return self.writable_path is not None and self.db_path == self.writable_path

    async def soft_delete_record(
        self, row_id: int, target: str = "record"
    ) -> dict[str, Any]:
        """软删除**单条**记录或工具调用（标记 ``deleted_at``，不物理删除）。

        软删除只对查询台开放，且一次只允许一条：记录本体不动、媒体文件也不动
        （媒体清理仍走 ``media_archive`` 的保留期/总量配置）。
        """
        return await self._write_deleted_at(row_id, target, deleted=True)

    async def restore_record(
        self, row_id: int, target: str = "record"
    ) -> dict[str, Any]:
        """恢复一条被软删除的记录或工具调用（清除 ``deleted_at``）。"""
        return await self._write_deleted_at(row_id, target, deleted=False)

    async def _write_deleted_at(
        self, row_id: int, target: str, *, deleted: bool
    ) -> dict[str, Any]:
        """查询台仅有的写路径：改单条记录/工具调用的 ``deleted_at``。

        只对插件实时库开放：数据源被改成其他路径（快照/备份库）时直接拒绝，避免误改快照。
        """
        table = self._target_table(target)
        try:
            normalized_id = int(row_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("ID 必须是整数") from exc
        if normalized_id <= 0:
            raise ValueError("ID 必须大于 0")
        if not self.writable:
            raise PermissionError("当前数据源不是插件实时数据库，删除/恢复操作已禁用")
        return await asyncio.to_thread(
            self._apply_deleted_at, table, normalized_id, target, deleted
        )

    @staticmethod
    def _target_table(target: str) -> str:
        """写目标表：record=主表，tool=工具调用表（两者 ID 空间独立）。"""
        normalized = str(target or "record").strip().lower()
        if normalized == "record":
            return TABLE_NAME
        if normalized == "tool":
            return TOOL_TABLE_NAME
        raise ValueError("未知的删除目标")

    def _apply_shadow_deleted_at(
        self,
        connection: sqlite3.Connection,
        row_id: int,
        target: str,
        deleted: bool,
    ) -> dict[str, Any]:
        """合并存储里的单条软删除/恢复：改 ``chat_memory_messages.deleted_at``。"""
        rowid = int(row_id) - _MERGED_ID_OFFSET
        row = connection.execute(
            "SELECT rowid AS rid, msg_uid, deleted_at FROM chat_memory_messages "
            "WHERE rowid = ?",
            (rowid,),
        ).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        current = str(row["deleted_at"] or "").strip()
        if deleted and current:
            raise ValueError("记录已被软删除")
        if not deleted and not current:
            raise ValueError("记录未被软删除")
        now_raw = (
            datetime.now(dt_timezone.utc)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S.%f")
        )
        if deleted:
            connection.execute(
                "UPDATE chat_memory_messages SET deleted_at = :now WHERE rowid = :rid",
                {"now": now_raw, "rid": rowid},
            )
            deleted_at_raw = now_raw
        else:
            connection.execute(
                "UPDATE chat_memory_messages SET deleted_at = NULL WHERE rowid = :rid",
                {"rid": rowid},
            )
            deleted_at_raw = ""
        connection.commit()
        return {
            "id": row_id,
            "target": target,
            "deleted": bool(deleted_at_raw),
            "deleted_at": (
                self._time_pair(deleted_at_raw)[1] if deleted_at_raw else ""
            ),
        }

    def _apply_deleted_at(
        self, table: str, row_id: int, target: str, deleted: bool
    ) -> dict[str, Any]:
        if not self.db_path.is_file():
            raise DatabaseUnavailableError(f"未找到 ChatMemory 数据库：{self.db_path}")
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            self._validate_schema(connection)
            # id >= _MERGED_ID_OFFSET 的是合并存储行（chat_memory_messages 的 rowid）：
            # 删除/恢复必须写那张表，否则按 id 去改旧表会改到另一条消息（或改不到）。
            shadow_read = (
                table == TABLE_NAME
                and row_id >= _MERGED_ID_OFFSET
                and self._shadow_ready(connection)
            )
            has_column = (
                (self._has_deleted_column(connection) or shadow_read)
                if table == TABLE_NAME
                else self._has_tool_deleted_column(connection)
            )
            if not has_column:
                raise ValueError("该数据库没有软删除列（schema < v8），无法软删除")
            if shadow_read:
                result = self._apply_shadow_deleted_at(
                    connection, row_id, target, deleted
                )
                # 与旧表分支同样的失效动作：只置 legacy 脏标记不够——页面已锁快照时
                # `_ensure_merged_cache` 会直接返回锁定缓存，详情抽屉读到的仍是删除前状态。
                self._legacy_cache_dirty = True
                self._merged_key = None
                self._merged_ready = False
                self._snapshot_locked = False
                self._overview_cache = None
                return result
            # 取整行：除状态校验外，还要拿 umo/cid/role/message_id/turn_id 去影子里
            # 定位同一条消息，把软删除一并写过去（双写期两边必须一致）
            current_row = connection.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()
            if current_row is None:
                # 区分「不存在」与「状态不符」，排障时不会被合成一条消息误导
                raise ValueError("记录不存在")
            current = str(current_row["deleted_at"] or "").strip()
            if deleted and current:
                raise ValueError("记录已被软删除")
            if not deleted and not current:
                raise ValueError("记录未被软删除")
            now_raw = (
                datetime.now(dt_timezone.utc)
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S.%f")
            )
            if deleted:
                connection.execute(
                    f"UPDATE {table} SET deleted_at = :now WHERE id = :rid",
                    {"now": now_raw, "rid": row_id},
                )
                deleted_at_raw = now_raw
            else:
                connection.execute(
                    f"UPDATE {table} SET deleted_at = NULL WHERE id = :rid",
                    {"rid": row_id},
                )
                deleted_at_raw = ""
            self._mirror_shadow_deleted_at(
                connection, table, current_row, now_raw if deleted else ""
            )
            connection.commit()
            self._legacy_cache_dirty = True
            self._merged_key = None      # 自己的写入立刻失效，不受节流与锁影响
            self._merged_ready = False
            self._snapshot_locked = False
            return {
                "id": row_id,
                "target": target,
                "deleted": bool(deleted_at_raw),
                "deleted_at": (
                    self._time_pair(deleted_at_raw)[1] if deleted_at_raw else ""
                ),
            }
        except sqlite3.DatabaseError as exc:
            raise DatabaseUnavailableError(f"写入失败：{exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    def _shadow_ready(self, connection: sqlite3.Connection) -> bool:
        """合并存储能否当读源：**插件已切读** + 三张表齐 + 结构版本一致 + 双写窗口已满。

        最后两条与插件侧（上下文 / 公开 API）用同一判据，但前提是插件本身就切了读：
        默认 ``shadow_read=false`` 时查询台必须一起读旧表，否则用户看到的记录与模型
        记住的不是同一套（页面显示分段合并后的文本，模型读到的却是计划全文）。
        """
        if getattr(self, "read_source", "v1") != "v2":
            return False
        try:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                    "(?, ?, ?)",
                    _SHADOW_TABLES,
                )
            }
            if present != set(_SHADOW_TABLES):
                return False
            row = connection.execute(
                "SELECT value FROM chat_memory_shadow_meta WHERE key = 'schema_version'"
            ).fetchone()
            # 版本号必须与写入侧同源：调用方传入（main 用 shared_store.SHADOW_SCHEMA_VERSION），
            # 写死字符串会在结构线升级时静默退回旧表（症状：昵称全变号码、同一条消息重复）。
            expected = str(getattr(self, "_expected_shadow_version", "") or "")
            if expected and str(row[0] if row else "") != expected:
                return False
            return self._dual_write_window_full(connection)
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def _dual_write_window_full(connection: sqlite3.Connection) -> bool:
        """双写是否已跑满 ``SHADOW_READ_MIN_DAYS`` 天（与插件侧同一判据）。"""
        try:
            row = connection.execute(
                "SELECT value FROM chat_memory_shadow_meta "
                "WHERE key = 'dual_write_started_at'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return False
        raw = str(row[0] if row else "").strip()
        if not raw:
            return False
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt_timezone.utc)
        elapsed = datetime.now(dt_timezone.utc) - started.astimezone(dt_timezone.utc)
        return elapsed.total_seconds() >= SHADOW_READ_MIN_DAYS * 86400

    def _v2_source(self, connection: sqlite3.Connection) -> str:
        """只有合并存储那一段（双写之后的物理消息，一条一行）。

        物化副本就绪时直接切它的高 id 段：合并视图里 v2 行的 id 都 ≥ 偏移、旧历史行都
        < 偏移（旧历史用 MIN(id) 折叠），所以两个来源天然一致，切出来等价于内联投影。
        """
        if self._merged_ready:
            return (
                f"(SELECT * FROM temp.{_MERGED_CACHE_NAME} "
                f"WHERE id >= {_MERGED_ID_OFFSET})"
            )
        shadow = _SHADOW_SELECT.replace("{offset}", str(_MERGED_ID_OFFSET))
        return "(WITH " + _SHADOW_CTES + ", v2 AS (" + shadow + ") SELECT * FROM v2)"

    def _legacy_total(
        self, connection: sqlite3.Connection, where: str, params: dict[str, Any]
    ) -> int | None:
        """旧历史（双写之前）在同样过滤条件下的条数。

        ``None`` = 临时表不在（``_prepare_legacy_cache`` 建表失败），此时**不能**返回 0：
        列表照样会从内联 UNION 里取到旧历史行，而 ``total`` 少算会让页码/末页对不上。
        调用方必须改用整段 COUNT。
        """
        cached = connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = ?",
            (_LEGACY_CACHE_NAME,),
        ).fetchone()
        if not cached:
            return None
        return int(
            connection.execute(
                f"SELECT COUNT(*) FROM temp.{_LEGACY_CACHE_NAME} AS r WHERE {where}",
                params,
            ).fetchone()[0]
        )

    def _records_source(self, connection: sqlite3.Connection) -> str:
        """记录表的 FROM 片段：优先物化副本，否则退回内联合并投影。

        物化副本（``temp._cm_merged_rows``）由 ``_ensure_merged_cache`` 按库指纹维护：
        库或旧历史缓存一变就重建，没变就直接查表（取一页 0.2ms 级）。
        """
        if not self._shadow_ready(connection):
            return TABLE_NAME
        if self._ensure_merged_cache(connection):
            return f"temp.{_MERGED_CACHE_NAME}"
        return self._inline_records_source(connection)

    def _ensure_merged_cache(self, connection: sqlite3.Connection) -> bool:
        """物化合并源到 TEMP 表；建不出来返回 False（调用方退回内联源，功能不变只慢些）。"""
        key = (self._db_fingerprint(), self._legacy_generation)
        # 先确认 TEMP 表真在**这条连接**上：连接被兜底重建过时布尔值还是 True，
        # 但表已经随旧连接销毁了（不确认就会报 no such table: temp._cm_merged_rows）。
        present = False
        if self._merged_ready:
            present = bool(connection.execute(
                "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = ?",
                (_MERGED_CACHE_NAME,),
            ).fetchone())
            if not present:
                self._merged_ready = False
        if present:
            if self._snapshot_locked:
                return True          # 锁定期：源就是这份快照，不看指纹
            if self._merged_key == key:
                return True
            # 活跃群聊里库几乎每条消息都在变：若每次查询都重建，页面刷新每次要付 565ms。
            # 同一连接上 2 秒内只重建一次（等同 2 秒快照）；我们自己的写入会强制失效。
            if time.time() - self._merged_built_at < _MERGED_REBUILD_MIN_SEC:
                return True
        ready = False
        try:
            connection.execute("PRAGMA query_only = OFF")
            try:
                connection.execute(f"DROP TABLE IF EXISTS temp.{_MERGED_CACHE_NAME}")
                connection.execute(
                    f"CREATE TEMP TABLE {_MERGED_CACHE_NAME} AS "
                    f"SELECT * FROM {self._inline_records_source(connection)}"
                )
                connection.execute(
                    f"CREATE INDEX temp.ix_{_MERGED_CACHE_NAME}_created "
                    f"ON {_MERGED_CACHE_NAME}(created_at DESC, id DESC)"
                )
                connection.execute(
                    f"CREATE INDEX temp.ix_{_MERGED_CACHE_NAME}_role "
                    f"ON {_MERGED_CACHE_NAME}(role)"
                )
                ready = True
            finally:
                connection.execute("PRAGMA query_only = ON")
        except sqlite3.DatabaseError:
            ready = False
        self._merged_key = key
        self._merged_ready = ready
        self._merged_built_at = time.time()
        return ready

    def _inline_records_source(self, connection: sqlite3.Connection) -> str:
        """内联合并投影（每个查询现算，慢但不占 temp）。"""
        if not self._shadow_ready(connection):
            return TABLE_NAME
        shadow = _SHADOW_SELECT.replace("{offset}", str(_MERGED_ID_OFFSET))
        cached = connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = ?",
            (_LEGACY_CACHE_NAME,),
        ).fetchone()
        if cached:
            return (
                "(WITH " + _SHADOW_CTES + ", v2 AS (" + shadow + ") "
                "SELECT * FROM v2 UNION ALL "
                f"SELECT * FROM temp.{_LEGACY_CACHE_NAME})"
            )
        return (
            "(WITH " + _SHADOW_CTES + ", v2 AS (" + shadow + "), "
            + _LEGACY_CTES + " "
            "SELECT * FROM v2 UNION ALL SELECT * FROM legacy)"
        )

    @classmethod
    def _mirror_shadow_deleted_at(
        cls,
        connection: sqlite3.Connection,
        table: str,
        row: sqlite3.Row,
        value: str,
    ) -> None:
        """把单条软删除/恢复镜像到合并存储（影子层），尽力而为、绝不外抛。

        为什么需要：查询台的删除只写旧表，切读之后被删的记录会"复活"。影子表可能不存在
        （查询台指向快照库、或影子开关关着）——那种情况直接跳过。
        定位规则（旧表里不会有"既没 mid 又没 turn_id"的行，实测 0 行）：
        ① 平台 message_id → 参与行 ``(umo, cid, mid, role)``；
        ② 定不到（assistant 行的旧表 ``message_id`` 恒为空）→ 按
           ``(umo, cid, turn_id, role)`` 取出这一轮该实例的**全部实发行**逐个标删
           （一轮回复可能是多段），再标这一轮的**计划行**。
           user 行**不走**这条兜底——计划成员代表的是这一轮 assistant 的产出，把用户
           记录的删除标上去会连带藏掉 BOT 的那句回复。
        注意：影子层一条物理消息只有一行（跨实例共享），所以删除是**消息级**的——
        同一个群里别的 BOT 实例在切读后也看不到它（旧表那边仍显示各自的未删除状态）。
        """
        try:
            if table == TABLE_NAME:
                if not cls._has_table(connection, SHADOW_MESSAGE_TABLE):
                    return
                msg_uid = cls._resolve_shadow_msg_uid(connection, row)
                if msg_uid:
                    connection.execute(
                        f"UPDATE {SHADOW_MESSAGE_TABLE} SET deleted_at = :v WHERE msg_uid = :uid",
                        {"v": value or None, "uid": msg_uid},
                    )
                    return
                role = str(row["role"] or "")
                turn = str(row["turn_id"] or "")
                if role == "assistant" and turn:
                    params = {
                        "v": value or None,
                        "umo": row["umo"],
                        "cid": row["conversation_id"],
                        "turn": turn,
                    }
                    # 这一轮的实发行（回显/拉取写进来的那几条，可能多个 msg_uid）
                    connection.execute(
                        f"UPDATE {SHADOW_MESSAGE_TABLE} SET deleted_at = :v WHERE msg_uid IN ("
                        "  SELECT msg_uid FROM chat_memory_message_bots "
                        "   WHERE umo = :umo AND conversation_id = :cid "
                        "     AND turn_id = :turn AND role = 'assistant')",
                        params,
                    )
                    if cls._has_table(connection, SHADOW_REPLY_TABLE):
                        connection.execute(
                            f"UPDATE {SHADOW_REPLY_TABLE} SET deleted_at = :v "
                            "WHERE umo = :umo AND conversation_id = :cid "
                            "AND turn_id = :turn",
                            params,
                        )
                return
            if not cls._has_table(connection, SHADOW_TOOL_TABLE):
                return
            connection.execute(
                f"UPDATE {SHADOW_TOOL_TABLE} SET deleted_at = :v WHERE umo = :umo "
                "AND conversation_id = :cid AND turn_id = :turn AND call_index = :idx",
                {
                    "v": value or None,
                    "umo": row["umo"],
                    "cid": row["conversation_id"],
                    "turn": row["turn_id"],
                    "idx": row["call_index"],
                },
            )
        except (sqlite3.DatabaseError, IndexError, KeyError, TypeError):
            # 镜像失败不能让查询台的删除/恢复失败：旧表那笔已经写好了。
            # IndexError/KeyError：老库缺 turn_id / message_id 列时按名取会抛，同样降级。
            return

    @staticmethod
    def _resolve_shadow_msg_uid(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> str:
        """按平台 mid 找物理消息；找不到返回空（计划成员行由调用方单独处理）。"""
        umo = str(row["umo"] or "")
        cid = str(row["conversation_id"] or "")
        role = str(row["role"] or "")
        mid = str(row["message_id"] or "")
        hit = None
        if mid:
            hit = connection.execute(
                f"SELECT msg_uid FROM chat_memory_message_bots WHERE umo = ? "
                "AND conversation_id = ? AND platform_message_id = ? AND role = ? LIMIT 1",
                (umo, cid, mid, role),
            ).fetchone()
        return str(hit[0]) if hit is not None else ""

    async def tool_records(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_tool_filters(payload)
        return await asyncio.to_thread(self._run_read, self._tool_records_query, normalized)

    def _run_read(self, operation: Callable[..., T], *args: Any) -> T:
        if not self.db_path.is_file():
            raise DatabaseUnavailableError(f"未找到 ChatMemory 数据库：{self.db_path}")

        try:
            if self.keep_open:
                connection = self._shared_connection()
                with self._cache_lock:
                    self._validate_schema(connection)
                    self._prepare_legacy_cache(connection)
                    payload = operation(connection, *args)
                access = self._access_info(connection, immutable=False)
            else:
                with closing(self._connect(immutable=False)) as connection:
                    self._validate_schema(connection)
                    self._prepare_legacy_cache(connection)
                    payload = operation(connection, *args)
                    access = self._access_info(connection, immutable=False)
        except sqlite3.DatabaseError as exc:
            self._drop_shared_connection()
            if not self.allow_immutable_fallback or not _is_corruption_error(exc):
                raise DatabaseUnavailableError(f"ChatMemory 数据库不可读：{exc}") from exc
            try:
                with closing(self._connect(immutable=True)) as connection:
                    self._validate_schema(connection)
                    self._prepare_legacy_cache(connection)
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

    def close(self) -> None:
        """释放复用的只读连接（TEMP 缓存表随之销毁）。"""
        self._drop_shared_connection()

    def __enter__(self) -> "ChatMemoryRepository":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _shared_connection(self) -> sqlite3.Connection:
        """复用一条只读连接（TEMP 缓存表跟着活着）；跨线程用锁串行化。"""
        with self._cache_lock:
            if self._cache_conn is None:
                self._cache_conn = self._connect(immutable=False, shared=True)
            return self._cache_conn

    def _drop_shared_connection(self) -> None:
        with self._cache_lock:
            if self._cache_conn is not None:
                try:
                    self._cache_conn.close()
                except sqlite3.DatabaseError:
                    pass
                self._cache_conn = None
            self._merged_key = None
            self._merged_ready = False

    def _connect(self, *, immutable: bool, shared: bool = False) -> sqlite3.Connection:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        connection = sqlite3.connect(
            f"{self.db_path.as_uri()}?{query}",
            uri=True,
            timeout=3,
            check_same_thread=not shared,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 3000")
        # 合并读源一次查询要扫十几万行、且同一份数据反复扫：64MB 页缓存 + 256MB mmap
        # 实测把重复扫描从 ~35ms 压到 ~16ms（内存映射读，不走每次 read()）
        try:
            connection.execute("PRAGMA cache_size = -65536")
            connection.execute("PRAGMA mmap_size = 268435456")
        except sqlite3.DatabaseError:
            pass
        return connection

    def _prepare_legacy_cache(self, connection: sqlite3.Connection) -> None:
        """把"双写前的旧历史去重结果"一次性落成 TEMP 表（只读连接允许建 temp 表）。

        为什么：旧段要按 (会话, 发送者, 正文, 平台时间) 折叠，116k 行每查一次 GROUP BY
        太贵（实测整页 2.3s）；折叠结果在双写之前的行上是**静止**的，一次建好后续都在
        0.1s 量级。建不出来（只读库/权限/旧 SQLite）就回落成每次查的 CTE。
        """
        if not self._shadow_ready(connection):
            return
        exists = connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = ?",
            (_LEGACY_CACHE_NAME,),
        ).fetchone()
        if exists and not self._legacy_cache_dirty:
            return
        try:
            # query_only 会连 TEMP 表一起禁掉：重建这段时间放开，建完立刻收回。
            # DROP 也必须在这个区间里做，否则只读 pragma 会把它挡掉、缓存永远不刷新。
            connection.execute("PRAGMA query_only = OFF")
            try:
                if exists:
                    # 有过软删除/恢复：旧缓存里的 deleted_at 已经过期，丢掉重建
                    connection.execute("DROP TABLE temp." + _LEGACY_CACHE_NAME)
                connection.execute(
                    "CREATE TEMP TABLE " + _LEGACY_CACHE_NAME + " AS "
                    + _LEGACY_DEDUPE_SELECT
                )
                connection.execute(
                    f"CREATE INDEX temp.ix_{_LEGACY_CACHE_NAME}_created "
                    f"ON {_LEGACY_CACHE_NAME}(created_at)"
                )
                self._legacy_cache_dirty = False
                self._legacy_generation += 1
            finally:
                connection.execute("PRAGMA query_only = ON")
        except sqlite3.DatabaseError:
            # 建不出来就当没有：_records_source 会走每次 GROUP BY 的版本（结果一样，慢些）
            try:
                connection.execute("PRAGMA query_only = ON")
            except sqlite3.DatabaseError:
                pass

    @staticmethod
    def _has_tool_deleted_column(connection: sqlite3.Connection) -> bool:
        """工具表是否有 deleted_at（schema >= v8）。"""
        try:
            info = connection.execute(
                f"PRAGMA table_info({TOOL_TABLE_NAME})"
            ).fetchall()
        except sqlite3.DatabaseError:
            return False
        return any(str(row[1]) == "deleted_at" for row in info)

    @staticmethod
    def _has_deleted_column(connection: sqlite3.Connection) -> bool:
        """主表是否有 deleted_at（schema >= v8）。旧库/旧快照按无软删除处理。"""
        try:
            info = connection.execute(
                f"PRAGMA table_info({TABLE_NAME})"
            ).fetchall()
        except sqlite3.DatabaseError:
            return False
        return any(str(row[1]) == "deleted_at" for row in info)

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
        source = self._records_source(connection)
        has_deleted = self._has_deleted_column(connection) or source != TABLE_NAME
        visible = "WHERE r.deleted_at IS NULL" if has_deleted else ""
        row = connection.execute(
            f"SELECT COUNT(*) AS total, MIN(r.created_at) AS oldest, "
            f"MAX(r.created_at) AS newest FROM {source} AS r {visible}"
        ).fetchone()
        return {
            "healthy": True,
            "total_records": int(row["total"] or 0),
            "oldest_at": self._local_time(row["oldest"]),
            "newest_at": self._local_time(row["newest"]),
            "deleted_records": self._deleted_count(connection),
        }

    def _overview_query(self, connection: sqlite3.Connection) -> dict[str, Any]:
        """概览：统计口径与列表一致，但计算代价高（合并源上多条聚合）。

        结果按「库文件 + WAL 的 mtime/size」缓存，另有 30 秒 TTL：库没变就复用，
        重开页面/切回概览不必再付一次一两秒；有任何写入（含 WAL 追加）指纹就变、
        立即重算，不会显示过期统计。列表与筛选完全不走这条路径。
        """
        now = time.time()
        fingerprint = self._db_fingerprint()
        cached = self._overview_cache
        if self._snapshot_locked and cached:
            return cached[2]         # 锁定期：概览也属于这份快照
        if (fingerprint is not None and cached and cached[0] == fingerprint
                and now - cached[1] < 30):
            return cached[2]
        source = self._records_source(connection)
        has_deleted = self._has_deleted_column(connection) or source != TABLE_NAME
        # 软删除行不算进概览（与列表口径一致），另用 deleted_records 单独说明。
        # 同一次扫描里用 CASE 区分：不筛 WHERE（否则统计不到被删的行），只跳过计数。
        visible = "WHERE deleted_at IS NULL" if has_deleted else ""
        if has_deleted:
            summary = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS total_records,
                    COUNT(DISTINCT CASE WHEN deleted_at IS NULL THEN umo END) AS total_umos,
                    COUNT(DISTINCT CASE WHEN deleted_at IS NULL THEN conversation_id END)
                        AS total_conversations,
                    COUNT(DISTINCT CASE WHEN deleted_at IS NULL AND role = 'user'
                                        THEN user_id END) AS total_users,
                    MIN(CASE WHEN deleted_at IS NULL THEN created_at END) AS oldest_at,
                    MAX(CASE WHEN deleted_at IS NULL THEN created_at END) AS newest_at,
                    SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_count
                FROM {source} AS r
                """
            ).fetchone()
        else:
            summary = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total_records,
                    COUNT(DISTINCT umo) AS total_umos,
                    COUNT(DISTINCT conversation_id) AS total_conversations,
                    COUNT(DISTINCT CASE WHEN role = 'user' THEN user_id END) AS total_users,
                    MIN(created_at) AS oldest_at,
                    MAX(created_at) AS newest_at,
                    0 AS deleted_count
                FROM {source} AS r
                """
            ).fetchone()
        # role 与 llm_status 用同一次扫描算：二维分组后各自求和即得边际分布，
        # 省掉一遍「v2 ∪ legacy」的重算（每条 overview 查询都在重算它）。
        pair_rows = connection.execute(
            f"SELECT role AS role, llm_status AS status, COUNT(*) AS amount "
            f"FROM {source} AS r {visible} GROUP BY role, llm_status"
        ).fetchall()
        role_totals: dict[str, int] = {}
        status_totals: dict[str, int] = {}
        for row in pair_rows:
            role_totals[str(row["role"] or "")] = (
                role_totals.get(str(row["role"] or ""), 0) + int(row["amount"])
            )
            status_totals[str(row["status"] or "")] = (
                status_totals.get(str(row["status"] or ""), 0) + int(row["amount"])
            )
        roles = _count_map_from_map(role_totals, empty_label="")
        statuses = _count_map_from_map(status_totals, empty_label="no_llm")
        kinds = _count_map(
            connection.execute(
                f"""
                SELECT kinds.value AS value, COUNT(*) AS amount
                FROM {source} AS r,
                     json_each(CASE WHEN json_valid(content_kind)
                                    THEN content_kind ELSE '[]' END) AS kinds
                {visible}
                GROUP BY kinds.value
                ORDER BY amount DESC
                """
            )
        )
        # created_at 存 UTC naive；按插件时区偏移分桶，避免日界与展示口径不一致
        soft_and = "r.deleted_at IS NULL" if has_deleted else "1 = 1"
        tz_offset = self.timezone.utcoffset(datetime.now(self.timezone))
        offset_seconds = int((tz_offset or timedelta(0)).total_seconds())
        daily_rows = connection.execute(
            f"""
            SELECT date(created_at, :tz_offset) AS day, COUNT(*) AS amount
            FROM {source} AS r
            WHERE r.created_at >= datetime('now', '-29 days')
              AND {soft_and}
            GROUP BY day
            ORDER BY day ASC
            """,
            {"tz_offset": f"{offset_seconds} seconds"},
        ).fetchall()
        tools = self._tools_overview(connection)
        media = self._media_overview(connection)
        payload = {
            # 当前读源：页面必须能看出"这份数据来自旧表还是合并存储"（两套口径不同）。
            "read_source": self.read_source,
            "summary": {
                "total_records": int(summary["total_records"] or 0),
                "total_umos": int(summary["total_umos"] or 0),
                "total_conversations": int(summary["total_conversations"] or 0),
                "total_users": int(summary["total_users"] or 0),
                "oldest_at": self._local_time(summary["oldest_at"]),
                "newest_at": self._local_time(summary["newest_at"]),
                "deleted_records": int(summary["deleted_count"] or 0),
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
        self._overview_cache = (
            (fingerprint, now, payload) if fingerprint is not None else None
        )
        return payload

    def _db_fingerprint(self) -> tuple | None:
        """库文件与 WAL 的 (mtime_ns, size)：任何写入都会变（WAL 追加也算）。"""
        parts: list[int] = []
        for suffix in ("", "-wal"):
            try:
                st = Path(f"{self.db_path}{suffix}").stat()
            except OSError:
                parts.extend((0, 0))
                continue
            parts.extend((st.st_mtime_ns, st.st_size))
        return tuple(parts)

    def _deleted_count(self, connection: sqlite3.Connection) -> int:
        """已软删除（deleted_at 非空）的记录数；旧库无该列时返回 0。"""
        source = self._records_source(connection)
        if not self._has_deleted_column(connection) and source == TABLE_NAME:
            return 0
        row = connection.execute(
            f"SELECT COUNT(*) AS amount FROM {source} AS r "
            f"WHERE r.deleted_at IS NOT NULL"
        ).fetchone()
        return int(row["amount"] or 0)

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
        tool_visible = ChatMemoryRepository._has_tool_deleted_column(connection)
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
                "present": False,
                "total_calls": 0,
                "deleted_calls": 0,
                "tool_names": {},
            }
        visible = " WHERE deleted_at IS NULL" if tool_visible else ""
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {TOOL_TABLE_NAME}{visible}"
            ).fetchone()[0]
            or 0
        )
        names = _count_map(
            connection.execute(
                f"SELECT tool_name AS value, COUNT(*) AS amount "
                f"FROM {TOOL_TABLE_NAME}{visible} "
                "GROUP BY tool_name ORDER BY amount DESC"
            ),
            empty_label="(empty)",
        )
        deleted_calls = 0
        if tool_visible:
            deleted_calls = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {TOOL_TABLE_NAME} "
                    "WHERE deleted_at IS NOT NULL"
                ).fetchone()[0]
                or 0
            )
        return {
            "present": True,
            "total_calls": total,
            "deleted_calls": deleted_calls,
            "tool_names": names,
        }

    def _facets_query(
        self, connection: sqlite3.Connection, filters: dict[str, Any]
    ) -> dict[str, Any]:
        """筛选项只保留「内容类型」的下拉候选（其余条件都是手填/固定选项）。"""
        source = self._records_source(connection)
        shadow = source != TABLE_NAME
        scoped = {
            key: value
            for key, value in filters.items()
            if key
            not in (
                "content_kind",
                "advanced",
                "paired_only",
                "persona_id",
                "persona_role",
                "persona_llm_status",
            )
        }
        conditions, params = self._where_clause(
            scoped,
            alias="r",
            soft_delete=self._has_deleted_column(connection) or shadow,
            sender_column="sender_id" if shadow else "user_id",
            shadow=shadow,
        )
        where = " AND ".join(conditions) if conditions else "1 = 1"
        kind_rows = connection.execute(
            f"""
            SELECT kinds.value AS value, COUNT(*) AS amount
            FROM {source} AS r
            JOIN json_each(
                CASE WHEN json_valid(r.content_kind)
                     THEN r.content_kind ELSE '[]' END
            ) AS kinds
            WHERE {where}
            GROUP BY kinds.value
            ORDER BY amount DESC
            """,
            params,
        ).fetchall()
        return {
            "kinds": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in kind_rows
                if row["value"] in VALID_KINDS
            ],
        }

    def _records_query(
        self, connection: sqlite3.Connection, filters: dict[str, Any]
    ) -> dict[str, Any]:
        source = self._records_source(connection)
        shadow = source != TABLE_NAME
        conditions, params = self._where_clause(
            filters,
            alias="r",
            soft_delete=self._has_deleted_column(connection) or shadow,
            sender_column="sender_id" if shadow else "user_id",
            shadow=shadow,
        )
        where = " AND ".join(conditions) if conditions else "1 = 1"
        page = filters["page"]
        page_size = filters["page_size"]
        offset = (page - 1) * page_size
        page_params = {**params, "page_size": page_size, "offset": offset}
        if shadow:
            # 取页统一走整段来源：原来按"v2 段整体在旧段之前"分片取（offset 落在 v2 段就只查
            # v2），但 `_SHADOW_SELECT` 对 created_at 没有下界——一旦合并存储里出现比双写
            # 起点更早的行（导入/回填/对拍写入），排序与分片切分就错位，跨页重复或漏行。
            # 物化副本就绪后整段取一页本来就是 76ms 级，不值得为它冒这个风险。
            legacy_total = self._legacy_total(connection, where, params)
            if legacy_total is None:
                # 临时表建不出来（如旧 SQLite / 只读限制）：退回整段 COUNT，别用 0 冒充
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {source} AS r WHERE {where}", params
                    ).fetchone()[0]
                )
            else:
                v2_total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {self._v2_source(connection)} AS r "
                        f"WHERE {where}",
                        params,
                    ).fetchone()[0]
                )
                total = v2_total + legacy_total
            rows = connection.execute(
                f"SELECT r.* FROM {source} AS r WHERE {where} "
                "ORDER BY r.created_at DESC, r.id DESC "
                "LIMIT :page_size OFFSET :offset",
                page_params,
            ).fetchall()
        else:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {source} AS r WHERE {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT r.* FROM {source} AS r WHERE {where} "
                "ORDER BY r.created_at DESC, r.id DESC "
                "LIMIT :page_size OFFSET :offset",
                page_params,
            ).fetchall()
        reply_targets = self._fetch_reply_targets(connection, rows)
        media_archive = self._fetch_media_archive(connection, rows)
        # 昵称补全依赖 messages.sender_id（合并存储专有列）：旧表库不启用，
        # V1 读路径与改造前完全一致。
        sender_names = (
            self._fetch_sender_names(connection, rows) if shadow else {}
        )
        notes = (
            self._advanced_notes(connection, rows, filters.get("persona_id") or "")
            if filters.get("advanced") and shadow
            else {}
        )
        return {
            "items": [
                self._serialize_record(
                    row,
                    preview_limit=4000,
                    reply_targets=reply_targets,
                    media_archive=media_archive,
                    advanced=notes.get(str(_row_value(row, "msg_uid") or "")),
                    sender_name=sender_names.get(
                        str(_row_value(row, "sender_id") or "")
                    ),
                )
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": (total + page_size - 1) // page_size if total else 0,
            "filters": filters,
        }

    @staticmethod
    def _resolved_nickname(row: sqlite3.Row, sender_name: str | None) -> str | None:
        """行内昵称缺失、或昵称就是号码本身时，用同类消息里出现过的名字补上。"""
        nickname = str(_row_value(row, "sender_nickname") or "").strip()
        sender_id = str(_row_value(row, "sender_id") or "").strip()
        if sender_name and (not nickname or (sender_id and nickname == sender_id)):
            return sender_name or None
        return nickname or None

    def _fetch_sender_names(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> dict[str, str]:
        """补发送者昵称：同一个 QQ 有时写进了名字、有时只有号（第一条观察没带卡名）。

        只在页面里确实缺名字的发送者上查一次，取该发送者出现最多的非空昵称。
        """
        def needs_name(row: Any) -> str:
            """缺名字，或"名字"其实就是号码本身（采集时没有群名片就回填成 id）。"""
            sender_id = str(_row_value(row, "sender_id") or "").strip()
            nickname = str(_row_value(row, "sender_nickname") or "").strip()
            if not sender_id:
                return ""
            if not nickname or nickname == sender_id:
                return sender_id
            return ""

        missing = {name for name in (needs_name(row) for row in rows) if name}
        if not missing:
            return {}
        names: dict[str, str] = {}
        wanted = sorted(missing)
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            named = {f"s{index}": value for index, value in enumerate(chunk)}
            placeholders = ", ".join(f":{name}" for name in named)
            try:
                found = connection.execute(
                    f"""
                    SELECT sender_id AS value, sender_nickname AS nickname,
                           COUNT(*) AS amount
                      FROM chat_memory_messages
                     WHERE sender_id IN ({placeholders})
                       AND COALESCE(sender_nickname, '') <> ''
                       AND TRIM(sender_nickname) <> sender_id
                     GROUP BY sender_id, sender_nickname
                     ORDER BY (sender_nickname GLOB '[0-9]*') ASC, amount DESC
                    """,
                    named,
                ).fetchall()
            except sqlite3.DatabaseError:
                return names
            for item in found:
                key = str(item["value"])
                if key not in names:
                    names[key] = str(item["nickname"])
        return names

    def _advanced_notes(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        persona_id: str,
    ) -> dict[str, dict[str, Any]]:
        """高级模式的每消息注解：这条消息在所选 persona 下的轮次与状态。

        消息层上面还有一层"逻辑轮次"（参与行上的 turn_id / llm_status / send_status），
        同一物理消息可能被多个 persona 的轮次各看一次——所以必须指定 persona 才有意义。
        """
        wanted = [
            str(_row_value(row, "msg_uid") or "")
            for row in rows
            if _row_value(row, "msg_uid")
        ]
        if not wanted:
            return {}
        notes: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            named = {f"u{index}": uid for index, uid in enumerate(chunk)}
            placeholders = ", ".join(f":{name}" for name in named)
            params = {"persona": persona_id, **named}
            try:
                found = connection.execute(
                    f"""
                    SELECT b.msg_uid AS msg_uid, b.turn_id AS turn_id,
                           COALESCE(b.llm_status, '') AS llm_status,
                           COALESCE(b.send_status, '') AS send_status,
                           b.role AS role,
                           EXISTS (
                               SELECT 1 FROM chat_memory_message_bots a
                                WHERE a.turn_id = b.turn_id
                                  AND a.role = 'assistant'
                                  AND COALESCE(b.turn_id, '') <> ''
                           ) AS paired,
                           COALESCE((
                               SELECT a.llm_status FROM chat_memory_message_bots a
                                WHERE a.turn_id = b.turn_id
                                  AND a.role = 'assistant'
                                ORDER BY (COALESCE(a.llm_status, '') <> '') DESC,
                                         a.id ASC
                                LIMIT 1
                           ), '') AS turn_llm_status,
                           COALESCE((
                               SELECT a.send_status FROM chat_memory_message_bots a
                                WHERE a.turn_id = b.turn_id
                                  AND a.role = 'assistant'
                                ORDER BY (COALESCE(a.send_status, '') <> '') DESC,
                                         a.id ASC
                                LIMIT 1
                           ), '') AS turn_send_status
                      FROM chat_memory_message_bots b
                     WHERE COALESCE(b.persona_id, '') = :persona
                       AND b.msg_uid IN ({placeholders})
                     ORDER BY (COALESCE(b.turn_id, '') <> '') DESC
                    """,
                    params,
                ).fetchall()
            except sqlite3.DatabaseError:
                return notes
            for item in found:
                key = str(item["msg_uid"])
                current = notes.get(key)
                candidate = {
                    "turn_id": str(item["turn_id"] or ""),
                    "llm_status": str(item["llm_status"] or ""),
                    "send_status": str(item["send_status"] or ""),
                    "turn_llm_status": str(item["turn_llm_status"] or ""),
                    "turn_send_status": str(item["turn_send_status"] or ""),
                    "role": str(item["role"] or ""),
                    "paired": bool(item["paired"]),
                }
                if current is None or (
                    not current["turn_id"] and candidate["turn_id"]
                ):
                    notes[key] = candidate
        return notes

    def _persona_facets_query(
        self, connection: sqlite3.Connection
    ) -> dict[str, Any]:
        """高级只读模式的 persona 候选（参与行上出现过的 persona + 条数）。"""
        shadow = self._records_source(connection) != TABLE_NAME
        table = "chat_memory_message_bots" if shadow else TABLE_NAME
        # 参与行是"每实例一行"，同一轮会被 N 个 BOT 各记一次；按 msg_uid 去重才是
        # 用户理解的"这个 persona 有多少条记录"。
        counted = "COUNT(DISTINCT msg_uid)" if shadow else "COUNT(*)"
        try:
            rows = connection.execute(
                f"SELECT COALESCE(persona_id, '') AS value, {counted} AS amount "
                f"FROM {table} GROUP BY COALESCE(persona_id, '') "
                f"ORDER BY amount DESC LIMIT 200"
            ).fetchall()
        except sqlite3.DatabaseError:
            return {"personas": []}
        return {
            "personas": [
                {"value": str(row["value"]), "count": int(row["amount"])}
                for row in rows
            ]
        }

    async def personas(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_read, self._persona_facets_query)

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
        ordered = sorted(wanted)
        for chunk_start in range(0, len(ordered), 400):
            chunk = ordered[chunk_start : chunk_start + 400]
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

    def export_snapshot(self, dest: Path) -> int:
        """用 SQLite backup API 把当前库导出为一致快照文件。

        WAL 模式下备份走读事务，源库的读写均不阻塞；返回快照字节数。
        库文件不存在时抛 DatabaseUnavailableError（与查询路径 503 语义一致）。
        """
        if not self.db_path.is_file():
            raise DatabaseUnavailableError(f"数据库不存在：{self.db_path}")
        with closing(self._connect(immutable=False)) as source:
            dest_conn = sqlite3.connect(str(dest))
            try:
                source.backup(dest_conn)
            finally:
                dest_conn.close()
        return dest.stat().st_size

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
        source = self._records_source(connection)
        shadow = source != TABLE_NAME
        for umo, turn_ids in needed.items():
            placeholders = ", ".join("?" * len(turn_ids))
            if shadow:
                # 合并存储里同一条 user 消息可能挂在别的实例的 umo 下（代表实例不一定是
                # 被回复的那个），只按 turn_id 取；走参与行 + 消息表的窄查询，别为了几行
                # 把整个单例投影再物化一遍（实测 0.31s → 0.02s）。
                sql = (
                    "SELECT m.content, m.content_kind, m.relation_data, "
                    "m.sender_nickname, b.turn_id FROM chat_memory_message_bots AS b "
                    "JOIN chat_memory_messages AS m ON m.msg_uid = b.msg_uid "
                    f"WHERE b.role = 'user' AND b.turn_id IN ({placeholders}) "
                    "ORDER BY m.created_at ASC"
                )
                args: tuple = tuple(sorted(turn_ids))
            else:
                sql = (
                    f"SELECT * FROM {TABLE_NAME} "
                    f"WHERE umo = ? AND role = 'user' AND turn_id IN ({placeholders}) "
                    "ORDER BY created_at ASC, id ASC"
                )
                args = (umo, *sorted(turn_ids))
            try:
                target_rows = connection.execute(sql, args).fetchall()
            except sqlite3.DatabaseError:
                target_rows = []
            for target in target_rows:
                raw = str(target["content"] or "")
                rel = _parse_relation_data(_row_value(target, "relation_data"))
                kinds = _parse_content_kind(_row_value(target, "content_kind"))
                targets[(umo, str(target["turn_id"]))] = {
                    "sender_nickname": str(
                        _row_value(target, "sender_nickname") or ""
                    ),
                    "content": _render_content(raw, rel, kinds),
                }
        return targets

    def _record_query(
        self, connection: sqlite3.Connection, record_id: int
    ) -> dict[str, Any]:
        source = self._records_source(connection)
        row = connection.execute(
            f"SELECT * FROM {source} AS r WHERE r.id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"记录 #{record_id} 不存在")
        reply_targets = self._fetch_reply_targets(connection, [row])
        media_archive = self._fetch_media_archive(connection, [row])
        # 昵称补全与列表同一口径：不然同一行在列表里有名字、点进详情又变回号码
        sender_names = (
            self._fetch_sender_names(connection, [row])
            if self._records_source(connection) != TABLE_NAME
            else {}
        )
        return {
            "record": self._serialize_record(
                row,
                preview_limit=200_000,
                reply_targets=reply_targets,
                media_archive=media_archive,
                sender_name=sender_names.get(str(_row_value(row, "sender_id") or "")),
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
        # 人话口径：umo 是 "平台:类型:会话号"，拆成三个可填的筛选，不再让人手抄 UMO
        platform_id = filters.get("platform_id") or ""
        if platform_id:
            conditions.append("umo LIKE :platform_like ESCAPE '\\'")
            params["platform_like"] = f"{_escape_like(platform_id)}:%"
        chat_type = filters.get("chat_type") or ""
        if chat_type == "group":
            conditions.append("umo LIKE '%:GroupMessage:%'")
        elif chat_type == "private":
            conditions.append("umo NOT LIKE '%:GroupMessage:%'")
        session_id = filters.get("session_id") or ""
        if session_id:
            # 尾部锚定：`LIKE '%:1234'` 会把 ":12345" 也算命中，与记录页的精确
            # `session_id = :session_id` 口径相反（填 1234 却看到别人的记录）。
            conditions.append(
                "substr(umo, -length(:session_suffix)) = :session_suffix"
            )
            params["session_suffix"] = f":{session_id}"
        if filters.get("conversation_id"):
            conditions.append("conversation_id = :conversation_id")
            params["conversation_id"] = filters["conversation_id"]
        persona_id = filters.get("persona_id")
        if persona_id is not None:
            persona_table = (
                "chat_memory_reply_messages"
                if self._shadow_ready(connection)
                else "chat_memory_records"
            )
            conditions.append(
                f"EXISTS (SELECT 1 FROM {persona_table} AS p "
                f"WHERE p.umo = {TOOL_TABLE_NAME}.umo "
                f"AND p.turn_id = {TOOL_TABLE_NAME}.turn_id "
                "AND COALESCE(p.persona_id, '') = :persona_id)"
            )
            params["persona_id"] = persona_id
        if filters.get("tool_name"):
            conditions.append("tool_name LIKE :tool_name ESCAPE '\\'")
            params["tool_name"] = f"%{_escape_like(filters['tool_name'])}%"
        if filters.get("since"):
            conditions.append("created_at >= :since")
            params["since"] = filters["since"]
        if filters.get("until"):
            conditions.append("created_at <= :until")
            params["until"] = filters["until"]
        # 软删除（v8）：与主表同一套三态筛选；旧库无该列时自动降级
        soft_delete = self._has_tool_deleted_column(connection)
        deleted_state = filters.get("deleted_state") or "visible"
        if soft_delete and deleted_state == "visible":
            conditions.append("deleted_at IS NULL")
        elif soft_delete and deleted_state == "deleted":
            conditions.append("deleted_at IS NOT NULL")
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
            SELECT *,
                   -- 这一轮一共调了几次工具（同 umo+cid+turn）
                   CASE WHEN COALESCE(turn_id, '') = '' THEN 1 ELSE (
                       SELECT COUNT(*) FROM {TOOL_TABLE_NAME} AS t2
                        WHERE t2.umo = {TOOL_TABLE_NAME}.umo
                          AND t2.conversation_id = {TOOL_TABLE_NAME}.conversation_id
                          AND t2.turn_id = {TOOL_TABLE_NAME}.turn_id
                   ) END AS turn_total
              FROM {TOOL_TABLE_NAME}
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT :page_size OFFSET :offset
            """,
            {**params, "page_size": page_size, "offset": offset},
        ).fetchall()
        who = self._fetch_tool_callers(connection, rows)
        return {
            "items": [
                self._serialize_tool_record(
                    row,
                    who_persona=who.get(
                        (str(row["umo"]), str(row["turn_id"] or "")), ("", "")
                    )[0],
                    who_self_id=who.get(
                        (str(row["umo"]), str(row["turn_id"] or "")), ("", "")
                    )[1],
                )
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": (total + page_size - 1) // page_size if total else 0,
            "filters": filters,
        }

    def _fetch_tool_callers(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> dict[tuple[str, str], tuple[str, str]]:
        """本页调用所属轮次的"谁调的"：persona_id 与 self_id（BOT 的 QQ）。

        工具表只存 umo / turn_id；人格与 BOT 号码要从这一轮的参与行（V1 是记录行）取。
        """
        pairs = {
            (str(row["umo"]), str(row["turn_id"] or ""))
            for row in rows
            if str(row["umo"]) and str(row["turn_id"] or "")
        }
        if not pairs:
            return {}
        table = (
            "chat_memory_message_bots"
            if self._shadow_ready(connection)
            else "chat_memory_records"
        )
        found: dict[tuple[str, str], tuple[str, str]] = {}
        wanted = sorted(pairs)
        for start in range(0, len(wanted), 200):
            chunk = wanted[start:start + 200]
            clauses, params = [], {}
            for index, (umo, turn) in enumerate(chunk):
                clauses.append(f"(umo = :u{index} AND turn_id = :t{index})")
                params[f"u{index}"] = umo
                params[f"t{index}"] = turn
            try:
                result = connection.execute(
                    "SELECT umo, turn_id, MAX(NULLIF(persona_id, '')) AS persona_id, "
                    "MAX(NULLIF(self_id, '')) AS self_id "
                    f"FROM {table} WHERE {' OR '.join(clauses)} "
                    "GROUP BY umo, turn_id",
                    params,
                ).fetchall()
            except sqlite3.DatabaseError:
                return found
            for item in result:
                found[(str(item["umo"]), str(item["turn_id"] or ""))] = (
                    str(item["persona_id"] or ""),
                    str(item["self_id"] or ""),
                )
        return found

    def _serialize_tool_record(
        self,
        row: sqlite3.Row,
        who_persona: str = "",
        who_self_id: str = "",
    ) -> dict[str, Any]:
        created_at_utc, created_at_local = self._time_pair(row["created_at"])
        deleted_at_raw = _row_value(row, "deleted_at")
        return {
            "tool_id": int(row["id"]),
            "deleted": bool(str(deleted_at_raw or "").strip()),
            "deleted_at": (
                self._time_pair(deleted_at_raw)[1] if deleted_at_raw else ""
            ),
            "umo": str(row["umo"]),
            # BOT = umo 第一段（platform_id），前端直接当 chip
            "platform_id": str(row["umo"]).split(":")[0],
            "conversation_id": str(row["conversation_id"]),
            "turn_id": str(row["turn_id"] or ""),
            "call_index": int(row["call_index"]),
            "turn_total": int(_row_value(row, "turn_total") or 0),
            "persona_id": who_persona,
            "self_id": who_self_id,
            "tool_name": str(row["tool_name"] or ""),
            "tool_args": str(row["tool_args"] or ""),
            "tool_result": str(row["tool_result"] or ""),
            "created_at": created_at_local,
            "created_at_utc": created_at_utc,
        }

    @staticmethod
    def _normalize_deleted_state(raw: Any) -> str:
        """删除状态三态归一：all / visible（默认） / deleted。"""
        value = str(raw or "").strip().lower() or "visible"
        if value not in VALID_DELETED_STATES:
            raise ValueError("未知的删除状态筛选")
        return value

    def _normalize_tool_filters(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("查询参数必须是对象")
        normalized: dict[str, Any] = {}
        for key, limit in (
            ("umo", 1024),
            ("conversation_id", 256),
            ("platform_id", 128),
            ("session_id", 64),
        ):
            value = str(raw.get(key, "") or "").strip()
            if len(value) > limit:
                raise ValueError(f"{key} 过长")
            normalized[key] = value
        chat_type = str(raw.get("chat_type", "") or "").strip().lower()
        if chat_type not in ("", "group", "private"):
            raise ValueError("未知聊天类型")
        normalized["chat_type"] = chat_type
        persona_raw = raw.get("persona_id", None)
        if persona_raw in (None, "", "__all__"):
            normalized["persona_id"] = None
        elif persona_raw == "__empty__":
            normalized["persona_id"] = ""
        else:
            persona = str(persona_raw or "").strip()
            if len(persona) > 256:
                raise ValueError("persona_id 过长")
            normalized["persona_id"] = persona
        tool_name = str(raw.get("tool_name", "") or "").strip()
        if len(tool_name) > 200:
            raise ValueError("工具名不能超过 200 个字符")
        normalized["tool_name"] = tool_name
        normalized["since"] = self._normalize_local_datetime(raw.get("since"), "起始时间")
        normalized["until"] = self._normalize_local_datetime(raw.get("until"), "结束时间")
        if normalized["since"] and normalized["until"]:
            if normalized["since"] > normalized["until"]:
                raise ValueError("起始时间不能晚于结束时间")
        # 工具表与主表同一套软删除筛选
        normalized["deleted_state"] = self._normalize_deleted_state(
            raw.get("deleted_state")
        )
        normalized["page"] = _bounded_int(raw.get("page", 1), 1, 1_000_000, 1)
        normalized["page_size"] = _bounded_int(
            raw.get("page_size", self.default_page_size),
            1,
            self.max_page_size,
            self.default_page_size,
        )
        return normalized

    def _where_clause(
        self,
        filters: dict[str, Any],
        *,
        alias: str,
        soft_delete: bool = True,
        sender_column: str = "user_id",
        shadow: bool = False,
    ) -> tuple[list[str], dict[str, Any]]:
        """筛选条件 → SQL 片段。列名按读源切换（旧表没有 sender_id，用 user_id 顶）。"""
        conditions: list[str] = []
        params: dict[str, Any] = {}

        # 聊天类型：群聊 / 私聊 / 全部（平台侧就是 message_type）
        chat_type = filters.get("chat_type") or ""
        if chat_type == "group":
            conditions.append(f"{alias}.message_type = 'GroupMessage'")
        elif chat_type == "private":
            conditions.append(f"COALESCE({alias}.message_type, '') <> 'GroupMessage'")

        # QQ 号：群聊填群号、私聊填对方号，精确匹配会话标识
        session_id = filters.get("session_id") or ""
        if session_id:
            conditions.append(f"{alias}.session_id = :session_id")
            params["session_id"] = session_id

        # 发言者：纯数字按 QQ 号精确匹配，否则按昵称模糊匹配
        sender = filters.get("sender") or ""
        if sender:
            if sender.isdigit():
                conditions.append(f"{alias}.{sender_column} = :sender")
                params["sender"] = sender
            elif shadow:
                # 库里存的昵称可能只是号码（历史行），显示时是补出来的；
                # 所以按昵称筛要同时命中"该昵称所属的发送者"，否则看到的名字搜不到。
                conditions.append(
                    "("
                    f"COALESCE({alias}.sender_nickname, '') LIKE :sender ESCAPE '\\' "
                    f"OR {alias}.sender_id IN ("
                    "  SELECT sender_id FROM chat_memory_messages "
                    "   WHERE COALESCE(sender_nickname, '') LIKE :sender ESCAPE '\\' "
                    "     AND COALESCE(sender_id, '') <> '')"
                    ")"
                )
                params["sender"] = f"%{_escape_like(sender)}%"
            else:
                conditions.append(
                    f"COALESCE({alias}.sender_nickname, '') LIKE :sender ESCAPE '\\'"
                )
                params["sender"] = f"%{_escape_like(sender)}%"

        # 高级只读模式：按 persona 收窄。合并存储里 persona 在参与行上（同一条消息
        # 可能被不同 persona 的轮次各看一次），所以查参与行；旧历史直接查记录列。
        persona = filters.get("persona_id")
        persona_role = filters.get("persona_role") or ""
        persona_status = filters.get("persona_llm_status") or ""
        paired_only = bool(filters.get("paired_only"))
        if persona is None and (persona_role or persona_status or paired_only):
            # 三个高级条件都依附于 persona：不选 persona 时它们没有作用点。静默丢弃等于
            # "下拉可点可改、结果没筛"，用户看不出自己没筛成功——直接报错更诚实。
            raise ValueError("角色/状态/仅成对筛选需要先选择 persona")
        if persona is not None:
            if shadow:
                extra = ""
                if persona_role:
                    extra += " AND pb.role = :persona_role"
                    params["persona_role"] = persona_role
                conditions.append(
                    "(EXISTS (SELECT 1 FROM chat_memory_message_bots pb "
                    f"WHERE pb.msg_uid = {alias}.msg_uid "
                    "AND COALESCE(pb.persona_id, '') = :persona_id"
                    f"{extra}) "
                    f"OR ({alias}.msg_uid IS NULL "
                    f"AND COALESCE({alias}.persona_id, '') = :persona_id))"
                )
                if persona_status:
                    # 该轮回复的状态：assistant 行按 (turn_id, role) 索引取，子查询只算一次
                    conditions.append(
                        "EXISTS (SELECT 1 FROM chat_memory_message_bots pb "
                        f"WHERE pb.msg_uid = {alias}.msg_uid "
                        "AND COALESCE(pb.persona_id, '') = :persona_id "
                        "AND pb.turn_id IN ("
                        "  SELECT turn_id FROM chat_memory_message_bots "
                        "   WHERE role = 'assistant' AND COALESCE(turn_id, '') <> '' "
                        "     AND COALESCE(llm_status, '') = :persona_llm_status))"
                    )
                    params["persona_llm_status"] = persona_status
                if filters.get("paired_only"):
                    # 可选：只看成对的消息（本轮必须有一条 assistant 行）。
                    # IN 子查询只算一次；用 JOIN 写法 + 缺 (turn_id, role) 索引时会退化成分钟级。
                    conditions.append(
                        "EXISTS (SELECT 1 FROM chat_memory_message_bots pb "
                        f"WHERE pb.msg_uid = {alias}.msg_uid "
                        "AND COALESCE(pb.persona_id, '') = :persona_id "
                        "AND pb.turn_id IN ("
                        "  SELECT turn_id FROM chat_memory_message_bots "
                        "   WHERE role = 'assistant' AND COALESCE(turn_id, '') <> ''))"
                    )
            else:
                condition = f"COALESCE({alias}.persona_id, '') = :persona_id"
                if persona_role:
                    condition += f" AND {alias}.role = :persona_role"
                    params["persona_role"] = persona_role
                conditions.append(condition)
            params["persona_id"] = persona

        if filters.get("content_kind"):
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each("
                f"CASE WHEN json_valid({alias}.content_kind) "
                f"THEN {alias}.content_kind ELSE '[]' END) WHERE value = :content_kind)"
            )
            params["content_kind"] = filters["content_kind"]
        if filters.get("event_type"):
            # json_valid 守卫：脏数据（空串/半截 JSON）会让 json_extract 抛 malformed，
            # 整条查询直接失败并被当成"库损坏"。与上面 content_kind 分支同一口径。
            conditions.append(
                "CASE WHEN json_valid({alias}.relation_data) "
                "THEN json_extract({alias}.relation_data, '$.event.type') END = :event_type"
                .format(alias=alias)
            )
            params["event_type"] = filters["event_type"]
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
        # 软删除（schema v7）：旧库无该列时调用方传 soft_delete=False 降级
        if not soft_delete:
            return conditions, params
        deleted_state = filters.get("deleted_state") or "visible"
        if deleted_state == "visible":
            conditions.append(f"{alias}.deleted_at IS NULL")
        elif deleted_state == "deleted":
            conditions.append(f"{alias}.deleted_at IS NOT NULL")
        return conditions, params

    def _normalize_filters(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("查询参数必须是对象")
        normalized: dict[str, Any] = {}

        # 只有这组筛选：聊天类型 / QQ号 / 发言者 / 内容类型 / 事件子类 / 时间 / 删除状态
        for key, limit in (("session_id", 64), ("sender", 64)):
            value = str(raw.get(key, "") or "").strip()
            if len(value) > limit:
                raise ValueError(f"{key} 过长")
            normalized[key] = value

        chat_type = str(raw.get("chat_type", "") or "").strip().lower()
        if chat_type not in ("", "group", "private"):
            raise ValueError("未知聊天类型")
        normalized["chat_type"] = chat_type

        keyword = str(raw.get("keyword", "") or "").strip()
        if len(keyword) > 200:
            raise ValueError("关键词不能超过 200 个字符")
        normalized["keyword"] = keyword

        kind = str(raw.get("content_kind", "") or "").strip()
        if kind and kind not in VALID_KINDS:
            raise ValueError("未知 content_kind")
        normalized["content_kind"] = kind

        event_type = str(raw.get("event_type", "") or "").strip()
        if event_type and event_type not in VALID_EVENT_TYPES:
            raise ValueError("未知事件子类")
        normalized["event_type"] = event_type

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
        # 高级只读模式：额外给每条消息附上"属于哪个轮次/什么状态"
        normalized["advanced"] = _as_bool(raw.get("advanced", False))
        # 高级模式可选：只看成对轮次（默认关：单边消息照样显示，只是标出来）
        normalized["paired_only"] = _as_bool(raw.get("paired_only", False))
        persona_role = str(raw.get("persona_role", "") or "").strip()
        if persona_role and persona_role not in VALID_ROLES:
            raise ValueError("未知角色")
        normalized["persona_role"] = persona_role
        persona_status = raw.get("persona_llm_status", None)
        if persona_status in (None, "", "__all__"):
            normalized["persona_llm_status"] = ""
        else:
            value = "" if persona_status in ("__empty__", "no_llm") else str(persona_status)
            if value not in VALID_STATUSES:
                raise ValueError("未知 llm_status")
            normalized["persona_llm_status"] = value

        normalized["since"] = self._normalize_local_datetime(raw.get("since"), "起始时间")
        normalized["until"] = self._normalize_local_datetime(raw.get("until"), "结束时间")
        if normalized["since"] and normalized["until"]:
            if normalized["since"] > normalized["until"]:
                raise ValueError("起始时间不能晚于结束时间")
        # 软删除（schema v7）：三态下拉 all / visible / deleted，默认只看未删除
        normalized["deleted_state"] = self._normalize_deleted_state(
            raw.get("deleted_state")
        )
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

    @staticmethod
    def _ui_event_sentence(
        row: sqlite3.Row,
        relation_data: dict,
        event: dict,
        display_name: str | None = None,
    ) -> str:
        """UI 展示用事件句子：昵称缺失时回退账号 ID（仅查询台可见，账号不进上下文）。"""
        ui_event = dict(event)
        event_type = str(ui_event.get("type") or "")
        # 事件的操作者就是这条 notice 的发送者：历史行里 operator 常是空（自己贴表情那类
        # 事件平台只给 self_id），列表里就变成「用 X 回应了 Y 的消息」这种没主语的句子。
        if not str(ui_event.get("operator") or "").strip():
            # 优先用显示用昵称（同发送者的其它消息里有名字时补出来的那个）——
            # 库里这行的 sender_nickname 可能只是号码，直接用会渲染成「10001 用…」。
            nick = str(display_name or "").strip()
            if not nick:
                nick = str(_row_value(row, "sender_nickname") or "").strip()
            if nick and not nick.isdigit():
                ui_event["operator"] = nick
            else:
                ui_event["operator"] = str(_row_value(row, "user_id") or "").strip()
        if event_type == "poke":
            if not str(ui_event.get("target") or "").strip():
                for item in relation_data.get("media") or []:
                    if (not isinstance(item, dict)
                            or str(item.get("kind") or "") != "poke"):
                        continue
                    name = str(item.get("name") or "").strip()
                    ui_event["target"] = name or str(item.get("id") or "").strip()
                    break
        return build_event_sentence(ui_event)

    def _serialize_record(
        self,
        row: sqlite3.Row,
        *,
        preview_limit: int,
        reply_targets: dict[tuple[str, str], dict[str, Any]] | None = None,
        media_archive: dict[str, dict[str, Any]] | None = None,
        advanced: dict[str, Any] | None = None,
        sender_name: str | None = None,
    ) -> dict[str, Any]:
        raw_content = str(row["content"] or "")
        nickname = self._resolved_nickname(row, sender_name)
        relation_data = _parse_relation_data(_row_value(row, "relation_data"))
        content_kind = _parse_content_kind(row["content_kind"])
        rendered_content = _render_content(
            raw_content, relation_data, content_kind, media_archive=media_archive
        )
        # 事件：有 event 结构时按自然语言句子展示（与上下文同一套模板，
        # UI 为纯文本、无 cm 标签）；老数据无 event 结构保持原摘要文本。
        if ("event" in content_kind or "system_event" in content_kind) and (
            isinstance(relation_data, dict)
        ):
            event = relation_data.get("event")
            if isinstance(event, dict):
                rendered_content = self._ui_event_sentence(
                    row, relation_data, event, display_name=nickname
                )
        # 纯图片消息：正文是 ⟦图片⟧ 占位时，对已归档图片输出缩略图标记
        # （前端据此加载内联缩略图）；未归档/旧数据保持占位文本。
        if rendered_content.strip() and not rendered_content.replace(
            "⟦图片⟧", ""
        ).strip():
            markers = []
            if isinstance(relation_data, dict) and media_archive:
                for item in relation_data.get("media") or []:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("kind") or "") != "image":
                        continue
                    media_id = str(item.get("id") or "")
                    if media_id in media_archive:
                        markers.append(f"⟦CM_IMG:{media_id}⟧")
            if markers:
                rendered_content = " ".join(markers)
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
                        "text": str((target or {}).get("content") or ""),
                    }
                else:
                    reply_view = {
                        "resolution": "snapshot",
                        "target": str(reply.get("target_nickname") or "").strip()
                        or "未知成员",
                        "text": str(reply.get("fallback_text") or ""),
                    }
        media_view = self._media_view(relation_data, media_archive)
        recalled_at_raw = _row_value(row, "recalled_at")
        deleted_at_raw = _row_value(row, "deleted_at")
        return {
            "record_id": int(row["id"]),
            "role": str(row["role"]),
            "content": rendered_content,
            "content_template": raw_content,
            "content_truncated": truncated,
            "umo": str(row["umo"]),
            "conversation_id": str(row["conversation_id"]),
            "user_id": str(row["user_id"]),
            # 列表头像用：合并存储里就是这条消息的实际发送者；旧历史只有 user 行有
            "sender_id": _optional_str(_row_value(row, "sender_id")),
            "message_id": _optional_str(_row_value(row, "message_id")),
            "pair_id": _optional_str(_row_value(row, "pair_id")),
            "turn_id": _optional_str(_row_value(row, "turn_id")),
            "llm_status": str(row["llm_status"] or ""),
            "recalled": bool(str(recalled_at_raw or "").strip()),
            "recalled_at": self._time_pair(recalled_at_raw)[1] if recalled_at_raw else "",
            # 软删除（schema v7）：deleted=True 表示被查询台单条软删除（/reset 是硬删除）
            "deleted": bool(str(deleted_at_raw or "").strip()),
            "deleted_at": self._time_pair(deleted_at_raw)[1] if deleted_at_raw else "",
            "content_kind": content_kind,
            "platform_id": _optional_str(_row_value(row, "platform_id")),
            "platform_name": _optional_str(_row_value(row, "platform_name")),
            "message_type": _optional_str(_row_value(row, "message_type")),
            "session_id": _optional_str(_row_value(row, "session_id")),
            "self_id": _optional_str(_row_value(row, "self_id")),
            "group_id": _optional_str(_row_value(row, "group_id")),
            "sender_nickname": nickname,
            "raw_timestamp": _row_value(row, "raw_timestamp"),
            # 合并存储不建这三列（写进 relation_data 了），这里两种来源都覆盖
            "at_id": _optional_str(_row_value(row, "at_id")) or _relation_link(
                relation_data, "at_id"
            ),
            "reply_id": _optional_str(_row_value(row, "reply_id")) or _relation_link(
                relation_data, "reply_id"
            ),
            "forward_id": _optional_str(_row_value(row, "forward_id")) or _relation_link(
                relation_data, "forward_id"
            ),
            "persona_id": _optional_str(_row_value(row, "persona_id")),
            "send_status": _optional_str(_row_value(row, "send_status")),
            "relation_data": relation_data,
            "reply_view": reply_view,
            "media_view": media_view,
            "created_at": created_at_local,
            "created_at_utc": created_at_utc,
            # 高级只读模式才有：这条消息在所选 persona 下的轮次与状态
            "advanced": advanced,
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


def _count_map_from_map(
    totals: dict[str, int], *, empty_label: str = "(empty)"
) -> dict[str, int]:
    """已按值聚合好的 {值: 计数} → 归一后的计数映射（空值换 empty_label）。

    overview 里 role 与 llm_status 共用一次二维扫描，各自的边际分布在这里还原。
    """
    result: dict[str, int] = {}
    for key, amount in totals.items():
        result[str(key or empty_label)] = int(amount)
    return result


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
    event = parsed.get("event")
    if event is not None and not isinstance(event, dict):
        event = None
    result = {
        "v": 1,
        "mentions": mentions,
        "reply": reply,
        "media": media,
        "event": event,
    }
    # 合并存储不建 at_id/reply_id/forward_id 三列（写进 relation_data 了）：原样带出来，
    # 旧表那三列仍在，两条路都能取到（序列化时优先列、其次这里）。
    for key in ("at_id", "reply_id", "forward_id"):
        value = str(parsed.get(key) or "").strip()
        if value:
            result[key] = value
    return result


def _relation_link(relation_data: Any, key: str) -> str | None:
    """从 relation_data 里取 @/引用/转发 id（没有就 None）。"""
    if not isinstance(relation_data, dict):
        return None
    value = str(relation_data.get(key) or "").strip()
    return value or None


def _clean_inline_name(value: Any) -> str:
    """文件名进占位符的净化：去控制字符与 ⟦⟧ 结构符；不截断（与上下文里的文件名一致）。"""
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch == "\t")
    return text.replace("⟦", "").replace("⟧", "").strip()


def _ui_media_label_of(item: Any) -> str:
    """单个 media 条目 → UI 占位标签（emoji 带中文名，其余走 MEDIA_KIND_LABELS）。"""
    kind = str(item.get("kind") or "") if isinstance(item, dict) else ""
    if kind == "emoji":
        name = face_name(str(item.get("id") or ""))
        if name:
            return f"⟦表情:{name}⟧"
    return MEDIA_KIND_LABELS.get(kind, "⟦媒体⟧")


def _render_content(
    template: str,
    relation_data: dict[str, Any] | None,
    content_kind: list[str] | None = None,
    media_archive: dict[str, dict[str, Any]] | None = None,
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
        item = media[index]
        kind = str(item.get("kind") or "")
        if kind == "image":
            # 已归档的图片输出缩略图标记（⟦CM_IMG:<id>⟧），前端据此加载缩略图；
            # 未归档保持 ⟦图片⟧ 占位。
            media_id = str(item.get("id") or "")
            if media_archive and media_id in media_archive:
                return f"⟦CM_IMG:{media_id}⟧"
        elif kind == "emoji":
            name = face_name(str(item.get("id") or ""))
            if name:
                return f"⟦表情:{name}⟧"
        elif kind == "file":
            name = _clean_inline_name(item.get("name"))
            return f"⟦{name}⟧" if name else MEDIA_KIND_LABELS.get(kind, "⟦媒体⟧")
        return MEDIA_KIND_LABELS.get(kind, "⟦媒体⟧")

    value = AT_TOKEN_RE.sub(replace, template)
    value = MEDIA_TOKEN_RE.sub(replace_media, value)
    value = value.replace(_SENTINEL_LEFT, "⟦").replace(_SENTINEL_RIGHT, "⟧")
    if not value.strip() and media:
        labels = [
            _ui_media_label_of(item)
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


def _as_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔归一化：字符串 "false"/"0"/"no" 不得被判成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


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
