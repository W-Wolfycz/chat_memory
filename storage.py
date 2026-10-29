"""chat_memory — 异步 SQLite 存储层（旧表 schema v8）。

Schema 说明：
- ``llm_status``：LLM 配对状态（单值）。'' = 默认（未走 LLM），
  其他取值：'llm_pending' / 'llm_success' / 'proactive' / 'orphan'。
- ``content_kind``：消息内容形态（JSON 数组字符串，如 '["text","image"]'）。
  支持值：'text' / 'image' / 'video' / 'voice' / 'file' / 'emoji' / 'forward' / 'poke'
  / 'event'（旧值 'system_event' 仅兼容读取）。空数组 '[]' 表示 empty（如纯 @ 无文本）。
- ``at_id`` / ``reply_id`` / ``forward_id``：上下文引用 ID，仅在对应组件出现时存。
- ``turn_id``：内部轮次 ID，新记录不依赖平台 message_id 即可配对。
- ``send_status``：assistant 发送流程状态（prepared / send_attempted），不表示平台回执。

缺列（1.4.0 之前的 schema）不做猜测性字段映射也不自动迁移：``init_db`` 直接抛错说明缺哪一列，
由用户按 README 的升级步骤处理；已有基础列的测试版 schema 则增量补齐后续新增列。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from astrbot.api import logger

from . import shadow_read, shared_store
from .models import VALID_LLM_STATUSES, VALID_SEND_STATUSES
from .relation_codec import parse_relation_data, render_content_template


_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS chat_memory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    message_id      TEXT,
    pair_id         TEXT,
    llm_status      TEXT NOT NULL DEFAULT '',
    content_kind    TEXT NOT NULL DEFAULT '[]',
    platform_id     TEXT,
    platform_name   TEXT,
    message_type    TEXT,
    session_id      TEXT,
    self_id         TEXT,
    group_id        TEXT,
    sender_nickname TEXT,
    raw_timestamp   INTEGER,
    at_id           TEXT,
    reply_id        TEXT,
    forward_id      TEXT,
    persona_id      TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    turn_id         TEXT,
    send_status     TEXT NOT NULL DEFAULT '',
    relation_data   TEXT,
    recalled_at     DATETIME,
    deleted_at      DATETIME
)"""

# 工具调用独立存档（schema v4）：不回放进主表的 user/assistant 配对，
# 只在 takeover 时按轮次渲染成 OpenAI 格式（assistant tool_calls + role=tool）。
_CREATE_TOOL_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS chat_memory_tool_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    call_index      INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_args       TEXT NOT NULL DEFAULT '',
    tool_result     TEXT NOT NULL DEFAULT '',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      DATETIME
)"""

# 媒体归档（schema v5）：user 侧媒体落盘后登记，CM_UI 据此回看。
# 只在归档成功时插行；失败不落行。文件路径不落库：由 file_name + created_at
# 推导（media/<YYYYMM>/<内容哈希>.<ext>）；v8 起文件名是内容寻址的哈希，不含隐私标识。
_CREATE_MEDIA_ARCHIVE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS chat_memory_media_archive (
    media_id        TEXT PRIMARY KEY,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    content_hash    TEXT,
    ext             TEXT NOT NULL,
    mime_type       TEXT,
    size_bytes      INTEGER NOT NULL,
    created_at      DATETIME NOT NULL
)"""

_MEDIA_ARCHIVE_INDEX_DEFINITIONS = {
    "ix_cm_media_created": (
        "CREATE INDEX IF NOT EXISTS ix_cm_media_created "
        "ON chat_memory_media_archive (created_at)"
    ),
    "ix_cm_media_cid_time": (
        "CREATE INDEX IF NOT EXISTS ix_cm_media_cid_time "
        "ON chat_memory_media_archive (conversation_id, created_at)"
    ),
    "ix_cm_media_turn": (
        "CREATE INDEX IF NOT EXISTS ix_cm_media_turn "
        "ON chat_memory_media_archive (turn_id)"
    ),
    # 内容哈希：同一张图被多个实例各存一份时按它去重（也是"这图出现过几次"的查询键）
    "ix_cm_media_hash": (
        "CREATE INDEX IF NOT EXISTS ix_cm_media_hash "
        "ON chat_memory_media_archive (content_hash)"
    ),
}

_SCHEMA_VERSION = 8


async def _mirror_record_payload(
    session,
    payload: dict,
    platform_timestamp,
    platform_uid,
    post_type: str,
) -> bool:
    """影子专用镜像（不写旧表）：供平台回显使用。"""
    return await shared_store.mirror_record(
        session,
        {
            **payload,
            "pts": platform_timestamp,
            "puid": platform_uid,
            "post_type": post_type,
        },
    )

# 现有库必须已具备的列：缺失说明是 1.4.0 之前的 schema，不再自动迁移。
# 这里必须覆盖**索引与 SELECT 实际依赖的列**：只查核心列的话，缺列不会走到下面那条
# 友好的 RuntimeError，而是在建索引/查询时中途报 `no such column`，用户看不出怎么修。
_REQUIRED_COLUMNS = {
    "id", "umo", "conversation_id", "user_id", "role", "content", "llm_status",
    "content_kind", "created_at", "persona_id", "turn_id", "send_status",
    "relation_data", "message_id", "pair_id", "platform_id", "group_id",
    "session_id", "message_type", "self_id", "at_id", "reply_id", "forward_id",
    "raw_timestamp", "deleted_at", "recalled_at",
}

_INDEX_DEFINITIONS = {
    "ix_cm_umo_cid_user": (
        "CREATE INDEX IF NOT EXISTS ix_cm_umo_cid_user "
        "ON chat_memory_records (umo, conversation_id, user_id)"
    ),
    "ix_cm_created": (
        "CREATE INDEX IF NOT EXISTS ix_cm_created "
        "ON chat_memory_records (created_at)"
    ),
    "ix_cm_pair_id": (
        "CREATE INDEX IF NOT EXISTS ix_cm_pair_id "
        "ON chat_memory_records (pair_id)"
    ),
    "ix_cm_platform_group_time": (
        "CREATE INDEX IF NOT EXISTS ix_cm_platform_group_time "
        "ON chat_memory_records (platform_id, group_id, created_at)"
    ),
    "ix_cm_platform_user_time": (
        "CREATE INDEX IF NOT EXISTS ix_cm_platform_user_time "
        "ON chat_memory_records (platform_id, user_id, created_at)"
    ),
    # 撤回/表情回应的 message_id 本地关联（此前无索引，每条通知全表扫描）
    "ix_cm_umo_message_id": (
        "CREATE INDEX IF NOT EXISTS ix_cm_umo_message_id "
        "ON chat_memory_records (umo, message_id)"
    ),
    "ix_cm_llm_status": (
        "CREATE INDEX IF NOT EXISTS ix_cm_llm_status "
        "ON chat_memory_records (llm_status)"
    ),
    "ix_cm_umo_role_time": (
        "CREATE INDEX IF NOT EXISTS ix_cm_umo_role_time "
        "ON chat_memory_records (umo, role, created_at)"
    ),
    "ix_cm_persona": (
        "CREATE INDEX IF NOT EXISTS ix_cm_persona "
        "ON chat_memory_records (persona_id)"
    ),
    # 软删除过滤（schema v7）：所有读路径都带 deleted_at IS NULL
    "ix_cm_umo_deleted": (
        "CREATE INDEX IF NOT EXISTS ix_cm_umo_deleted "
        "ON chat_memory_records (umo, deleted_at)"
    ),
    "ux_cm_turn_role": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cm_turn_role "
        "ON chat_memory_records (turn_id, role) "
        "WHERE turn_id IS NOT NULL AND turn_id <> ''"
    ),
}

_TOOL_INDEX_DEFINITIONS = {
    "ux_cm_tool_turn_seq": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cm_tool_turn_seq "
        "ON chat_memory_tool_records (turn_id, call_index)"
    ),
    "ix_cm_tool_umo_cid_time": (
        "CREATE INDEX IF NOT EXISTS ix_cm_tool_umo_cid_time "
        "ON chat_memory_tool_records (umo, conversation_id, created_at)"
    ),
    # 软删除过滤（schema v7）：查询台单条软删除记录后的读路径过滤
    "ix_cm_tool_deleted": (
        "CREATE INDEX IF NOT EXISTS ix_cm_tool_deleted "
        "ON chat_memory_tool_records (umo, deleted_at)"
    ),
}


def schema_statements(
    existing_columns: set, existing_tool_columns: set, existing_media_columns: set = None
) -> list:
    """按**执行顺序**返回建表 / 迁移 / 建索引语句（纯函数，便于测试顺序）。

    顺序约定（踩过的坑）：**必须先 `ALTER TABLE ADD COLUMN` 再建该列的索引**，
    否则存量旧库执行 `CREATE INDEX ... (umo, deleted_at)` 会因缺列直接失败，
    插件加载阶段就崩。传入的两组列名来自 `PRAGMA table_info`；空集合表示建新表
    （新表定义里已含新列，无需 ALTER）。
    """
    statements: list = [_CREATE_TABLE_SQL]
    if existing_columns and "recalled_at" not in existing_columns:
        # v5 → v6：撤回感知需要标记被撤回的原消息（NULL=未撤回）
        statements.append(
            "ALTER TABLE chat_memory_records ADD COLUMN recalled_at DATETIME"
        )
    if existing_columns and "deleted_at" not in existing_columns:
        # v6 → v7：软删除标记（NULL=正常；非空=被查询台单条标记删除）
        statements.append(
            "ALTER TABLE chat_memory_records ADD COLUMN deleted_at DATETIME"
        )
    statements.extend(_INDEX_DEFINITIONS.values())
    # 工具调用表（schema v4 起）：独立存档，不进主表配对。
    statements.append(_CREATE_TOOL_TABLE_SQL)
    if existing_tool_columns and "deleted_at" not in existing_tool_columns:
        statements.append(
            "ALTER TABLE chat_memory_tool_records ADD COLUMN deleted_at DATETIME"
        )
    statements.extend(_TOOL_INDEX_DEFINITIONS.values())
    # 媒体归档表（schema v5 起）：CREATE IF NOT EXISTS 幂等，无数据迁移。
    statements.append(_CREATE_MEDIA_ARCHIVE_TABLE_SQL)
    if existing_media_columns and "content_hash" not in existing_media_columns:
        # v7 → v8：媒体内容哈希（同图多实例去重 + 出现次数查询）
        statements.append(
            "ALTER TABLE chat_memory_media_archive ADD COLUMN content_hash TEXT"
        )
    statements.extend(_MEDIA_ARCHIVE_INDEX_DEFINITIONS.values())
    return statements


async def _shadow_step_is_noop(conn, statement: str) -> tuple[bool, str]:
    """影子层迁移步骤是否"已经生效过"（只对可重复执行的步骤判定）。

    ``ALTER TABLE <表> DROP COLUMN <列>`` 没有 ``IF EXISTS`` 写法：上次迁移中途失败后
    重跑会报 ``no such column``。这里先查一次 ``PRAGMA table_info``，列已不在就跳过；
    其余语句一律照常执行（不能靠 try/except 吞异常，那会把真正的结构错误一起藏掉）。
    """
    value = " ".join(str(statement or "").split())
    upper = value.upper()
    if not (upper.startswith("ALTER TABLE") and " DROP COLUMN " in upper):
        return False, ""
    parts = value.split()
    if len(parts) < 6:
        return False, ""
    table, column = parts[2], parts[5]
    if not (table.replace("_", "").isalnum() and column.replace("_", "").isalnum()):
        return False, ""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    columns = {str(row[1]) for row in result.fetchall()}
    if column not in columns:
        return True, f"{table}.{column} 已不存在"
    return False, ""


class DBManager:
    def __init__(
        self,
        data_dir: Path,
        tz: Optional[ZoneInfo] = None,
        shadow: bool = False,
    ):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "chat_memory.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.engine = create_async_engine(self.db_url, echo=False)
        self.async_session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()
        # 输出时区：created_at 存 UTC naive，查询返回时转此 tz naive；None 则原样输出 UTC
        self._tz = tz
        # 合并存储（影子双写，阶段一）：只写新表，读路径仍走旧表。
        self.shadow_enabled = bool(shadow)
        self.shadow_stats = {"writes": 0, "skipped": 0, "failures": 0, "last_error": ""}
        # 影子写出现的"非运行期异常"（代码/结构问题）只 ERROR 报一次，避免刷屏
        self._shadow_code_error_logged = False
        self._register_pragmas()

    async def _shadow_write(self, operation) -> bool:
        """尽力而为地镜像到合并存储表；返回是否**真的写入**了影子行。

        影子写失败必须完全隔离：只累加计数 + 记异常类型（异常串可能带 SQL 参数
        ＝聊天正文），绝不让旧表的主写入链路失败。

        返回假（数据不足被跳过 / 影子层关闭 / 写异常）时调用方**不得**当成功上报：
        平台回显曾因此记成 ``echo/fetched`` 而实际一行没落，线上看着像通道正常。
        """
        if not self.shadow_enabled:
            return False
        try:
            async with self.async_session() as session:
                mirrored = await operation(session)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - 影子链路整体兜底
            self.shadow_stats["failures"] += 1
            self.shadow_stats["last_error"] = type(exc).__name__
            # DBAPI/驱动类错误是可重试的运行期故障；其它（NameError/AttributeError/
            # ProgrammingError＝代码或结构问题）第一次就以 ERROR 报一次——它们不会自愈，
            # 只记 DEBUG 会让"镜像从来没写全"一路静默到切读那天。
            module = type(exc).__module__ or ""
            if not module.startswith("sqlalchemy.exc") and not isinstance(exc, OSError):
                if not self._shadow_code_error_logged:
                    self._shadow_code_error_logged = True
                    logger.error(
                        "[ChatMemory] 影子写入出现非运行期异常"
                        f"（不会再自愈，请查代码/结构）: {type(exc).__name__}"
                    )
            shared_store.log_shadow_failure(self.shadow_stats["failures"], exc)
            return False
        if mirrored is False:
            self.shadow_stats["skipped"] += 1
            return False
        self.shadow_stats["writes"] += 1
        return True

    def _register_pragmas(self):
        """开 WAL + synchronous=NORMAL：群聊高频写入下避免 database is locked。

        WAL 允许读写并发，NORMAL 同步级别配合 WAL 在性能与耐久性间取平衡
        （崩溃时仅丢最后一轮事务，对聊天存档可接受）。
        """

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, conn_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # busy_timeout=5000ms：群聊高频写入时遇到锁等待，最多等 5s 而非立即报 locked
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def init_db(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with self.engine.begin() as conn:
                # 1.4.0 之前的旧 schema 不再迁移（市场首发 1.4.0）：
                # 缺列时直接给出可诊断的失败，而不是静默增量迁移。
                result = await conn.execute(text("PRAGMA table_info(chat_memory_records)"))
                existing_cols = {row[1] for row in result.fetchall()}
                missing = (
                    sorted(_REQUIRED_COLUMNS - existing_cols) if existing_cols else []
                )
                if missing:
                    raise RuntimeError(
                        "检测到 1.4.0 之前的旧数据库 schema（缺少列："
                        + ", ".join(missing)
                        + "）。请先用 1.4.0 升级该数据库，或移走旧库文件后重启。"
                    )
                tool_info = await conn.execute(
                    text("PRAGMA table_info(chat_memory_tool_records)")
                )
                existing_tool_cols = {row[1] for row in tool_info.fetchall()}
                media_info = await conn.execute(
                    text("PRAGMA table_info(chat_memory_media_archive)")
                )
                existing_media_cols = {row[1] for row in media_info.fetchall()}
                # 建表 → 补列（v5→v6 recalled_at、v6→v7 deleted_at、v7→v8 content_hash）
                # → 建索引：顺序写死在 schema_statements 里并有单测守卫（索引不得早于补列）。
                if existing_cols:
                    # ux_cm_turn_role 是唯一索引：1.6.0 之前没有该约束，存量库里**可能**
                    # 已有重复 (turn_id, role)。直接建索引会在本事务里抛 IntegrityError，
                    # 插件加载阶段就崩。索引已存在时跳过这步（无重复可谈，别每次加载都扫）。
                    has_unique = await conn.execute(
                        text(
                            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                            "AND name = 'ux_cm_turn_role'"
                        )
                    )
                    if has_unique.first() is None:
                        dup_result = await conn.execute(
                            text(
                                "SELECT COUNT(*) FROM chat_memory_records "
                                "WHERE COALESCE(turn_id, '') <> '' AND id NOT IN ("
                                "  SELECT MIN(id) FROM chat_memory_records "
                                "  WHERE COALESCE(turn_id, '') <> '' "
                                "  GROUP BY turn_id, role)"
                            )
                        )
                        dup_rows = int(dup_result.scalar() or 0)
                        if dup_rows:
                            logger.warning(
                                "[ChatMemory] 旧库存在 %d 条重复 (turn_id, role) 记录："
                                "保留每轮最早的一条、删除其余，以便建立唯一约束",
                                dup_rows,
                            )
                            await conn.execute(
                                text(
                                    "DELETE FROM chat_memory_records "
                                    "WHERE COALESCE(turn_id, '') <> '' AND id NOT IN ("
                                    "  SELECT MIN(id) FROM chat_memory_records "
                                    "  WHERE COALESCE(turn_id, '') <> '' "
                                    "  GROUP BY turn_id, role)"
                                )
                            )
                for statement in schema_statements(
                    existing_cols, existing_tool_cols, existing_media_cols
                ):
                    await conn.execute(text(statement))
                # 防止备份表残留的同名索引再次让 IF NOT EXISTS 静默跳过。
                for index_name, table_name in [
                    *((name, "chat_memory_records")
                      for name in _INDEX_DEFINITIONS),
                    *((name, "chat_memory_tool_records")
                      for name in _TOOL_INDEX_DEFINITIONS),
                    *((name, "chat_memory_media_archive")
                      for name in _MEDIA_ARCHIVE_INDEX_DEFINITIONS),
                ]:
                    index_result = await conn.execute(
                        text(
                            "SELECT tbl_name FROM sqlite_master "
                            "WHERE type = 'index' AND name = :name"
                        ),
                        {"name": index_name},
                    )
                    if index_result.scalar() != table_name:
                        raise RuntimeError(
                            f"ChatMemory index {index_name} 未绑定到 {table_name}"
                        )
                if self.shadow_enabled:
                    # 合并存储影子表：只增不改，旧表结构不受影响；版本记在
                    # chat_memory_shadow_meta，不动旧库 user_version。结构升级（版本不等）
                    # 时按迁移步骤走；不认识的旧结构丢弃重建，但**切读窗口沿用**。
                    carried_start = ""
                    migrate_trace = ""
                    meta_exists = await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'chat_memory_shadow_meta'"
                        )
                    )
                    if meta_exists.first():
                        current = await conn.execute(
                            text(
                                "SELECT value FROM chat_memory_shadow_meta "
                                "WHERE key = 'schema_version'"
                            )
                        )
                        recorded = str(current.scalar() or "")
                        if recorded != str(shared_store.SHADOW_SCHEMA_VERSION):
                            # 认识的旧结构走**迁移**（数据与切读窗口都保住）；不认识的
                            # 才丢弃重建。
                            steps = shared_store.shadow_migration_statements(recorded)
                            if steps is None:
                                # 重建等于把镜像清空，但**切读窗口沿用**：窗口记的是"双写
                                # 连续启用了多久"，不该因为一次结构升级就让人重等 7 天。
                                kept = await conn.execute(
                                    text(
                                        "SELECT value FROM chat_memory_shadow_meta "
                                        "WHERE key = 'dual_write_started_at'"
                                    )
                                )
                                carried_start = str(kept.scalar() or "")
                                for statement in shared_store.drop_shadow_statements():
                                    await conn.execute(text(statement))
                                migrate_trace = "rebuild:unknown_version"
                            else:
                                for statement in steps:
                                    # `ALTER TABLE … DROP COLUMN` 不是幂等的：上次迁移
                                    # 中途失败后重跑时列可能已经没了。先查列再决定执行，
                                    # 而不是靠吞异常（那会把真正的结构错误一起藏掉）。
                                    skip, reason = await _shadow_step_is_noop(
                                        conn, statement
                                    )
                                    if skip:
                                        logger.debug(
                                            "[ChatMemory] 迁移步骤跳过（%s）: %s",
                                            reason, statement.strip()[:80],
                                        )
                                        continue
                                    await conn.execute(text(statement))
                                migrate_trace = (
                                    f"migrated:{recorded}->{shared_store.SHADOW_SCHEMA_VERSION}"
                                )
                                # 迁移必须留一条 INFO：用户/维护者要能从日志确认
                                # "这次加载到底迁移了没有、跑了几步"（以前只写进 meta 表，
                                # 日志里一片空白，没法判断）。
                                logger.info(
                                    "[ChatMemory] 合并存储结构迁移完成：%s（%d 步，"
                                    "切读窗口沿用）",
                                    migrate_trace, len(steps),
                                )
                    for statement in shared_store.shadow_schema_statements():
                        await conn.execute(text(statement))
                    # 版本号与迁移痕迹都在表建好之后再写（重建会把 meta 一起删掉）
                    await conn.execute(
                        text(
                            "INSERT INTO chat_memory_shadow_meta (key, value) "
                            "VALUES ('schema_version', :version) "
                            "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                        ),
                        {"version": str(shared_store.SHADOW_SCHEMA_VERSION)},
                    )
                    if migrate_trace:
                        await conn.execute(
                            text(
                                "INSERT INTO chat_memory_shadow_meta (key, value) "
                                "VALUES ('shadow_migrate', :detail) "
                                "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                            ),
                            {"detail": migrate_trace},
                        )
                    # 双写起点：只在第一次建立影子层时写一次。有历史数据就取最早的一条
                    # （从旧 beta 升上来时窗口接着算，不白扔）；全新库取当前时刻。
                    if carried_start:
                        await conn.execute(
                            text(
                                "INSERT OR IGNORE INTO chat_memory_shadow_meta (key, value) "
                                "VALUES ('dual_write_started_at', :start)"
                            ),
                            {"start": carried_start},
                        )
                    await conn.execute(
                        text(
                            "INSERT OR IGNORE INTO chat_memory_shadow_meta (key, value) "
                            "VALUES ('dual_write_started_at', COALESCE("
                            "  (SELECT MIN(created_at) FROM chat_memory_message_bots),"
                            "  (SELECT MIN(created_at) FROM chat_memory_messages),"
                            "  :now))"
                        ),
                        {"now": _utc_now_str()},
                    )
                await conn.execute(text(f"PRAGMA user_version = {_SCHEMA_VERSION}"))
            self._initialized = True

    async def dual_write_window_days(self) -> float:
        """双写已经跑了几天（影子层起点 → 现在）；没有起点或影子关闭时返回 -1。

        切读门槛用（见 ``main._SHADOW_READ_MIN_DAYS``）：一份新装的库在跑满门槛之前
        只写不读，避免拿只有几小时的数据就切读。读失败按 -1 处理＝未满。
        """
        if not self.shadow_enabled:
            return -1.0
        try:
            await self.init_db()
            async with self.async_session() as session:
                result = await session.execute(
                    text(
                        "SELECT value FROM chat_memory_shadow_meta "
                        "WHERE key = 'dual_write_started_at'"
                    )
                )
                started = str(result.scalar() or "")
            if not started:
                return -1.0
            begin = datetime.fromisoformat(started.replace("T", " "))
            return (datetime.now(timezone.utc).replace(tzinfo=None) - begin).total_seconds() / 86400
        except Exception:  # noqa: BLE001 - 取不到就当未满（宁可晚切，不可早切）
            return -1.0

    async def upsert_group_names(
        self, rows: list[tuple[str, str, str]]
    ) -> int:
        """写入群名快照 ``[(platform_name, session_id, group_name)]``，返回受影响行数。

        值没变时不算受影响（SQL 自带 ``WHERE`` 守卫），所以反复刷新只会动真正改名的群。
        群名是展示用的糖：失败只记 DEBUG，不影响任何主流程。
        """
        items = [
            {
                "platform_name": str(pname),
                "session_id": str(sid),
                "group_name": str(name),
                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }
            for pname, sid, name in rows
            if str(pname) and str(sid) and str(name)
        ]
        if not items or not self.shadow_enabled:
            return 0
        try:
            await self.init_db()
            async with self.async_session() as session:
                affected = 0
                for item in items:
                    result = await session.execute(
                        shared_store.UPSERT_GROUP_NAME_SQL, item
                    )
                    affected += int(result.rowcount or 0)
                await session.commit()
                return affected
        except Exception as exc:  # noqa: BLE001 - 群名写不进去就当没有
            logger.debug(f"[ChatMemory] 群名落库失败: {type(exc).__name__}")
            return 0

    async def load_group_names(self) -> dict[str, str]:
        """读取群名快照 ``{群号: 群名}``（跨平台合并；查询台只按群号显示）。"""
        if not self.shadow_enabled:
            return {}
        try:
            await self.init_db()
            async with self.async_session() as session:
                result = await session.execute(shared_store.SELECT_GROUP_NAMES_SQL)
                return {
                    str(row[0]): str(row[1])
                    for row in result.all()
                    if row and row[0] and row[1]
                }
        except Exception as exc:  # noqa: BLE001 - 读不到就是没有群名
            logger.debug(f"[ChatMemory] 群名读取失败: {type(exc).__name__}")
            return {}

    async def backfill_turn_persona(self) -> int:
        """补齐"同轮有 persona 但本行空着"的影子行（影子专用，尽力而为）。"""
        if not self.shadow_enabled:
            return 0
        try:
            await self.init_db()
            async with self.async_session() as session:
                filled = await shared_store.backfill_turn_persona(session)
                await session.commit()
                return int(filled)
        except Exception as exc:  # noqa: BLE001 - 回填失败不得影响任何流程
            logger.debug(f"[ChatMemory] persona 回填失败: {type(exc).__name__}")
            return 0

    async def shadow_conversation_id(
        self, *, platform_id: str, message_type: str, session_id: str
    ) -> str:
        """按会话找回 conversation_id（push 回显没有事件、冷启动时内存表可能还是空的）。

        只看**同一个实例**在该会话里已有的参与行——它就是这个实例眼中的 conversation_id。
        """
        if not (self.shadow_enabled and platform_id and message_type and session_id):
            return ""
        try:
            await self.init_db()
            async with self.async_session() as session:
                result = await session.execute(
                    text(
                        "SELECT b.conversation_id FROM chat_memory_message_bots b "
                        "JOIN chat_memory_messages m ON m.msg_uid = b.msg_uid "
                        "WHERE b.platform_id = :platform_id AND m.message_type = :message_type "
                        "AND m.session_id = :session_id AND b.conversation_id <> '' "
                        "ORDER BY b.id DESC LIMIT 1"
                    ),
                    {
                        "platform_id": str(platform_id),
                        "message_type": str(message_type),
                        "session_id": str(session_id),
                    },
                )
                row = result.first()
                return str(row[0]) if row is not None else ""
        except Exception:  # noqa: BLE001 - 查不到就按"没有 cid"处理，交给拉取通道
            return ""

    async def insert(
        self,
        umo: str,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        message_id: Optional[str] = None,
        pair_id: Optional[str] = None,
        llm_status: str = "",
        content_kind: Optional[list[str]] = None,
        platform_id: Optional[str] = None,
        platform_name: Optional[str] = None,
        message_type: Optional[str] = None,
        session_id: Optional[str] = None,
        self_id: Optional[str] = None,
        group_id: Optional[str] = None,
        sender_nickname: Optional[str] = None,
        raw_timestamp: Optional[int] = None,
        at_id: Optional[str] = None,
        reply_id: Optional[str] = None,
        forward_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        send_status: str = "",
        relation_data: Optional[str] = None,
        update_user_llm_status: Optional[str] = None,
        platform_timestamp: Optional[int] = None,
        platform_uid: Optional[str] = None,
        post_type: str = "",
        shadow_only: bool = False,
        claim_turn: bool = False,
    ) -> Optional[bool]:
        """写入一条记录。

        ``shadow_only=True``：**只**镜像到合并存储影子表，不写旧表/不更新旧表状态。
        用于"影子层专属"的事实：**自己发言的实际形态**——它由两条供货通道之一带回
        （push＝NapCat 回显，主通道；pull＝发送后拉群历史，兜底），旧表已有发送路径写的
        assistant 行，重复写会让现有读路径看到两份。

        返回值：普通路径返回**旧表是否真的插入了行**（``INSERT OR IGNORE`` 遇到唯一键
        冲突时 rowcount=0，绝不能再当成成功——那会让上层把"这轮没写进去"记成写成功、
        assistant 照旧升级 llm_status）；``shadow_only=True`` 时返回影子行是否真的落库
        （``False`` = 数据不足被归一拒绝/影子层关闭/写异常）。

        ``claim_turn=True`` 表示"这是本实例自己发出的一条"（push/pull 供货路径）：
        调拨层会把它记进逻辑表并并回本轮计划行的组；否则只写物理行与参与行。
        """
        await self.init_db()
        if not umo or not conversation_id:
            raise ValueError("insert requires umo/conversation_id")
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported role: {role}")
        if llm_status not in VALID_LLM_STATUSES:
            raise ValueError(f"unsupported llm_status: {llm_status}")
        if send_status not in VALID_SEND_STATUSES:
            raise ValueError(f"unsupported send_status: {send_status}")
        if role == "user" and send_status:
            raise ValueError("user record must not set send_status")
        kind_json = json.dumps(content_kind or [], ensure_ascii=False)
        payload = {
            "umo": umo,
            "cid": conversation_id,
            "uid": user_id,
            "role": role,
            "content": content,
            "mid": message_id,
            "pid": pair_id,
            "lstatus": llm_status,
            "ckind": kind_json,
            "pid_plat": platform_id,
            "pname": platform_name,
            "mtype": message_type,
            "sid": session_id,
            "self_id": self_id,
            "gid": group_id,
            "nick": sender_nickname,
            "rts": raw_timestamp,
            "at_id": at_id,
            "reply_id": reply_id,
            "fwd_id": forward_id,
            "per_id": persona_id,
            "now": datetime.now(timezone.utc).replace(tzinfo=None),
            "turn_id": turn_id,
            "send_status": send_status,
            "relation_data": relation_data,
            "claim_turn": bool(claim_turn),
        }
        if shadow_only:
            # 平台回显：只镜像到影子表（旧表已有发送路径写的 assistant 行，不能写第二份）
            return await self._shadow_write(
                lambda session: _mirror_record_payload(
                    session, payload, platform_timestamp, platform_uid, post_type
                )
            )
        async with self.async_session() as session:
            # assistant 最终写入时，把对应 user 状态升级放在同一事务中。
            # 1.0.0 的实时写入链路统一使用 turn_id；pair_id 仅保留给历史查询。
            if role == "assistant" and update_user_llm_status is not None:
                if not turn_id:
                    raise ValueError("assistant finalize requires turn_id")
                await session.execute(
                    text(
                        "UPDATE chat_memory_records SET llm_status = :status "
                        "WHERE umo = :umo AND conversation_id = :cid "
                        "AND turn_id = :turn_id AND role = 'user' "
                        "AND deleted_at IS NULL"
                    ),
                    {
                        "status": update_user_llm_status,
                        "umo": umo,
                        "cid": conversation_id,
                        "turn_id": turn_id,
                    },
                )
            insert_result = await session.execute(
                text(
                    "INSERT OR IGNORE INTO chat_memory_records "
                    "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
                    "llm_status, content_kind, "
                    "platform_id, platform_name, message_type, session_id, self_id, "
                    "group_id, sender_nickname, raw_timestamp, "
                    "at_id, reply_id, forward_id, persona_id, created_at, "
                    "turn_id, send_status, relation_data) "
                    "VALUES (:umo, :cid, :uid, :role, :content, :mid, :pid, "
                    ":lstatus, :ckind, "
                    ":pid_plat, :pname, :mtype, :sid, :self_id, "
                    ":gid, :nick, :rts, :at_id, :reply_id, :fwd_id, :per_id, :now, "
                    ":turn_id, :send_status, :relation_data)"
                ),
                payload,
            )
            inserted_rows = int(insert_result.rowcount or 0)
            await session.commit()

        async def _mirror_record_and_status(session):
            """镜像记录本身；assistant 落库顺带把 user 行的 llm_status 提升也镜像过去。

            旧表这条状态提升是 insert() 内的内联 UPDATE（不走 update_llm_status_by_turn），
            **只挂钩后者会漏掉**——线上实测影子 user 行的 llm_status 全为空。
            """
            mirrored = await shared_store.mirror_record(
                session,
                {
                    **payload,
                    "pts": platform_timestamp,
                    "puid": platform_uid,
                    "post_type": post_type,
                },
            )
            if mirrored and update_user_llm_status is not None and turn_id:
                await shared_store.mirror_llm_status(
                    session, umo, conversation_id, turn_id, update_user_llm_status
                )
            return mirrored

        # 只有真的插进旧表的行才镜像：没落库（唯一键冲突）的行进合并存储就是凭空造事实。
        if inserted_rows:
            await self._shadow_write(_mirror_record_and_status)
        else:
            # INSERT OR IGNORE 把唯一键冲突静默吞掉了（典型：同 (turn_id, role) 重复写入）。
            # 必须让调用方看见：否则这轮 user 根本没落库，assistant 却会被标 llm_success。
            # 只记 id/状态，不带正文。
            logger.warning(
                "[ChatMemory] 记录未落库（唯一键冲突被忽略）: role=%s turn=%s send_status=%s",
                role, turn_id or "-", send_status or "-",
            )
        return bool(inserted_rows)

    async def mark_plan_send_attempted(
        self, umo: str, conversation_id: str, turn_id: str
    ) -> int:
        """发送流程结束后，把**没有实发行**的轮次计划成员标成 ``send_attempted``。

        被禁言、平台拒收、或别的插件清空结果时不产生任何平台消息，这一轮只剩计划成员；
        不标它永远停在 ``prepared``，和"还没开始发"无法区分。只在确实没有 assistant
        实发行时才更新，正常轮次不受影响。
        """
        if not self.shadow_enabled:
            return 0
        await self.init_db()
        affected = {"rows": 0}

        async def _mark(session):
            result = await session.execute(
                text(
                    "UPDATE chat_memory_reply_messages "
                    "SET send_status = :status "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND turn_id = :turn "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM chat_memory_message_bots b "
                    "   WHERE b.turn_id = :turn AND b.role = 'assistant' "
                    "     AND b.umo = :umo AND b.conversation_id = :cid)"
                ),
                {
                    "status": "send_attempted",
                    "umo": umo,
                    "cid": conversation_id,
                    "turn": turn_id,
                },
            )
            affected["rows"] = int(result.rowcount or 0)
            # 0 行＝这一轮有实发行（正常轮次），不是"数据不足被跳过"；
            # 返回 True 免得把它记进 shadow_stats.skipped 污染健康度读数。
            return True

        await self._shadow_write(_mark)
        return affected["rows"]

    async def update_send_status(
        self,
        umo: str,
        conversation_id: str,
        turn_id: str,
        new_status: str,
    ) -> int:
        """按内部 turn_id 更新 assistant 的发送流程状态。"""
        await self.init_db()
        if new_status not in VALID_SEND_STATUSES:
            # insert() 对同名字段是严格校验的；这里不校验会让脏状态直接落库，
            # 之后所有按状态过滤的读路径（含 <cm_unsent/> 判定）一起被污染。
            raise ValueError(f"unsupported send_status: {new_status}")
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "UPDATE chat_memory_records SET send_status = :status "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND turn_id = :turn_id AND role = 'assistant' "
                    "AND deleted_at IS NULL"
                ),
                {
                    "status": new_status,
                    "umo": umo,
                    "cid": conversation_id,
                    "turn_id": turn_id,
                },
            )
            await session.commit()
            rowcount = int(result.rowcount or 0)
        await self._shadow_write(
            lambda session: shared_store.mirror_send_status(
                session, umo, conversation_id, turn_id, new_status
            )
        )
        return rowcount

    async def update_llm_status_by_turn(
        self, umo: str, conversation_id: str, turn_id: str, new_status: str
    ) -> int:
        """按内部 turn_id 更新 user 的 llm_status，覆盖无平台 message_id 的平台。"""
        await self.init_db()
        if new_status not in VALID_LLM_STATUSES:
            raise ValueError(f"unsupported llm_status: {new_status}")
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "UPDATE chat_memory_records SET llm_status = :status "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND turn_id = :turn_id AND role = 'user' "
                    "AND deleted_at IS NULL"
                ),
                {
                    "status": new_status,
                    "umo": umo,
                    "cid": conversation_id,
                    "turn_id": turn_id,
                },
            )
            await session.commit()
            rowcount = int(result.rowcount or 0)
        await self._shadow_write(
            lambda session: shared_store.mirror_llm_status(
                session, umo, conversation_id, turn_id, new_status
            )
        )
        return rowcount

    async def query_latest(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[dict]:
        """查询最近 N 条记录（按时间升序返回）。

        ``user_id`` 为 None / 空字符串时不按用户过滤，返回该会话下所有用户的混合记录。
        ``llm_status`` 支持 str 或 list[str]：按 LLM 状态过滤（list 用 IN）。
        ``content_kind`` 支持 str 或 list[str]：默认返回 content_kind JSON 数组中
        **任一包含**这些值的记录；``content_kind_all_match=True`` 时只保留非空且
        全部 kind 都属于白名单的记录。
        ``role_filter`` 给定时仅返回 role 匹配的记录。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``（与 takeover 对齐）。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤时间窗口（含端点）；
        ``after_id`` 必须与 ``since`` 一起使用，此时下界变为严格的
        ``(created_at, id) > (since, after_id)`` keyset 游标。
        ``datetime`` 为 tz-aware 时自动转 UTC naive，naive 假定已是 UTC（与落库 ``CURRENT_TIMESTAMP`` 对齐）。
        ``limit`` 钳到 ``[1, 1000]``：防第三方调用方传 -1 触发 SQLite ``LIMIT -1``（=不限制）导致全库返回。
        """
        await self.init_db()
        limit = max(1, min(1000, int(limit)))
        async with self.async_session() as session:
            conditions = [
                "umo = :umo",
                "conversation_id = :cid",
                "deleted_at IS NULL",
            ]
            params: dict = {"umo": umo, "cid": conversation_id}
            expanding_binds: list[str] = []

            if user_id:
                conditions.append("user_id = :uid")
                params["uid"] = user_id
            if role_filter:
                conditions.append("role = :role")
                params["role"] = role_filter
            if llm_status:
                if isinstance(llm_status, str):
                    status_list = [llm_status]
                else:
                    status_list = list(llm_status)
                if status_list:
                    conditions.append("llm_status IN :statuses")
                    params["statuses"] = status_list
                    expanding_binds.append("statuses")
            if content_kind:
                if isinstance(content_kind, str):
                    kind_list = [content_kind]
                else:
                    kind_list = list(content_kind)
                if kind_list:
                    if content_kind_all_match:
                        conditions.append(
                            "EXISTS (SELECT 1 FROM json_each(content_kind)) "
                            "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) "
                            "WHERE value NOT IN :kinds)"
                        )
                    else:
                        conditions.append(
                            "EXISTS (SELECT 1 FROM json_each(content_kind) "
                            "WHERE value IN :kinds)"
                        )
                    params["kinds"] = kind_list
                    expanding_binds.append("kinds")

            since_norm = _normalize_dt(since)
            until_norm = _normalize_dt(until)
            if after_id is not None and since_norm is None:
                raise ValueError("after_id requires since")
            if since_norm:
                if after_id is None:
                    conditions.append("created_at >= :since")
                else:
                    conditions.append(
                        "(created_at > :since OR "
                        "(created_at = :since AND id > :after_id))"
                    )
                    params["after_id"] = int(after_id)
                params["since"] = since_norm
            if until_norm:
                conditions.append("created_at <= :until")
                params["until"] = until_norm
            if persona_id is not None:
                if persona_id:
                    conditions.append("persona_id = :persona_id")
                    params["persona_id"] = persona_id
                else:
                    conditions.append("(persona_id IS NULL OR persona_id = '')")

            where = " AND ".join(conditions)
            params["lim"] = limit

            order = "ASC" if from_oldest else "DESC"
            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           f"ORDER BY created_at {order}, id {order} LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, params)
            rows = result.fetchall()
            if not from_oldest:
                rows.reverse()
            return [_row_to_dict(r, self._tz) for r in rows]

    async def query_rounds(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[list[dict]]:
        """按配对返回对话轮次。每轮保证 ``[user_dict, assistant_dict]`` 两条。

        仅返回**有 assistant 配对**的 user（用 EXISTS 子查询过滤单边），
        因此 ``limit_rounds`` 轮对应 ``2 * limit_rounds`` 条记录。

        ``llm_status`` / ``content_kind`` 仅过滤 user 侧（assistant 按配对字段返回）。
        ``content_kind_all_match=True`` 时，user 的 content_kind 必须非空且全部属于
        ``content_kind`` 白名单。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``。persona
        条件同时作用于 user、assistant 和配对 EXISTS 子查。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤，条件同时作用于 user、assistant
        和配对 EXISTS 子查；``after_id`` 与 ``since`` 同时使用时，下界改为严格的
        ``(created_at, id)`` keyset 游标；``datetime`` tz-aware 自动转 UTC naive。
        结果严格为完整 ``[user_dict, assistant_dict]``，不会返回孤立 user；历史重复 assistant
        时按 ``created_at ASC, id ASC`` 取最早的一条。
        ``limit_rounds`` 钳到 ``[1, 1000]``：防第三方调用方传 -1 触发 SQLite ``LIMIT -1``（=不限制）。
        """
        await self.init_db()
        limit_rounds = max(1, min(1000, int(limit_rounds)))
        async with self.async_session() as session:
            conditions = [
                "umo = :umo",
                "conversation_id = :cid",
                "role = 'user'",
                "deleted_at IS NULL",
            ]
            user_params: dict = {"umo": umo, "cid": conversation_id}
            expanding_binds: list[str] = []
            if user_id:
                conditions.append("user_id = :uid")
                user_params["uid"] = user_id
            if llm_status:
                if isinstance(llm_status, str):
                    status_list = [llm_status]
                else:
                    status_list = list(llm_status)
                if status_list:
                    conditions.append("llm_status IN :statuses")
                    user_params["statuses"] = status_list
                    expanding_binds.append("statuses")
            if content_kind:
                if isinstance(content_kind, str):
                    kind_list = [content_kind]
                else:
                    kind_list = list(content_kind)
                if kind_list:
                    if content_kind_all_match:
                        conditions.append(
                            "EXISTS (SELECT 1 FROM json_each(content_kind)) "
                            "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) "
                            "WHERE value NOT IN :kinds)"
                        )
                    else:
                        conditions.append(
                            "EXISTS (SELECT 1 FROM json_each(content_kind) "
                            "WHERE value IN :kinds)"
                        )
                    user_params["kinds"] = kind_list
                    expanding_binds.append("kinds")

            since_norm = _normalize_dt(since)
            until_norm = _normalize_dt(until)
            if after_id is not None and since_norm is None:
                raise ValueError("after_id requires since")
            if since_norm:
                if after_id is None:
                    conditions.append("created_at >= :since")
                else:
                    conditions.append(
                        "(created_at > :since OR "
                        "(created_at = :since AND id > :after_id))"
                    )
                    user_params["after_id"] = int(after_id)
                user_params["since"] = since_norm
            if until_norm:
                conditions.append("created_at <= :until")
                user_params["until"] = until_norm
            if persona_id is not None:
                if persona_id:
                    conditions.append("persona_id = :persona_id")
                    user_params["persona_id"] = persona_id
                else:
                    conditions.append("(persona_id IS NULL OR persona_id = '')")

            pair_exists = [
                "EXISTS (SELECT 1 FROM chat_memory_records a",
                "WHERE a.umo = chat_memory_records.umo",
                "AND a.conversation_id = chat_memory_records.conversation_id",
                "AND a.role = 'assistant'",
                "AND a.deleted_at IS NULL",
                "AND ((chat_memory_records.turn_id IS NOT NULL "
                "AND a.turn_id = chat_memory_records.turn_id) OR "
                "(chat_memory_records.turn_id IS NULL "
                "AND a.pair_id = chat_memory_records.message_id))",
            ]
            if since_norm:
                if after_id is None:
                    pair_exists.append("AND a.created_at >= :since")
                else:
                    pair_exists.append(
                        "AND (a.created_at > :since OR "
                        "(a.created_at = :since AND a.id > :after_id))"
                    )
            if until_norm:
                pair_exists.append("AND a.created_at <= :until")
            if persona_id is not None:
                if persona_id:
                    pair_exists.append("AND a.persona_id = :persona_id")
                else:
                    pair_exists.append("AND (a.persona_id IS NULL OR a.persona_id = '')")
            pair_exists.append(")")
            conditions.append(" ".join(pair_exists))

            where = " AND ".join(conditions)
            user_params["lim"] = limit_rounds

            order = "ASC" if from_oldest else "DESC"
            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           f"ORDER BY created_at {order}, id {order} LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, user_params)
            user_rows = list(result.fetchall())
            if not from_oldest:
                user_rows.reverse()

            if not user_rows:
                return []

            user_msg_ids = [r[3] for r in user_rows if r[3]]
            user_turn_ids = [r[20] for r in user_rows if r[20]]
            assistant_map: dict[tuple[str, str], list[dict]] = {}
            if user_msg_ids or user_turn_ids:
                asst_conditions = [
                    "umo = :umo",
                    "conversation_id = :cid",
                    "role = 'assistant'",
                    "(turn_id IN :turn_ids OR pair_id IN :pids)",
                    "deleted_at IS NULL",
                ]
                asst_params: dict = {
                    "umo": umo,
                    "cid": conversation_id,
                    "turn_ids": user_turn_ids or ["__no_turn_id__"],
                    "pids": user_msg_ids or ["__no_message_id__"],
                }
                if since_norm:
                    if after_id is None:
                        asst_conditions.append("created_at >= :since")
                    else:
                        asst_conditions.append(
                            "(created_at > :since OR "
                            "(created_at = :since AND id > :after_id))"
                        )
                        asst_params["after_id"] = int(after_id)
                    asst_params["since"] = since_norm
                if until_norm:
                    asst_conditions.append("created_at <= :until")
                    asst_params["until"] = until_norm
                if persona_id is not None:
                    if persona_id:
                        asst_conditions.append("persona_id = :persona_id")
                        asst_params["persona_id"] = persona_id
                    else:
                        asst_conditions.append("(persona_id IS NULL OR persona_id = '')")
                asst_sql = (
                    _SELECT_COLS + " FROM chat_memory_records WHERE "
                    + " AND ".join(asst_conditions) +
                    " ORDER BY created_at ASC, id ASC"
                )
                asst_result = await session.execute(
                    text(asst_sql)
                    .bindparams(bindparam("turn_ids", expanding=True))
                    .bindparams(bindparam("pids", expanding=True)),
                    asst_params,
                )
                for r in asst_result.fetchall():
                    record = _row_to_dict(r, self._tz)
                    if r[20]:
                        assistant_map.setdefault(("turn", r[20]), []).append(record)
                    if r[4]:
                        assistant_map.setdefault(("pair", r[4]), []).append(record)

            rounds: list[list[dict]] = []
            for r in user_rows:
                if r[20]:
                    assistants = assistant_map.get(("turn", r[20]), [])
                else:
                    assistants = assistant_map.get(("pair", r[3]), [])
                if assistants:
                    rounds.append([_row_to_dict(r, self._tz), assistants[0]])
            return rounds

    async def resolve_user_reply_target(
        self,
        umo: str,
        platform_id: str,
        reply_id: str,
    ) -> Optional[dict]:
        """用平台 Reply.id 唯一解析同一 UMO 内的 user turn；歧义时拒绝猜测。"""
        await self.init_db()
        if not umo or not reply_id:
            return None
        conditions = [
            "umo = :umo",
            "role = 'user'",
            "message_id = :reply_id",
            "turn_id IS NOT NULL",
            "turn_id <> ''",
            "deleted_at IS NULL",
        ]
        params = {"umo": umo, "reply_id": reply_id}
        if platform_id:
            conditions.append("platform_id = :platform_id")
            params["platform_id"] = platform_id
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    _SELECT_COLS + " FROM chat_memory_records WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY created_at DESC, id DESC LIMIT 2"
                ),
                params,
            )
            rows = result.fetchall()
            if len(rows) != 1:
                return None
            return _row_to_dict(rows[0], self._tz)

    async def query_turn_targets(
        self,
        umo: str,
        turn_ids: list[str],
    ) -> dict[tuple[str, str], dict]:
        """在当前 UMO 内批量读取引用目标，key 为 ``(turn_id, role)``。"""
        await self.init_db()
        clean_ids = sorted({str(value) for value in turn_ids if value})
        if not umo or not clean_ids:
            return {}
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    _SELECT_COLS
                    + " FROM chat_memory_records WHERE umo = :umo "
                    "AND turn_id IN :turn_ids "
                    "AND deleted_at IS NULL "
                    "ORDER BY created_at ASC, id ASC"
                ).bindparams(bindparam("turn_ids", expanding=True)),
                {"umo": umo, "turn_ids": clean_ids},
            )
            targets: dict[tuple[str, str], dict] = {}
            for row in result.fetchall():
                record = _row_to_dict(row, self._tz)
                key = (str(record.get("turn_id") or ""), str(record.get("role") or ""))
                targets.setdefault(key, record)
            return targets

    async def insert_tool_record(
        self,
        umo: str,
        conversation_id: str,
        turn_id: str,
        call_index: int,
        tool_name: str,
        tool_args: str,
        tool_result: str,
    ) -> bool:
        """写入一条工具调用记录；(turn_id, call_index) 幂等。

        返回**是否真的插入**：``INSERT OR IGNORE`` 命中幂等键时 rowcount=0，静默当成功
        会让"这一轮的工具段根本没落库"完全看不见（读侧随后回放不到任何工具调用）。
        """
        await self.init_db()
        if not umo or not conversation_id or not turn_id or not tool_name:
            raise ValueError("insert_tool_record requires umo/cid/turn_id/tool_name")
        async with self.async_session() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            result = await session.execute(
                text(
                    "INSERT OR IGNORE INTO chat_memory_tool_records "
                    "(umo, conversation_id, turn_id, call_index, tool_name, "
                    "tool_args, tool_result, created_at) "
                    "VALUES (:umo, :cid, :turn_id, :call_index, :tool_name, "
                    ":tool_args, :tool_result, :now)"
                ),
                {
                    "umo": umo,
                    "cid": conversation_id,
                    "turn_id": turn_id,
                    "call_index": int(call_index),
                    "tool_name": tool_name,
                    "tool_args": tool_args or "",
                    "tool_result": tool_result or "",
                    "now": now,
                },
            )
            inserted = int(result.rowcount or 0)
            await session.commit()
        await self._shadow_write(
            lambda session: shared_store.mirror_tool_call(
                session,
                umo=umo,
                conversation_id=conversation_id,
                turn_id=turn_id,
                call_index=int(call_index),
                tool_name=tool_name,
                tool_args=tool_args or "",
                tool_result=tool_result or "",
                now=now,
            )
        )
        return bool(inserted)

    async def query_tool_records(
        self,
        umo: str,
        conversation_id: str,
        turn_limit: int = 2,
    ) -> list[dict]:
        """查询最近 ``turn_limit`` 个轮次的工具调用记录（按时间正序）。

        只查当前 umo + cid：工具调用天然属于当前会话，不参与
        cross_session / full_group 的扩大范围。``turn_limit=0`` 返回空
        （调用方用于关闭工具回放）。
        """
        await self.init_db()
        turn_limit = min(50, max(0, int(turn_limit)))
        if turn_limit <= 0:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT turn_id, call_index, tool_name, tool_args, "
                    "tool_result, created_at "
                    "FROM chat_memory_tool_records "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND deleted_at IS NULL "
                    "AND turn_id IN ("
                    "  SELECT turn_id FROM chat_memory_tool_records "
                    "  WHERE umo = :umo AND conversation_id = :cid "
                    "  AND deleted_at IS NULL "
                    "  GROUP BY turn_id "
                    "  ORDER BY MIN(created_at) DESC, MIN(id) DESC "
                    "  LIMIT :turn_limit"
                    ") "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"umo": umo, "cid": conversation_id, "turn_limit": turn_limit},
            )
            return [
                {
                    "turn_id": row[0],
                    "call_index": int(row[1]),
                    "tool_name": row[2],
                    "tool_args": row[3] or "",
                    "tool_result": row[4] or "",
                    "created_at": row[5],
                }
                for row in result.fetchall()
            ]

    async def delete_by_conversation(
        self, umo: str, conversation_id: str
    ) -> tuple[int, list[dict]]:
        """清除某个 conversation_id 下的所有记录（用于 /reset）。工具表与媒体归档同步清理。

        返回 ``(删除的记录数, 被删归档行列表)``；归档行含 file_name/created_at，
        由调用方负责删除对应媒体文件。
        """
        await self.init_db()
        async with self.async_session() as session:
            await session.execute(
                text(
                    "DELETE FROM chat_memory_tool_records "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            media_result = await session.execute(
                text(
                    "SELECT file_name, created_at FROM chat_memory_media_archive "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            media_rows = [
                {"file_name": row[0], "created_at": row[1]}
                for row in media_result.fetchall()
            ]
            await session.execute(
                text(
                    "DELETE FROM chat_memory_media_archive "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            result = await session.execute(
                text(
                    "DELETE FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            await session.commit()
            rowcount = int(result.rowcount or 0)
        await self._shadow_write(
            lambda session: shared_store.mirror_delete_conversation(
                session, umo, conversation_id
            )
        )
        return rowcount, media_rows

    async def delete_old(self, before: datetime) -> tuple[int, list[dict]]:
        """删除 created_at 早于 ``before`` 的所有记录（用于 auto_cleanup_days）。

        工具表与媒体归档同步清理；返回 ``(删除的记录数, 被删归档行列表)``。
        """
        await self.init_db()
        async with self.async_session() as session:
            await session.execute(
                text("DELETE FROM chat_memory_tool_records WHERE created_at < :before"),
                {"before": before},
            )
            media_result = await session.execute(
                text(
                    "SELECT file_name, created_at FROM chat_memory_media_archive "
                    "WHERE created_at < :before"
                ),
                {"before": before},
            )
            media_rows = [
                {"file_name": row[0], "created_at": row[1]}
                for row in media_result.fetchall()
            ]
            await session.execute(
                text(
                    "DELETE FROM chat_memory_media_archive "
                    "WHERE created_at < :before"
                ),
                {"before": before},
            )
            result = await session.execute(
                text("DELETE FROM chat_memory_records WHERE created_at < :before"),
                {"before": before},
            )
            await session.commit()
            rowcount = int(result.rowcount or 0)
        await self._shadow_write(
            lambda session: shared_store.mirror_delete_before(session, before)
        )
        return rowcount, media_rows

    async def purge_orphans(self) -> tuple[int, int]:
        """回收合并存储里的孤儿行（无参与行的消息 / 指向已删消息的参与行）。

        返回 ``(消息数, 参与行数)``。影子层关闭时返回 ``(0, 0)``（那是"没有合并存储"，
        不是"没有孤儿"——调用方据此区分，不打日志）。
        """
        if not self.shadow_enabled:
            return (0, 0)
        await self.init_db()
        async with self.async_session() as session:
            removed = await shared_store.purge_orphan_rows(session)
            await session.commit()
            return removed

    async def count_records_in_conversation(
        self, umo: str, conversation_id: str
    ) -> list[dict]:
        """当前 CID 下的可见记录按 ``content_kind`` 分组计数（/reset 残留自检）。

        返回 ``[{content_kind, count}]``，按数量降序；只统计未软删除的行。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT content_kind, COUNT(*) AS n FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND deleted_at IS NULL "
                    "GROUP BY content_kind ORDER BY n DESC LIMIT 10"
                ),
                {"umo": umo, "cid": str(conversation_id or "")},
            )
            return [
                {"content_kind": str(row[0] or "[]"), "count": int(row[1] or 0)}
                for row in result.fetchall()
            ]

    async def count_records_by_conversation(
        self, umo: str, exclude_cid: str = ""
    ) -> list[dict]:
        """按 conversation_id 统计同 UMO 的记录数（/reset 残留诊断用）。

        ``exclude_cid`` 排除刚清理的 CID；按记录数降序返回，最多 200 组。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT conversation_id, COUNT(*) AS n "
                    "FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id <> :cid "
                    "AND deleted_at IS NULL "
                    "GROUP BY conversation_id ORDER BY n DESC LIMIT 200"
                ),
                {"umo": umo, "cid": str(exclude_cid or "")},
            )
            return [
                {"conversation_id": str(row[0] or ""), "count": int(row[1] or 0)}
                for row in result.fetchall()
            ]

    # ── 媒体归档表操作（schema v5）────────────────────

    async def insert_media_archive(
        self,
        media_id: str,
        umo: str,
        conversation_id: str,
        turn_id: str,
        kind: str,
        file_name: str,
        ext: str,
        mime_type: Optional[str],
        size_bytes: int,
        created_at: Optional[datetime] = None,
        content_hash: str = "",
    ) -> bool:
        """登记一条已落盘媒体。created_at 缺省为当前 UTC naive（与主表一致）。

        ``content_hash`` 是文件内容的哈希：同一张图被多个实例各存一份时，文件按它
        内容寻址（同名即同内容），这里也记一份供查询与"出现过几次"统计。
        """
        await self.init_db()
        if created_at is None:
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "INSERT OR IGNORE INTO chat_memory_media_archive "
                    "(media_id, umo, conversation_id, turn_id, kind, file_name, "
                    "content_hash, ext, mime_type, size_bytes, created_at) "
                    "VALUES (:media_id, :umo, :cid, :turn_id, :kind, :file_name, "
                    ":content_hash, :ext, :mime_type, :size_bytes, :created_at)"
                ),
                {
                    "media_id": media_id,
                    "umo": umo,
                    "cid": conversation_id,
                    "turn_id": turn_id,
                    "kind": kind,
                    "file_name": file_name,
                    "content_hash": str(content_hash or ""),
                    "ext": ext,
                    "mime_type": mime_type,
                    "size_bytes": int(size_bytes),
                    "created_at": created_at,
                },
            )
            inserted = int(result.rowcount or 0)
            await session.commit()
        # 返回"行是否真的落库"：media_id 冲突时 INSERT OR IGNORE 是空操作，恒返回 True
        # 会让调用方把"文件已落位、却没有任何归档行引用"当成功（只剩孤儿扫描兜底）。
        return bool(inserted)

    async def media_file_referenced(
        self, *, file_name: str, created_at: Optional[datetime]
    ) -> bool:
        """同一路径的媒体文件是否还有别的归档行在用（内容寻址后多个 media_id 共用一个文件）。

        删除文件前必须问一次：多实例各自归档同一张图 → 多行指向同一文件，
        删掉其中一行就把别人的文件也删了。
        """
        if not file_name or created_at is None:
            return False
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM chat_memory_media_archive "
                    "WHERE file_name = :name AND strftime('%Y%m', created_at) = :month "
                    "LIMIT 1"
                ),
                {"name": file_name, "month": created_at.strftime("%Y%m")},
            )
            return result.first() is not None

    async def query_media_without_hash(self, limit: int = 200) -> list[dict]:
        """取一批还没有内容哈希的归档行（老库迁移用，按 id 顺序，天然断点续跑）。

        只认 ``content_hash IS NULL``：``''`` 是迁移时写下的"文件已不在、无需迁移"标记，
        不能再被取出来（否则这些行会一直占着前排，把后面要改名的行永久堵死）。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT media_id, file_name, ext, created_at, size_bytes "
                    "FROM chat_memory_media_archive "
                    "WHERE content_hash IS NULL "
                    "ORDER BY media_id LIMIT :limit"
                ),
                {"limit": max(1, int(limit))},
            )
            return [dict(row._mapping) for row in result.fetchall()]

    async def update_media_content_hash(
        self, media_id: str, *, content_hash: str, file_name: str = ""
    ) -> None:
        """补写内容哈希（顺带改文件名：内容寻址后同名即同内容）。"""
        await self.init_db()
        async with self.async_session() as session:
            if file_name:
                await session.execute(
                    text(
                        "UPDATE chat_memory_media_archive SET content_hash = :hash, "
                        "file_name = :name WHERE media_id = :mid"
                    ),
                    {"hash": str(content_hash or ""), "name": file_name, "mid": media_id},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE chat_memory_media_archive SET content_hash = :hash "
                        "WHERE media_id = :mid"
                    ),
                    {"hash": str(content_hash or ""), "mid": media_id},
                )
            await session.commit()

    async def query_media_archive_for_cleanup(
        self, limit: int = 1000
    ) -> list[dict]:
        """按 created_at 升序取归档行（清理用：保留期/总量上限从旧到新删除）。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT media_id, file_name, created_at, size_bytes "
                    "FROM chat_memory_media_archive ORDER BY created_at ASC "
                    "LIMIT :limit"
                ),
                {"limit": max(1, int(limit))},
            )
            return [
                {
                    "media_id": row[0],
                    "file_name": row[1],
                    "created_at": row[2],
                    "size_bytes": row[3],
                }
                for row in result.fetchall()
            ]

    async def media_archive_total_size(self) -> int:
        """归档**占用磁盘**的字节数：同一张物理文件只算一次。

        内容寻址后多个归档行指向同一个文件（多实例各存一份同一张图 → 落到同一文件），按行
        累加会把同一份文件重复计数，总量上限因此提前触发、删掉本来还不需要删的行。
        文件按 ``(file_name, created_at 的月份)`` 定位（目录就是 ``media/<YYYYMM>/``）。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT COALESCE(SUM(size), 0) FROM ("
                    "  SELECT MAX(size_bytes) AS size FROM chat_memory_media_archive"
                    "  GROUP BY file_name, strftime('%Y%m', created_at)"
                    ")"
                )
            )
            return int(result.scalar() or 0)

    async def media_archive_file_counts(self) -> dict[tuple[str, str], int]:
        """每个物理文件被多少归档行引用：``{(file_name, 月份): 行数}``。

        配额清理用：删行只有在该文件**最后一行**被删掉时才真的腾出磁盘，中间那些行不该算
        进释放量（否则按"每行都腾出空间"的错觉会删过头）。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT file_name, strftime('%Y%m', created_at) AS month, COUNT(*) "
                    "FROM chat_memory_media_archive GROUP BY 1, 2"
                )
            )
            return {
                (str(row[0] or ""), str(row[1] or "")): int(row[2] or 0)
                for row in result.fetchall()
            }

    async def delete_media_archive_by_ids(self, media_ids: list[str]) -> int:
        """按 media_id 删除归档行（清理总量/保留期时使用）。返回删除行数。"""
        await self.init_db()
        ids = [str(item) for item in (media_ids or []) if item]
        if not ids:
            return 0
        async with self.async_session() as session:
            placeholders = ", ".join(f":id_{i}" for i in range(len(ids)))
            result = await session.execute(
                text(
                    "DELETE FROM chat_memory_media_archive "
                    f"WHERE media_id IN ({placeholders})"
                ),
                {f"id_{i}": ids[i] for i in range(len(ids))},
            )
            await session.commit()
            return result.rowcount

    async def find_by_message_id(self, umo: str, message_id: str) -> Optional[dict]:
        """按平台 message_id 查 user 记录（撤回 notice 的本地关联）。

        返回原始 ``content`` / ``relation_data`` / ``sender_nickname``；
        无命中返回 None。message_id 在平台实例内唯一，umo 已含实例 ID。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT content, relation_data, sender_nickname "
                    "FROM chat_memory_records "
                    "WHERE umo = :umo AND message_id = :mid AND role = 'user' "
                    "AND deleted_at IS NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"umo": umo, "mid": str(message_id or "")},
            )
            row = result.first()
            if row is None:
                return None
            return {
                "content": row[0] or "",
                "relation_data": row[1],
                "sender_nickname": row[2] or "",
            }

    async def mark_recalled(self, umo: str, message_id: str) -> int:
        """把被撤回的 user 记录标记为已撤回（``recalled_at``，幂等）。

        只标记 role='user' 且尚未标记的行；未命中返回 0。assistant 记录没有
        message_id，天然不会被误标。
        """
        mid = str(message_id or "").strip()
        if not umo or not mid:
            return 0
        await self.init_db()
        async with self.async_session() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            result = await session.execute(
                text(
                    "UPDATE chat_memory_records SET recalled_at = :now "
                    "WHERE umo = :umo AND message_id = :mid AND role = 'user' "
                    "AND recalled_at IS NULL AND deleted_at IS NULL"
                ),
                {
                    "now": now,
                    "umo": umo,
                    "mid": mid,
                },
            )
            await session.commit()
            rowcount = int(result.rowcount or 0)
        await self._shadow_write(
            lambda session: shared_store.mirror_recall(session, umo, mid, now)
        )
        return rowcount

    async def latest_sender_nickname(self, umo: str, user_id: str) -> Optional[str]:
        """按 umo+user_id 查最近一条 user 记录的真实昵称。

        排除空昵称与「昵称=账号」（notice 事件的适配器兜底）；用于
        事件（<cm_event>）的目标昵称解析。无命中返回 None。
        """
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT sender_nickname FROM chat_memory_records "
                    "WHERE umo = :umo AND user_id = :uid AND role = 'user' "
                    "AND sender_nickname IS NOT NULL AND sender_nickname != '' "
                    "AND sender_nickname != :uid AND deleted_at IS NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"umo": umo, "uid": str(user_id or "")},
            )
            row = result.first()
            return str(row[0] or "") if row is not None else None

    async def query_rounds_raw(
        self,
        umo: str,
        conversation_id: Optional[str],
        user_id: Optional[str],
        limit_rounds: int,
        include_kinds: Optional[set[str]] = None,
        all_match: bool = False,
        cross_umo: bool = False,
        full_group: bool = False,
        persona_id: Optional[str] = None,
        filter_by_persona: bool = False,
        skip_recalled: bool = False,
        blacklist: Optional[list[str]] = None,
    ) -> list[list[dict]]:
        """内部方法：配对模式查询（takeover 专用）。

        类似 ``query_rounds`` 但支持 ``include_kinds`` 白名单过滤。
        仅返回**有 assistant 配对**的 user，每轮 ``[user_dict, assistant_dict]`` 两条。

        scope 由 ``cross_umo`` × ``full_group`` 决定（见 ``_scope_filter``）：
        - F/F：当前 umo 当前 user
        - T/F：跨 umo 当前 user（群私聊互通）
        - F/T：当前 umo 整群
        - T/T：当前 umo 整群 + 其他 umo 当前 user（混合语义）

        ``conversation_id`` 为 None 时跨 CID。EXISTS 子查的
        ``a.umo = chat_memory_records.umo`` 是行内自连接，跨 umo 仍保证
        user/assistant 在同一 umo 内配对；新记录优先使用 turn_id，旧记录才依赖
        message_id/pair_id。

        ``include_kinds``：白名单语义，空集合 = 不过滤；非空时配合 ``all_match``：
        - ``all_match=False`` (ANY)：record 的 content_kind 与白名单**任一交集**即保留
        - ``all_match=True`` (ALL)：record 的 content_kind **全部**属于白名单（且非空）才保留
        仅在 user 查询时过滤（assistant 不过滤，与配对语义一致）。

        ``filter_by_persona``：开启后按 ``persona_id`` 严格过滤（user、assistant 和 EXISTS
        子查都加条件，保证只返回完整的同 persona 配对）；
        ``persona_id`` 为空时过滤 ``IS NULL OR ''`` 的记录（匹配老库补列后的旧行）。
        """
        await self.init_db()
        # 与 query_rounds 同口径钳制：-1 在 SQLite 里等于"不限制"（整库返回）。
        limit_rounds = max(1, min(1000, int(limit_rounds)))
        async with self.async_session() as session:
            scope_cond, scope_param = _scope_filter(
                umo, user_id, cross_umo, full_group, blacklist
            )
            # EXISTS 子查：cid 非空时加 conversation_id 条件（防跨 cid 误配对）；
            # cid=None（cross_session）时不加，允许跨 cid 配对
            exists_parts = [
                "EXISTS (SELECT 1 FROM chat_memory_records a",
                "WHERE a.umo = chat_memory_records.umo",
            ]
            if conversation_id:
                exists_parts.append(
                    "AND a.conversation_id = chat_memory_records.conversation_id"
                )
            exists_parts.extend([
                "AND a.role = 'assistant'",
                "AND a.deleted_at IS NULL",
                # 空串与 NULL 必须同等对待：写入侧历史上出现过 turn_id=''（唯一索引的
                # 条件就是 `turn_id <> ''`），只判 IS NULL 会让这些行在配对里整段消失，
                # 而 Python 侧用的是 falsy 判断——两处口径从此一致。
                "AND ((COALESCE(chat_memory_records.turn_id, '') <> '' "
                "AND a.turn_id = chat_memory_records.turn_id) OR "
                "(COALESCE(chat_memory_records.turn_id, '') = '' "
                "AND a.pair_id = chat_memory_records.message_id)))",
            ])
            conditions = [
                scope_cond,
                "role = 'user'",
                " ".join(exists_parts),
                "deleted_at IS NULL",
            ]
            if skip_recalled:
                conditions.append("recalled_at IS NULL")
            params: dict = {**scope_param, "lim": limit_rounds}
            expanding_binds: list[str] = []

            if conversation_id:
                conditions.append("conversation_id = :cid")
                params["cid"] = conversation_id

            # persona 过滤：user、assistant 和 EXISTS 三处一致，保证严格完整配对。
            persona_cond = None
            if filter_by_persona:
                if persona_id:
                    persona_cond = "persona_id = :persona_id"
                    conditions.append(persona_cond)
                    params["persona_id"] = persona_id
                    exists_parts.insert(-1, "AND a.persona_id = :persona_id")
                else:
                    persona_cond = "(persona_id IS NULL OR persona_id = '')"
                    conditions.append(persona_cond)
                    exists_parts.insert(-1, "AND (a.persona_id IS NULL OR a.persona_id = '')")
                conditions[2] = " ".join(exists_parts)

            # include_kinds：白名单
            if include_kinds:
                if all_match:
                    # ALL：非空且全部 kind ∈ 白名单
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind)) "
                        "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value NOT IN :include_kinds)"
                    )
                else:
                    # ANY：任一 kind ∈ 白名单
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value IN :include_kinds)"
                    )
                params["include_kinds"] = list(include_kinds)
                expanding_binds.append("include_kinds")

            where = " AND ".join(conditions)
            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           "ORDER BY created_at DESC, id DESC LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, params)
            user_rows = list(reversed(result.fetchall()))  # 升序

            if not user_rows:
                return []

            user_msg_ids = [r[3] for r in user_rows if r[3]]  # message_id 在第 4 列
            user_turn_ids = [r[20] for r in user_rows if r[20]]
            assistant_map: dict[tuple[str, str], list[dict]] = {}
            if user_msg_ids or user_turn_ids:
                # assistant 查询：与 user 同 scope（保证配对在范围内），按 turn_id 或 pair_id 配对
                asst_conditions = [
                    scope_cond,
                    "role = 'assistant'",
                    "(turn_id IN :turn_ids OR pair_id IN :pids)",
                    "deleted_at IS NULL",
                ]
                asst_params = {
                    **scope_param,
                    "turn_ids": user_turn_ids or ["__no_turn_id__"],
                    "pids": user_msg_ids or ["__no_message_id__"],
                }
                if conversation_id:
                    asst_conditions.append("conversation_id = :cid")
                    asst_params["cid"] = conversation_id
                if persona_cond:
                    asst_conditions.append(persona_cond)
                    asst_params["persona_id"] = persona_id
                asst_sql = (
                    _SELECT_COLS + " FROM chat_memory_records WHERE "
                    + " AND ".join(asst_conditions) +
                    " ORDER BY created_at ASC, id ASC"
                )
                asst_result = await session.execute(
                    text(asst_sql)
                    .bindparams(bindparam("turn_ids", expanding=True))
                    .bindparams(bindparam("pids", expanding=True)),
                    asst_params,
                )
                for r in asst_result.fetchall():
                    record = _row_to_dict(r, self._tz)
                    if r[20]:
                        assistant_map.setdefault(("turn", r[20]), []).append(record)
                    if r[4]:
                        assistant_map.setdefault(("pair", r[4]), []).append(record)

            rounds: list[list[dict]] = []
            for r in user_rows:
                if r[20]:
                    assistants = assistant_map.get(("turn", r[20]), [])
                else:
                    assistants = assistant_map.get(("pair", r[3]), [])
                if assistants:
                    rounds.append([_row_to_dict(r, self._tz), assistants[0]])
            return rounds

    async def query_messages_raw(
        self,
        umo: str,
        conversation_id: Optional[str],
        user_id: Optional[str],
        limit_messages: int,
        statuses: set[str],
        include_kinds: Optional[set[str]] = None,
        all_match: bool = False,
        cross_umo: bool = False,
        full_group: bool = False,
        persona_id: Optional[str] = None,
        filter_by_persona: bool = False,
        exclude_turn_id: Optional[str] = None,
        skip_recalled: bool = False,
        blacklist: Optional[list[str]] = None,
    ) -> list[dict]:
        """内部方法：混合模式查询（takeover 专用）。

        查询全量消息（user + assistant），按 ``limit_messages`` 条数切片。
        scope 由 ``cross_umo`` × ``full_group`` 决定（见 ``_scope_filter``）：
        - F/F：当前 umo 当前 user
        - T/F：跨 umo 当前 user（群私聊互通）
        - F/T：当前 umo 整群
        - T/T：当前 umo 整群 + 其他 umo 当前 user（混合语义）
        ``conversation_id`` 为 None 时跨 CID。
        ``statuses`` 过滤 llm_status（IN 语义）。

        ``include_kinds``：白名单语义，空集合 = 不过滤；非空时配合 ``all_match``：
        - ``all_match=False`` (ANY)：record 的 content_kind 与白名单**任一交集**即保留
        - ``all_match=True`` (ALL)：record 的 content_kind **全部**属于白名单（且非空）才保留

        ``filter_by_persona``：开启后按 ``persona_id`` 严格过滤；``persona_id`` 为空时
        过滤 ``IS NULL OR ''`` 的记录（匹配老库补列后的旧行）。

        ``exclude_turn_id``：排除当前正在请求 LLM 的轮次，避免当前 user 同时出现在
        takeover history 与 ProviderRequest.prompt 中。
        """
        await self.init_db()
        # 钳制：负数会变成 SQLite 的 LIMIT -1（等于不限制，整库返回）。同族的
        # query_latest / query_rounds 一直是这么钳的，这里补齐。
        limit_messages = max(1, min(1000, int(limit_messages)))
        async with self.async_session() as session:
            scope_cond, scope_param = _scope_filter(
                umo, user_id, cross_umo, full_group, blacklist
            )
            conditions = [
                scope_cond,
                "role IN ('user', 'assistant')",
                "deleted_at IS NULL",
            ]
            if skip_recalled:
                conditions.append("recalled_at IS NULL")
            params: dict = {**scope_param, "lim": limit_messages}
            expanding_binds: list[str] = []

            if conversation_id:
                conditions.append("conversation_id = :cid")
                params["cid"] = conversation_id

            if exclude_turn_id:
                conditions.append("(turn_id IS NULL OR turn_id != :exclude_turn_id)")
                params["exclude_turn_id"] = exclude_turn_id

            if filter_by_persona:
                if persona_id:
                    conditions.append("persona_id = :persona_id")
                    params["persona_id"] = persona_id
                else:
                    conditions.append("(persona_id IS NULL OR persona_id = '')")

            if statuses:
                conditions.append("llm_status IN :statuses")
                params["statuses"] = list(statuses)
                expanding_binds.append("statuses")

            if include_kinds:
                if all_match:
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind)) "
                        "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value NOT IN :include_kinds)"
                    )
                else:
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value IN :include_kinds)"
                    )
                params["include_kinds"] = list(include_kinds)
                expanding_binds.append("include_kinds")

            where = " AND ".join(conditions)
            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           "ORDER BY created_at DESC, id DESC LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, params)
            rows = result.fetchall()
            return [_row_to_dict(r, self._tz) for r in reversed(rows)]  # 升序


    # ── V2 读路径（合并存储/影子层；V1 的旧表读法一行不改）──────────────
    #
    # 为什么保留两份：切读要能对拍、能回退。V2 的方法名与 V1 一一对应，返回的 dict
    # 也保持同形（额外多几个影子层字段：msg_uid / platform_seq / segments…）。
    # 影子层没起来时**明确报错**，不返回空列表假装没数据。

    @asynccontextmanager
    async def _v2_session(self):
        """V2 读的统一入口：确认影子层可用 → 建连接 → 交出 session。"""
        if not self.shadow_enabled:
            raise RuntimeError(
                "合并存储未启用（双写未开启或影子层未建立），V2 读接口不可用"
            )
        await self.init_db()
        async with self.async_session() as session:
            yield session

    @staticmethod
    def _session_scope_v2(umo: str, user_id: Optional[str]) -> tuple[str, dict]:
        """公开 V2 接口的会话范围：与 V1 ``query_latest`` / ``query_rounds`` 同语义。

        ``user_id`` 为空 = 该会话所有用户混排；非空 = ``user_id`` 列匹配（user 行是发送者、
        assistant 行是被回复对象——影子视图里的 ``user_id`` 就是这么算的）。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return "umo = :scope_umo", {"scope_umo": umo}
        return (
            "umo = :scope_umo AND user_id = :scope_uid",
            {"scope_umo": umo, "scope_uid": uid},
        )

    @staticmethod
    def _as_list(value: Optional[Union[str, list[str]]]) -> Optional[set[str]]:
        """``str | list[str] | None`` → ``set[str] | None``（V1 的 status/kind 参数口径）。"""
        if not value:
            return None
        return {value} if isinstance(value, str) else set(value)

    @staticmethod
    def _check_after_id(after_id: Optional[int], since: Optional[datetime]) -> None:
        if after_id is not None and since is None:
            raise ValueError("after_id requires since")

    async def query_latest_v2(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[dict]:
        """V2 版 ``query_latest``：同样的参数与语义，数据来自影子层。

        ``record_id`` / ``after_id`` 用影子层的**会话内序号**（``seq``），不是旧表行号。
        """
        self._check_after_id(after_id, since)
        scope_cond, scope_params = self._session_scope_v2(umo, user_id)
        async with self._v2_session() as session:
            return await shadow_read.fetch_messages(
                session,
                scope_cond=scope_cond,
                scope_params=scope_params,
                tz=self._tz,
                cid=conversation_id,
                limit=limit,
                statuses=self._as_list(llm_status),
                include_kinds=self._as_list(content_kind),
                all_match=content_kind_all_match,
                role_filter=role_filter,
                persona_id=persona_id,
                filter_by_persona=persona_id is not None,
                since=_normalize_dt(since),
                until=_normalize_dt(until),
                from_oldest=from_oldest,
                after_id=after_id,
            )

    async def query_rounds_v2(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[list[dict]]:
        """V2 版 ``query_rounds``：每轮 ``[user, assistant]``；assistant 侧是合并后的完整回复。"""
        self._check_after_id(after_id, since)
        scope_cond, scope_params = self._session_scope_v2(umo, user_id)
        async with self._v2_session() as session:
            return await shadow_read.fetch_rounds(
                session,
                scope_cond=scope_cond,
                scope_params=scope_params,
                tz=self._tz,
                cid=conversation_id,
                limit_rounds=limit_rounds,
                statuses=self._as_list(llm_status),
                include_kinds=self._as_list(content_kind),
                all_match=content_kind_all_match,
                persona_id=persona_id,
                filter_by_persona=persona_id is not None,
                since=_normalize_dt(since),
                until=_normalize_dt(until),
                from_oldest=from_oldest,
                after_id=after_id,
            )

    async def query_tool_records_v2(
        self,
        umo: str,
        conversation_id: str,
        turn_limit: int = 2,
    ) -> list[dict]:
        """V2 版 ``query_tool_records``：读合并存储的工具调用表，返回同形 dict。"""
        turn_limit = min(50, max(0, int(turn_limit)))
        if turn_limit <= 0:
            return []
        async with self._v2_session() as session:
            result = await session.execute(
                text(
                    "SELECT turn_id, call_index, tool_name, tool_args, "
                    "tool_result, created_at "
                    "FROM chat_memory_tool_calls "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND deleted_at IS NULL "
                    "AND turn_id IN ("
                    "  SELECT turn_id FROM chat_memory_tool_calls "
                    "  WHERE umo = :umo AND conversation_id = :cid "
                    "  AND deleted_at IS NULL "
                    "  GROUP BY turn_id "
                    "  ORDER BY MIN(created_at) DESC, MIN(id) DESC "
                    "  LIMIT :turn_limit"
                    ") "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"umo": umo, "cid": conversation_id, "turn_limit": turn_limit},
            )
            return [
                {
                    "turn_id": row[0],
                    "call_index": int(row[1]),
                    "tool_name": row[2],
                    "tool_args": row[3] or "",
                    "tool_result": row[4] or "",
                    "created_at": row[5],
                }
                for row in result.fetchall()
            ]

    async def query_turn_targets_v2(
        self,
        umo: str,
        turn_ids: list[str],
    ) -> dict[tuple[str, str], dict]:
        """V2 版 ``query_turn_targets``：Reply 目标批量读取，key 为 ``(turn_id, role)``。"""
        async with self._v2_session() as session:
            return await shadow_read.fetch_turn_targets(
                session, umo=umo, turn_ids=turn_ids, tz=self._tz
            )

    async def query_messages_raw_v2(
        self,
        umo: str,
        conversation_id: Optional[str],
        user_id: Optional[str],
        limit_messages: int,
        statuses: set[str],
        include_kinds: Optional[set[str]] = None,
        all_match: bool = False,
        cross_umo: bool = False,
        full_group: bool = False,
        persona_id: Optional[str] = None,
        filter_by_persona: bool = False,
        exclude_turn_id: Optional[str] = None,
        skip_recalled: bool = False,
        blacklist: Optional[list[str]] = None,
    ) -> list[dict]:
        """V2 版 ``query_messages_raw``（takeover 混合模式）：scope 语义与 V1 完全一致。"""
        scope_cond, scope_param = _scope_filter(
            umo, user_id, cross_umo, full_group, blacklist
        )
        async with self._v2_session() as session:
            return await shadow_read.fetch_messages(
                session,
                scope_cond=scope_cond,
                scope_params=scope_param,
                tz=self._tz,
                cid=conversation_id,
                limit=limit_messages,
                statuses=statuses or None,
                include_kinds=include_kinds or None,
                all_match=all_match,
                persona_id=persona_id,
                filter_by_persona=filter_by_persona,
                exclude_turn_id=exclude_turn_id or None,
                skip_recalled=skip_recalled,
            )

    async def query_rounds_raw_v2(
        self,
        umo: str,
        conversation_id: Optional[str],
        user_id: Optional[str],
        limit_rounds: int,
        include_kinds: Optional[set[str]] = None,
        all_match: bool = False,
        cross_umo: bool = False,
        full_group: bool = False,
        persona_id: Optional[str] = None,
        filter_by_persona: bool = False,
        skip_recalled: bool = False,
        blacklist: Optional[list[str]] = None,
    ) -> list[list[dict]]:
        """V2 版 ``query_rounds_raw``（takeover 配对模式）。

        与 V1 同语义：**不按 llm_status 过滤**——"配对"指这一轮有 assistant 回复
        （由 ``fetch_rounds`` 按 turn_id 挂回时自然保证），V1 的 ``query_rounds_raw``
        里也没有状态条件。曾经在这里硬编码 ``{"llm_success"}``，切读后会把所有
        "有回复但 user 行状态不是 llm_success"的轮次整轮丢掉（静默失忆）。
        """
        scope_cond, scope_param = _scope_filter(
            umo, user_id, cross_umo, full_group, blacklist
        )
        async with self._v2_session() as session:
            return await shadow_read.fetch_rounds(
                session,
                scope_cond=scope_cond,
                scope_params=scope_param,
                tz=self._tz,
                cid=conversation_id,
                limit_rounds=limit_rounds,
                statuses=None,
                include_kinds=include_kinds or None,
                all_match=all_match,
                persona_id=persona_id,
                filter_by_persona=filter_by_persona,
                skip_recalled=skip_recalled,
            )


# SELECT 列顺序固定，_row_to_dict 按位置映射
def _utc_now_str() -> str:
    """库里统一用 UTC naive 字符串（与 schema default CURRENT_TIMESTAMP 对齐）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """归一化 datetime 为 UTC naive（与 schema default ``CURRENT_TIMESTAMP`` 对齐）。

    - None 透传
    - tz-aware 转 UTC naive
    - naive 假定已经是 UTC（与 ``insert`` 里 ``datetime.now(timezone.utc).replace(tzinfo=None)`` 一致）

    供对外 API ``query_latest`` / ``query_rounds`` 的 ``since`` / ``until`` 参数使用。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_blacklist_entry(entry) -> Optional[tuple[str, str]]:
    """解析一条黑名单配置：``G:<群号>`` / ``F:<用户号>`` → ``("group"|"friend", id)``。

    只认这两种写法（大小写不敏感，冒号必须半角）：

    - ``G:123456789`` —— 群聊会话（``message_type='GroupMessage'``）
    - ``F:123456789`` —— 私聊会话（``message_type='FriendMessage'``，id 是对方用户号）

    其它写法一律返回 ``None``（调用方负责提示，别让"写错的名单"变成静默失效）。
    与前缀一起看，标识的是**会话本身**、与实例无关：同一群在不同平台实例下都命中。
    """
    raw = str(entry or "").strip()
    if len(raw) < 3 or raw[1] != ":":
        return None
    kind = raw[0].lower()
    value = raw[2:].strip()
    if kind not in ("g", "f") or not value or ":" in value:
        return None
    # 只接受 ASCII 且不含空白的 id：全角数字（１２３）、内嵌空格这类写法永远匹配不上
    # 真实会话号，判成"写错"并告警，而不是留下一个静默失效的名单条目。
    if not value.isascii() or any(ch.isspace() for ch in value):
        return None
    return ("group" if kind == "g" else "friend", value)


def _blacklist_items(entries) -> list[str]:
    """把配置值摊平成待解析的字符串条目。

    只接受 list / tuple / 单条字符串：写成裸字符串时按**一条**处理（否则会被逐字符拆开，
    告警里出现一堆单字符）；其余类型（数字、dict…）按一条非法值处理，交给告警去说。
    """
    if entries is None or entries == "":
        return []
    if isinstance(entries, str):
        return [entries]
    if isinstance(entries, (list, tuple)):
        return [str(x) for x in entries if x is not None and str(x).strip()]
    return [str(entries)]


def normalize_session_blacklist(entries) -> list[str]:
    """把配置值归一化成去重后的**合法**条目（去空白、丢空串/非字符串/解析失败的）。

    黑名单是"跨会话记忆"的**边界**：它同时决定"别人的记忆里不出现这些会话"和
    "这些会话看不到别人的记忆"，所以只接受 ``G:<群号>`` / ``F:<用户号>``；写错的条目
    由调用方（插件加载时）列出并告警——被静默忽略的名单等于没有名单。
    """
    out: list[str] = []
    for raw in _blacklist_items(entries):
        item = raw.strip()
        if item and item not in out and parse_blacklist_entry(item) is not None:
            out.append(item)
    return out


def invalid_blacklist_entries(entries) -> list[str]:
    """列出写错的条目（供加载时告警用；空列表表示配置没问题）。"""
    bad: list[str] = []
    for raw in _blacklist_items(entries):
        if raw.strip() and parse_blacklist_entry(raw) is None:
            bad.append(raw.strip())
    return bad


def session_key(umo: str) -> Optional[tuple[str, str]]:
    """UMO → ``("group"|"friend", 会话号)``；形态不对返回 ``None``。

    只认 AstrBot 的 ``{platform_id}:{message_type}:{session_id}``：``GroupMessage`` 归
    ``group``、``FriendMessage`` 归 ``friend``，其余类型不参与黑名单匹配。
    """
    parts = str(umo or "").split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    message_type, session_id = parts[1], parts[2]
    if message_type == "GroupMessage":
        return ("group", session_id)
    if message_type == "FriendMessage":
        return ("friend", session_id)
    return None


def session_blacklisted(umo: str, entries) -> bool:
    """这个会话是否在跨会话黑名单里（纯函数，读取侧与查询侧共用）。

    UMO 形如 ``{platform_id}:{message_type}:{session_id}``：``G:`` 只对
    ``GroupMessage`` 生效、``F:`` 只对 ``FriendMessage`` 生效（私聊与群聊的 id 空间不同，
    不区分类型就会出现"拉黑一个群，结果把同号的私聊也拉了"）。
    """
    key = session_key(umo)
    if key is None:
        return False
    return any(
        parse_blacklist_entry(item) == key
        for item in normalize_session_blacklist(entries)
    )


def effective_cross_session(
    cross_session: bool,
    umo: str,
    entries,
    *,
    force_current_session: bool = False,
) -> bool:
    """当前会话**实际**是否跨会话读取：黑名单内的会话强制只读本会话。

    ``force_current_session``（公开 API 在缺少 user_id 时的降级）优先于配置；
    黑名单只做减法——它永远不会把没开跨会话的会话变成跨会话。
    """
    if not cross_session or force_current_session:
        return False
    return not session_blacklisted(umo, entries)


def blacklist_exclusion(entries, *, current_umo: str = "") -> tuple[str, dict]:
    """构造"排除黑名单会话"的 SQL 片段 ``(条件, 绑定参数)``；没有条目时返回空串。

    逐条展开成 ``(message_type = 'GroupMessage' AND session_id = :blk_g_i)`` 或
    ``(message_type = 'FriendMessage' AND session_id = :blk_f_i)``——**类型与会话号一起比**，
    所以拉黑一个群不会连带同号的私聊。

    只在**跨 umo 查询**里使用；``current_umo`` 命中的条目会被跳过——名单内的会话即便
    被谁用 ``cross_umo=True`` 直接查，也必须仍能看到**自己**的记录（排除自己是把记忆排空，
    不是"不参与跨会话"）。非跨会话查询只覆盖当前会话，同样不需要排除。
    """
    current = session_key(current_umo)
    conds: list[str] = []
    params: dict = {}
    for i, item in enumerate(normalize_session_blacklist(entries)):
        parsed = parse_blacklist_entry(item)
        if parsed is None or parsed == current:
            continue
        kind, value = parsed
        column = "blk_g" if kind == "group" else "blk_f"
        mtype = "GroupMessage" if kind == "group" else "FriendMessage"
        conds.append(f"(message_type = '{mtype}' AND session_id = :{column}_{i})")
        params[f"{column}_{i}"] = value
    if not conds:
        return "", {}
    return "NOT (" + " OR ".join(conds) + ")", params


def _scope_filter(
    umo: str,
    user_id: Optional[str],
    cross_umo: bool,
    full_group: bool,
    blacklist: Optional[list[str]] = None,
) -> tuple[str, dict]:
    """构造 takeover scope 的 WHERE 条件 + 绑定参数。

    4 种组合（``cross_umo`` × ``full_group``）：

    - F/F：当前 umo 当前 user（默认）
      → ``umo = :scope_umo AND user_id = :scope_uid``
    - T/F：跨 umo 当前 user（群私聊互通）
      → ``platform_id = :scope_pid AND user_id = :scope_uid``
    - F/T：当前 umo 整群（同群所有人）
      → ``umo = :scope_umo``
    - T/T：混合语义 — 当前 umo 整群 + 其他 umo 当前 user
      → ``((umo = :scope_umo) OR (platform_id = :scope_pid AND umo != :scope_umo
        AND user_id = :scope_uid))``

    ``user_id`` 为空时禁止进入跨 UMO scope：仅 ``full_group=True`` 可降级为当前 UMO，
    其他组合返回恒假条件。公开 API 还会进一步把该场景限制在当前 CID。

    ``blacklist``：跨会话黑名单（``G:<群号>`` / ``F:<用户号>``）。**只在跨 umo 时生效**——
    名单里的会话不作为来源出现在别人的记忆里；名单内的会话自己也读不到别人（那一步由调用方
    用 ``effective_cross_session`` 强制关掉 cross_session）。非跨会话查询只覆盖当前会话，
    排掉它自己会让整段记忆变空。
    """
    # 名单内的会话**只覆盖自己**：即便调用方传了 cross_umo=True（例如第三方直接查），
    # 也不允许它读到别的会话——把它降级成非跨 umo（full_group 同会话整群仍照旧）。
    # 插件路径另有 effective_cross_session 先关一次，这里是对 storage 层的兜底。
    if cross_umo and session_blacklisted(umo, blacklist):
        cross_umo = False

    user_id = str(user_id or "").strip()
    pid = umo.split(":", 1)[0] if umo else ""
    has_uid = bool(user_id)

    if not has_uid:
        if full_group:
            return "umo = :scope_umo", {"scope_umo": umo}
        return "1 = 0", {}

    def _with_blacklist(cond: str, params: dict) -> tuple[str, dict]:
        """跨 umo 时追加黑名单排除；非跨 umo 原样返回。"""
        if not cross_umo:
            return cond, params
        excl, excl_params = blacklist_exclusion(blacklist, current_umo=umo)
        if not excl:
            return cond, params
        return f"({cond}) AND {excl}", {**params, **excl_params}

    if cross_umo and full_group:
        cond = (
            "((umo = :scope_umo) "
            "OR (platform_id = :scope_pid AND umo != :scope_umo "
            "AND user_id = :scope_uid))"
        )
        return _with_blacklist(cond, {"scope_umo": umo, "scope_pid": pid, "scope_uid": user_id})

    if cross_umo:
        return _with_blacklist(
            "platform_id = :scope_pid AND user_id = :scope_uid",
            {"scope_pid": pid, "scope_uid": user_id},
        )

    if full_group:
        return "umo = :scope_umo", {"scope_umo": umo}

    return (
        "umo = :scope_umo AND user_id = :scope_uid",
        {"scope_umo": umo, "scope_uid": user_id},
    )


_SELECT_COLS = (
    "SELECT role, content, user_id, message_id, pair_id, llm_status, content_kind, "
    "platform_id, platform_name, message_type, session_id, self_id, "
    "group_id, sender_nickname, raw_timestamp, at_id, reply_id, forward_id, "
    "persona_id, created_at, turn_id, send_status, relation_data, id, recalled_at, "
    "deleted_at"
)

# 列位置索引（与 _SELECT_COLS 一一对应）
# 0 role | 1 content | 2 user_id | 3 message_id | 4 pair_id
# 5 llm_status | 6 content_kind | 7 platform_id | 8 platform_name | 9 message_type
# 10 session_id | 11 self_id | 12 group_id | 13 sender_nickname | 14 raw_timestamp
# 15 at_id | 16 reply_id | 17 forward_id | 18 persona_id | 19 created_at
# 20 turn_id | 21 send_status | 22 relation_data | 23 id | 24 recalled_at
# 25 deleted_at


def _row_to_dict(r, tz: Optional[ZoneInfo] = None) -> dict:
    """把 SELECT 出来的行映射为 dict，content_kind 解析回 list。

    ``tz`` 给定时把 ``created_at`` 从 UTC naive 转为配置时区 naive 输出；同时提供
    ``created_at_utc`` 明确的 UTC ISO 8601 字符串。None 则原样返回 UTC 字符串。
    """
    try:
        kind_list = json.loads(r[6]) if r[6] else []
    except (json.JSONDecodeError, TypeError):
        # 静默降级成 [] 会让这一行从此在任何 content_kind 白名单（json_each）下都不匹配，
        # 等于从上下文里悄悄消失。至少留一条带 record_id 的 DEBUG（类型的原值不是正文）。
        logger.debug(
            "[ChatMemory] content_kind 不是合法 JSON（record_id=%s）: %r",
            r[23] if len(r) > 23 else "-", r[6],
        )
        kind_list = []
    created_at_raw = r[19]
    if tz is not None and created_at_raw:
        try:
            dt = datetime.fromisoformat(str(created_at_raw).replace("T", " "))
            created_at_utc = dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            created_at_str = (
                dt.replace(tzinfo=timezone.utc)
                .astimezone(tz)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (ValueError, TypeError):
            created_at_str = str(created_at_raw)
            created_at_utc = str(created_at_raw)
    else:
        created_at_str = str(created_at_raw)
        created_at_utc = str(created_at_raw)
    relation_data = parse_relation_data(r[22])
    content_template = r[1]
    return {
        "role": r[0],
        "content": render_content_template(content_template, relation_data),
        "content_template": content_template,
        "user_id": r[2],
        "message_id": r[3],
        "pair_id": r[4],
        "llm_status": r[5],
        "content_kind": kind_list,
        "platform_id": r[7],
        "platform_name": r[8],
        "message_type": r[9],
        "session_id": r[10],
        "self_id": r[11],
        "group_id": r[12],
        "sender_nickname": r[13],
        "raw_timestamp": r[14],
        "at_id": r[15],
        "reply_id": r[16],
        "forward_id": r[17],
        "persona_id": r[18],
        "created_at": created_at_str,
        # created_at 保持旧的配置时区 naive 字符串兼容；新调用方应使用明确 UTC 值。
        "created_at_utc": created_at_utc,
        "turn_id": r[20],
        "send_status": r[21],
        "relation_data": relation_data,
        "record_id": int(r[23]),
        # 撤回感知：非空表示该消息已被撤回（NULL=未撤回）
        "recalled_at": r[24] if len(r) > 24 else None,
        # 软删除（schema v7 加列；v8 加的是媒体归档的 content_hash）：
        # 非空表示该行已被查询台标记删除（读路径默认已过滤）
        "deleted_at": r[25] if len(r) > 25 else None,
    }
