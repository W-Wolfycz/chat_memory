"""chat_memory — 异步 SQLite 存储层。

数据落在合并存储（``chat_memory_messages`` / ``chat_memory_message_bots`` /
``chat_memory_reply_messages`` / ``chat_memory_tool_calls``）；旧表
``chat_memory_records`` / ``chat_memory_tool_records`` 自 2.1 起不再读写。

Schema 说明：
- ``llm_status``：LLM 配对状态（单值）。'' = 默认（未走 LLM），
  其他取值：'llm_pending' / 'llm_success' / 'proactive' / 'orphan'。
- ``content_kind``：消息内容形态（JSON 数组字符串，如 '["text","image"]'）。
  支持值：'text' / 'image' / 'video' / 'voice' / 'file' / 'emoji' / 'forward' / 'poke'
  / 'event'。空数组 '[]' 表示 empty（如纯 @ 无文本）。
- ``at_id`` / ``reply_id`` / ``forward_id``：上下文引用 ID，合并存储里折进 ``relation_data``。
- ``turn_id``：内部轮次 ID，新记录不依赖平台 message_id 即可配对。
- ``send_status``：assistant 发送流程状态（prepared / send_attempted），不表示平台回执。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from astrbot.api import logger

from . import shadow_read, shared_store
from .models import VALID_LLM_STATUSES, VALID_SEND_STATUSES


# 媒体归档（schema v5）：user 侧媒体落盘后登记，CM_UI 据此回看。
# 只在归档成功时插行；失败不落行。文件路径不落库：由 file_name + created_at
# 推导（media/<YYYYMM>/<内容哈希>.<ext>）；v8 起文件名是内容寻址的哈希，不含隐私标识。
_MEDIA_ARCHIVE_TABLE_NAME = "chat_memory_media_archive"

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

async def _mirror_record_payload(
    session,
    payload: dict,
    platform_timestamp,
    platform_uid,
    post_type: str,
) -> bool:
    """只写这一条到合并存储（供平台回显使用，不写计划行）。"""
    return await shared_store.mirror_record(
        session,
        {
            **payload,
            "pts": platform_timestamp,
            "puid": platform_uid,
            "post_type": post_type,
        },
    )


async def _collect_media_rows(session, where: str, params: dict) -> list[dict]:
    """取待删归档行（file_name + created_at），供调用方删物理文件。

    必须在**同一个事务**里、删行之前取：先提交删行再取会漏掉刚落库的行，
    取不到就等于文件永久留在磁盘上。``where`` 只接受本模块内写死的条件片段。
    """
    result = await session.execute(
        text(f"SELECT file_name, created_at FROM chat_memory_media_archive WHERE {where}"),
        params,
    )
    return [{"file_name": row[0], "created_at": row[1]} for row in result.fetchall()]


async def _preserve_shadow_tables(conn, recorded: str) -> list[str]:
    """不认识的旧结构：把合并存储表改名留档，而不是直接 DROP。

    ``DROP`` 会把用户全部聊天历史一次删光，而返回值仍是"加载成功"——一次结构编号
    异常就等于数据丢失。改名后数据仍在这份库里（可用任何 SQLite 工具导出），
    新结构照常建立，代价只是多占一份空间，由用户自己决定何时清理。

    留档表名**唯一**：同名已存在时追加序号（`_before_99` → `_before_99_2` → …），
    绝不删掉上一份。重跑一次不代表上一份留档可以先扔——那是同样的用户数据，
    再次丢弃等于把"别删数据"的初衷做反。

    返回实际留档的新表名（空列表＝没有可留档的表，例如全新库）。
    """
    keep = (
        "chat_memory_messages",
        "chat_memory_message_bots",
        "chat_memory_reply_messages",
        "chat_memory_tool_calls",
        "chat_memory_session_seq",
        "chat_memory_shadow_meta",
    )
    base = "_before_" + "".join(
        ch if ch.isalnum() else "_" for ch in str(recorded or "unknown")
    )[:20]
    preserved: list[str] = []
    for table in keep:
        exists = await conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"
            ),
            {"name": table},
        )
        if exists.first() is None:
            continue
        renamed = await _unique_table_name(conn, f"{table}{base}")
        # 表名来自本函数内的白名单与清洗过的版本串，仍需防御性校验后拼进 DDL
        # （SQLite 不支持把标识符做成绑定参数）。
        if not (table.isidentifier() and renamed.isidentifier()):
            raise RuntimeError(f"留档表名不合法：{renamed!r}")
        await conn.execute(text(f'ALTER TABLE "{table}" RENAME TO "{renamed}"'))
        preserved.append(renamed)
    return preserved


async def _unique_table_name(conn, base: str) -> str:
    """在 ``base`` 上找一个尚未占用的表名（``x`` → ``x_2`` → ``x_3`` …）。

    留档必须**每份都留**：同名就换个序号，不覆盖。重跑一次结构异常说明用户又
    多了一份历史数据，上一份留档不是废料。
    """
    candidate = base
    for n in range(2, 1000):
        exists = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": candidate},
        )
        if exists.first() is None:
            return candidate
        candidate = f"{base}_{n}"
    raise RuntimeError(f"留档表名用尽：{base}")


async def _shadow_step_is_noop(conn, statement: str) -> tuple[bool, str]:
    """合并存储的迁移步骤是否"已经生效过"（只对可重复执行的步骤判定）。

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
        # 合并存储是唯一存储：写入只落新表。
        self.shadow_enabled = bool(shadow)
        self.shadow_stats = {"writes": 0, "skipped": 0, "failures": 0, "last_error": ""}
        # 写入链路出现的"非运行期异常"（代码/结构问题）只 ERROR 报一次，避免刷屏
        self._shadow_code_error_logged = False
        self._register_pragmas()

    async def _shadow_write(self, operation) -> bool:
        """把一条数据写进合并存储；返回是否**真的写入**了。

        写失败必须完全隔离：只累加计数 + 记异常类型（异常串可能带 SQL 参数
        ＝聊天正文），不让一次写失败拖垮调用方的其余流程。

        返回假（数据不足被跳过 / 存储未启用 / 写异常）时调用方**不得**当成功上报：
        平台回显曾因此记成 ``echo/fetched`` 而实际一行没落，线上看着像通道正常。
        """
        if not self.shadow_enabled:
            return False
        try:
            async with self.async_session() as session:
                mirrored = await operation(session)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - 写入链路整体兜底
            self.shadow_stats["failures"] += 1
            self.shadow_stats["last_error"] = type(exc).__name__
            # DBAPI/驱动类错误是可重试的运行期故障；其它（NameError/AttributeError/
            # ProgrammingError＝代码或结构问题）第一次就以 ERROR 报一次——它们不会自愈，
            # 只记 DEBUG 会让"从来没写全"一路静默下去。
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
                # 旧表（chat_memory_records / chat_memory_tool_records）自 2.1 起不再
                # 建表、不再迁移、不再读写；用户自己清掉之前它们就静静躺在库里。
                # 媒体归档表仍是本插件的数据，继续维护。
                await conn.execute(text(_CREATE_MEDIA_ARCHIVE_TABLE_SQL))
                # 存量库（v7 及更早建的媒体归档表）缺 content_hash 列：索引建在该列上，
                # 不先补列会以 `no such column: content_hash` 在加载阶段崩掉。
                media_info = await conn.execute(
                    text(f"PRAGMA table_info({_MEDIA_ARCHIVE_TABLE_NAME})")
                )
                media_columns = {row[1] for row in media_info.fetchall()}
                if "content_hash" not in media_columns:
                    await conn.execute(
                        text(
                            f"ALTER TABLE {_MEDIA_ARCHIVE_TABLE_NAME} "
                            "ADD COLUMN content_hash TEXT"
                        )
                    )
                for statement in _MEDIA_ARCHIVE_INDEX_DEFINITIONS.values():
                    await conn.execute(text(statement))
                # 防止同名索引残留在别的表上时 `IF NOT EXISTS` 静默跳过：
                # 逐个确认索引真绑在媒体归档表上。注意 `.items()` 的值是**建索引 SQL**，
                # 不能拿来当期望表名（那会让校验永远不通过，插件加载即失败）。
                for index_name in _MEDIA_ARCHIVE_INDEX_DEFINITIONS:
                    index_result = await conn.execute(
                        text(
                            "SELECT tbl_name FROM sqlite_master "
                            "WHERE type = 'index' AND name = :name"
                        ),
                        {"name": index_name},
                    )
                    bound_table = index_result.scalar()
                    if bound_table != _MEDIA_ARCHIVE_TABLE_NAME:
                        raise RuntimeError(
                            f"ChatMemory index {index_name} 未绑定到 "
                            f"{_MEDIA_ARCHIVE_TABLE_NAME}（实际：{bound_table or '不存在'}）"
                        )
                if self.shadow_enabled:
                    # 合并存储表：结构版本记在 chat_memory_shadow_meta。结构升级
                    # （版本不等）时按迁移步骤走；不认识的旧结构丢弃重建。
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
                            # 认识的旧结构走**迁移**（数据与起点时间都保住）；不认识的
                            # 才丢弃重建。
                            steps = shared_store.shadow_migration_statements(recorded)
                            if steps is None:
                                # 不认识的旧编号：**不能直接丢弃重建**——那是把用户
                                # 全部聊天历史一次删光，而且看起来像"加载成功"。改为把
                                # 旧表改名留档（数据仍在这份库里，可人工导出/恢复），
                                # 再按当前结构建一套空的；日志点名留档表名。
                                kept = await conn.execute(
                                    text(
                                        "SELECT value FROM chat_memory_shadow_meta "
                                        "WHERE key = 'dual_write_started_at'"
                                    )
                                )
                                carried_start = str(kept.scalar() or "")
                                preserved = await _preserve_shadow_tables(
                                    conn, recorded or "unknown"
                                )
                                for statement in shared_store.drop_shadow_statements():
                                    await conn.execute(text(statement))
                                migrate_trace = "rebuild:unknown_version"
                                if preserved:
                                    logger.warning(
                                        "[ChatMemory] 合并存储结构版本无法识别（记录值 %r），"
                                        "已把旧表改名留档，不会自动删除：%s。"
                                        "需要旧数据请直接从这些表导出；确认不要后自行 DROP。",
                                        recorded or "空", "、".join(preserved),
                                    )
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
                                    "双写起点沿用）",
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
                    # 起点时间：只在第一次建立合并存储时写一次。有历史数据就取最早的
                    # 一条（升级上来的接着算，不白扔）；全新库取当前时刻。
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
                # 旧表已停用：不再写 user_version（那是旧表的结构版本号，写了反而误导）
            self._initialized = True

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

    async def shadow_meta(self) -> dict[str, str]:
        """读合并存储的结构元信息（``chat_memory_shadow_meta`` 整表键值）。

        供 ``get_shared_store_status`` 这类只读状态接口用：键只有几个、值都是短字符串，
        一次查完比按需逐键查询省事。存储未启用或读失败返回空字典（调用方给默认值）。
        """
        if not self.shadow_enabled:
            return {}
        try:
            await self.init_db()
            async with self.async_session() as session:
                result = await session.execute(
                    text("SELECT key, value FROM chat_memory_shadow_meta")
                )
                return {str(key): str(value) for key, value in result.all()}
        except Exception as exc:  # noqa: BLE001 - 读不到就当没有
            logger.debug(f"[ChatMemory] 结构元信息读取失败: {type(exc).__name__}")
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
        except Exception as exc:  # noqa: BLE001 - 回显通道的兜底查 cid
            # DB 故障与"确实没有 cid"必须分得开：前者会让回显整条通道静默断流
            # （表现为 push 一直 no_cid、库里 origin=echo 零行，日志里看不出原因）。
            logger.warning(
                "[ChatMemory] 回显查 cid 失败（本次按无 cid 处理）: %s",
                type(exc).__name__,
            )
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

        ``shadow_only=True``：**只**写这一条、不更新计划行状态。用于"自己发言的实际
        形态"——它由两条供货通道之一带回（push＝NapCat 回显，主通道；pull＝发送后拉
        群历史，兜底），发送路径已经写过计划行，重复写会让读路径看到两份。

        返回值：``bool(mirrored)``——这条记录是否**真的**写进了合并存储。
        返回 ``False`` 时上层必须当失败处理（别把"这轮没写进去"记成写成功、
        assistant 照旧升级 llm_status）。

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
            # 平台回显：只写这一条（发送路径已写过计划行，不能写第二份）
            return await self._shadow_write(
                lambda session: _mirror_record_payload(
                    session, payload, platform_timestamp, platform_uid, post_type
                )
            )
        # assistant 最终写入时，把对应 user 状态升级放在同一事务中。
        if role == "assistant" and update_user_llm_status is not None and not turn_id:
            raise ValueError("assistant finalize requires turn_id")

        async def _mirror_record_and_status(session):
            """写记录本身；assistant 落库顺带提升 user 行的 ``llm_status``。"""
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

        # 合并存储是唯一存储：按 ``msg_uid`` / 计划行主键 upsert（计划行"只补空"），
        # 重复写幂等——多段回复、补捕获重试都不会撞唯一键。
        mirrored = await self._shadow_write(_mirror_record_and_status)
        if not mirrored and self.shadow_enabled:
            # 开着却没写进去才是真失败：让调用方看见（否则这轮 user 没落库、
            # assistant 却会被标 llm_success）。只记 id/状态，不带正文。
            logger.warning(
                "[ChatMemory] 记录未落库: role=%s turn=%s send_status=%s",
                role, turn_id or "-", send_status or "-",
            )
        return bool(mirrored)

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
        updated = 0

        async def _mark(session):
            nonlocal updated
            updated = await shared_store.mirror_send_status(
                session, umo, conversation_id, turn_id, new_status
            )
            return True

        await self._shadow_write(_mark)
        return int(updated or 0)

    async def update_llm_status_by_turn(
        self, umo: str, conversation_id: str, turn_id: str, new_status: str
    ) -> int:
        """按内部 turn_id 更新 user 的 llm_status，覆盖无平台 message_id 的平台。"""
        await self.init_db()
        if new_status not in VALID_LLM_STATUSES:
            raise ValueError(f"unsupported llm_status: {new_status}")
        updated = 0

        async def _mark(session):
            nonlocal updated
            updated = await shared_store.mirror_llm_status(
                session, umo, conversation_id, turn_id, new_status
            )
            return True

        await self._shadow_write(_mark)
        return int(updated or 0)

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
        """写入一条工具调用记录（``(turn_id, call_index)`` 幂等）。

        返回**这次是否新插入了行**：幂等键已存在时行本来就在，对调用方来说是"这一轮
        的工具段确实在库里"，不是失败。所以这里不能直接把 ``mirror_tool_call`` 的
        ``rowcount == 0`` 交给 ``_shadow_write``——那会把它记进 ``skipped`` 污染健康度，
        上层若按失败重试还会永久重试同一个幂等键（事件重放、插件重载都会触发）。
        """
        if not umo or not conversation_id or not turn_id:
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        inserted = {"new": False}

        async def _write(session):
            inserted["new"] = await shared_store.mirror_tool_call(
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
            return True

        await self._shadow_write(_write)
        return bool(inserted["new"])

    async def delete_by_conversation(
        self, umo: str, conversation_id: str
    ) -> tuple[int, list[dict]]:
        """清除某个 conversation_id 下的所有记录（用于 /reset）。工具表与媒体归档同步清理。

        返回 ``(删除的记录数, 被删归档行列表)``；归档行含 file_name/created_at，
        由调用方负责删除对应媒体文件。

        **媒体行与消息必须同一个事务**：拆成两个事务时，消息删除失败但媒体行已提交，
        调用方拿到非空的 ``media_rows`` 就会把物理文件也删掉——消息还在库里、图永久
        没了，而返回值又像"什么都没删"。失败一律外抛，由调用方决定是否继续删文件。
        """
        await self.init_db()
        if not self.shadow_enabled:
            # 没有合并存储就没有"消息"可删；媒体归档仍是本插件数据，照常清理。
            async with self.async_session() as session:
                media_rows = await _collect_media_rows(
                    session,
                    "umo = :umo AND conversation_id = :cid",
                    {"umo": umo, "cid": conversation_id},
                )
                await session.execute(
                    text(
                        "DELETE FROM chat_memory_media_archive "
                        "WHERE umo = :umo AND conversation_id = :cid"
                    ),
                    {"umo": umo, "cid": conversation_id},
                )
                await session.commit()
            return 0, media_rows

        deleted = {"rows": 0, "media": []}

        async def _drop(session):
            deleted["media"] = await _collect_media_rows(
                session,
                "umo = :umo AND conversation_id = :cid",
                {"umo": umo, "cid": conversation_id},
            )
            await session.execute(
                text(
                    "DELETE FROM chat_memory_media_archive "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            deleted["rows"] = await shared_store.mirror_delete_conversation(
                session, umo, conversation_id
            )
            return True

        if not await self._shadow_write(_drop):
            # 事务已回滚：媒体行没删、消息也没删。抛出去，调用方不得去删物理文件。
            raise RuntimeError(
                f"清除会话存档失败（umo={umo}, cid={conversation_id}），"
                f"未删除任何记录，详见服务端日志"
            )
        return int(deleted["rows"] or 0), deleted["media"]

    async def delete_old(self, before: datetime) -> tuple[int, list[dict]]:
        """删除 created_at 早于 ``before`` 的所有记录（用于 auto_cleanup_days）。

        工具表与媒体归档同步清理；返回 ``(删除的记录数, 被删归档行列表)``。

        与 ``delete_by_conversation`` 同样的约束：媒体行与消息同一个事务，失败外抛，
        否则"消息没删掉、媒体文件却被删了"。
        """
        await self.init_db()
        if not self.shadow_enabled:
            async with self.async_session() as session:
                media_rows = await _collect_media_rows(
                    session, "created_at < :before", {"before": before}
                )
                await session.execute(
                    text("DELETE FROM chat_memory_media_archive WHERE created_at < :before"),
                    {"before": before},
                )
                await session.commit()
            return 0, media_rows

        state = {"rows": 0, "media": []}

        async def _drop(session):
            state["media"] = await _collect_media_rows(
                session, "created_at < :before", {"before": before}
            )
            await session.execute(
                text("DELETE FROM chat_memory_media_archive WHERE created_at < :before"),
                {"before": before},
            )
            state["rows"] = await shared_store.mirror_delete_before(session, before)
            return True

        if not await self._shadow_write(_drop):
            raise RuntimeError(
                f"按保留期清理存档失败（早于 {before}），未删除任何记录，详见服务端日志"
            )
        return int(state["rows"] or 0), state["media"]

    async def purge_orphans(self) -> tuple[int, int]:
        """回收合并存储里的孤儿行（无参与行的消息 / 指向已删消息的参与行）。

        返回 ``(消息数, 参与行数)``。存储未启用时返回 ``(0, 0)``（那是"没有合并存储"，
        不是"没有孤儿"——调用方据此区分，不打日志）。
        """
        if not self.shadow_enabled:
            return (0, 0)
        await self.init_db()
        async with self.async_session() as session:
            removed = await shared_store.purge_orphan_rows(session)
            await session.commit()
            return removed

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

    async def mark_recalled(self, umo: str, message_id: str) -> int:
        """把被撤回的 user 记录标记为已撤回（``recalled_at``，幂等）。

        只标记 role='user' 且尚未标记的行；未命中返回 0。assistant 记录没有
        message_id，天然不会被误标。
        """
        mid = str(message_id or "").strip()
        if not umo or not mid:
            return 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        updated = 0

        async def _mark(session):
            nonlocal updated
            updated = await shared_store.mirror_recall(session, umo, mid, now)
            return True

        await self._shadow_write(_mark)
        return int(updated or 0)

    # ── 读路径（合并存储）──────────────────────────────────────────────
    #
    # 全部读方法都走 `shadow_read`：合并存储是唯一读源，读不到明确报错，
    # 不返回空列表假装没数据。

    async def find_by_message_id_v2(
        self, umo: str, message_id: str
    ) -> Optional[dict]:
        """按平台 message_id 查 user 消息（撤回 notice 的本地关联）。"""
        if not umo or not message_id:
            return None
        scope_cond, scope_params = self._session_scope_v2(umo, None)
        async with self._v2_session() as session:
            rows = await shadow_read.fetch_messages(
                session,
                scope_cond=scope_cond,
                scope_params=scope_params,
                tz=self._tz,
                limit=1,
                role_filter="user",
                from_oldest=False,
                collapse_turns=False,
                extra_conditions=["platform_message_id = :mid"],
                extra_params={"mid": str(message_id)},
            )
        if not rows:
            return None
        row = rows[0]
        return {
            "content": row.get("content") or "",
            "relation_data": row.get("relation_data"),
            "sender_nickname": row.get("sender_nickname") or "",
        }

    async def latest_sender_nickname_v2(
        self, umo: str, user_id: str
    ) -> Optional[str]:
        """按 umo+user_id 取最近一条 user 消息的真实昵称。

        排除空昵称与「昵称=账号」（适配器兜底）；无命中返回 None。
        """
        if not umo or not user_id:
            return None
        scope_cond, scope_params = self._session_scope_v2(umo, user_id)
        async with self._v2_session() as session:
            rows = await shadow_read.fetch_messages(
                session,
                scope_cond=scope_cond,
                scope_params=scope_params,
                tz=self._tz,
                limit=5,
                role_filter="user",
                collapse_turns=False,
            )
        for row in reversed(rows):
            nick = str(row.get("sender_nickname") or "").strip()
            if nick and nick != str(user_id):
                return nick
        return None

    async def resolve_user_reply_target_v2(
        self,
        umo: str,
        platform_id: str,
        reply_id: str,
    ) -> Optional[dict]:
        """用平台 Reply.id 解析同一 UMO 内的 user 轮次；歧义时拒绝猜测。"""
        if not umo or not reply_id:
            return None
        scope_cond, scope_params = self._session_scope_v2(umo, None)
        extra = ["(platform_message_id = :mid)"]
        params = {"mid": str(reply_id)}
        if platform_id:
            extra.append("platform_id = :pid_plat")
            params["pid_plat"] = platform_id
        async with self._v2_session() as session:
            rows = await shadow_read.fetch_messages(
                session,
                scope_cond=scope_cond,
                scope_params=scope_params,
                tz=self._tz,
                limit=2,
                role_filter="user",
                collapse_turns=False,
                extra_conditions=extra,
                extra_params=params,
            )
        if len(rows) != 1:
            return None
        if not str(rows[0].get("turn_id") or ""):
            return None
        return rows[0]

    async def count_records_in_conversation_v2(
        self, umo: str, conversation_id: str
    ) -> list[dict]:
        """当前 CID 下的可见消息按 ``content_kind`` 分组计数（/reset 残留自检）。"""
        scope_cond, scope_params = self._session_scope_v2(umo, None)
        async with self._v2_session() as session:
            result = await session.execute(
                text(
                    "SELECT m.content_kind AS content_kind, COUNT(*) AS n "
                    "FROM chat_memory_message_bots b "
                    "JOIN chat_memory_messages m ON m.msg_uid = b.msg_uid "
                    "WHERE b.umo = :umo AND b.conversation_id = :cid "
                    "AND m.deleted_at IS NULL "
                    "GROUP BY m.content_kind ORDER BY n DESC LIMIT 10"
                ),
                {"umo": umo, "cid": str(conversation_id or "")},
            )
            return [
                {"content_kind": str(row[0] or "[]"), "count": int(row[1] or 0)}
                for row in result.fetchall()
            ]

    async def count_records_by_conversation_v2(
        self, umo: str, exclude_cid: str = ""
    ) -> list[dict]:
        """按 conversation_id 统计同 UMO 的消息数（/reset 残留诊断）。"""
        async with self._v2_session() as session:
            result = await session.execute(
                text(
                    "SELECT b.conversation_id, COUNT(*) AS n "
                    "FROM chat_memory_message_bots b "
                    "JOIN chat_memory_messages m ON m.msg_uid = b.msg_uid "
                    "WHERE b.umo = :umo AND b.conversation_id <> :cid "
                    "AND m.deleted_at IS NULL "
                    "GROUP BY b.conversation_id ORDER BY n DESC LIMIT 200"
                ),
                {"umo": umo, "cid": str(exclude_cid or "")},
            )
            return [
                {"conversation_id": str(row[0] or ""), "count": int(row[1] or 0)}
                for row in result.fetchall()
            ]

    @asynccontextmanager
    async def _v2_session(self):
        """读操作统一的 session 入口：确认合并存储可用 → 建连接 → 交出 session。"""
        if not self.shadow_enabled:
            raise RuntimeError(
                "合并存储未建立，读接口不可用"
            )
        await self.init_db()
        async with self.async_session() as session:
            yield session

    @staticmethod
    def _session_scope_v2(umo: str, user_id: Optional[str]) -> tuple[str, dict]:
        """会话范围条件：与对外查询接口同一套语义。

        ``user_id`` 为空 = 该会话所有用户混排；非空 = ``user_id`` 列匹配（user 行是发送者、
        assistant 行是被回复对象——合并视图里的 ``user_id`` 就是这么算的）。
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
        """``str | list[str] | None`` → ``set[str] | None``（对外查询的 status/kind 参数口径）。"""
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
        skip_recalled: bool = False,
    ) -> list[dict]:
        """按会话范围取消息（消息级读路径）。

        ``record_id`` / ``after_id`` 用**会话内序号**（``seq``）。
        ``skip_recalled``：跳过被撤回的消息（消费者不希望撤回内容进入下游）。
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
                skip_recalled=skip_recalled,
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
        skip_recalled: bool = False,
    ) -> list[list[dict]]:
        """按轮次取 ``[user, assistant]``；assistant 侧是合并后的完整回复。

        ``skip_recalled``：跳过被撤回的 user（其 assistant 侧随之整轮不返回）。
        """
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
                skip_recalled=skip_recalled,
            )

    async def query_tool_records_v2(
        self,
        umo: str,
        conversation_id: str,
        turn_limit: int = 2,
    ) -> list[dict]:
        """读工具调用表，返回同形 dict。"""
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
        """Reply 目标批量读取，key 为 ``(turn_id, role)``。"""
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
        after: Optional[int] = None,
    ) -> list[dict]:
        """takeover 混合模式：按条件取消息（scope 语义与公开查询一致）。"""
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
                after_seq=after,
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
        after: Optional[int] = None,
    ) -> list[list[dict]]:
        """takeover 配对模式：按条件取轮次。

        语义：**不按 llm_status 过滤**——"配对"指这一轮有 assistant 回复
        （由 ``fetch_rounds`` 按 turn_id 挂回时自然保证）。曾经在这里硬编码
        ``{"llm_success"}``，会把所有"有回复但 user 行状态不是 llm_success"
        的轮次整轮丢掉（静默失忆）。
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
                after_seq=after,
            )


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
    private_only: bool = False,
) -> bool:
    """当前会话**实际**是否跨会话读取：黑名单内的会话强制只读本会话。

    ``force_current_session``（公开 API 在缺少 user_id 时的降级）优先于配置；
    ``private_only``：只在私聊里跨会话——群聊即便开了跨会话也只读当前群，
    不把该用户在其他群/私聊说过的话带进群聊上下文。
    黑名单只做减法——它永远不会把没开跨会话的会话变成跨会话。
    """
    if not cross_session or force_current_session:
        return False
    if private_only:
        kind = session_key(umo)
        if kind is None or kind[0] != "friend":
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


