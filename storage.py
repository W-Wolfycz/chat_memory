"""chat_memory — 异步 SQLite 存储层（v2.3 双列状态）。

Schema 说明：
- ``llm_status``：LLM 配对状态（单值）。'' = 默认（未走 LLM），
  其他取值：'llm_pending' / 'llm_success' / 'proactive' / 'orphan'。
- ``content_kind``：消息内容形态（JSON 数组字符串，如 '["text","image"]'）。
  支持值：'text' / 'image' / 'video' / 'voice' / 'file' / 'face' / 'forward'
  / 'system_event'。空数组 '[]' 表示 empty（如纯 @ 无文本）。
- ``at_id`` / ``reply_id`` / ``forward_id``：上下文引用 ID，仅在对应组件出现时存。

老库（v2.x）不兼容，启动时 PRAGMA 检测到缺 ``llm_status`` 列则 RENAME 备份后重建（不直接 DROP）。
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


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
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_cm_umo_cid_user ON chat_memory_records (umo, conversation_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_cm_created ON chat_memory_records (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_cm_pair_id ON chat_memory_records (pair_id)",
    "CREATE INDEX IF NOT EXISTS ix_cm_platform_group_time ON chat_memory_records (platform_id, group_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_cm_llm_status ON chat_memory_records (llm_status)",
    "CREATE INDEX IF NOT EXISTS ix_cm_umo_role_time ON chat_memory_records (umo, role, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_cm_persona ON chat_memory_records (persona_id)",
)


class DBManager:
    def __init__(self, data_dir: Path, tz: Optional[ZoneInfo] = None):
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
        self._register_pragmas()

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
                # v2.3 不做迁移：检测到老 schema（缺 llm_status 列）→ RENAME 备份后重建
                # 不直接 DROP，给用户事后手动恢复的机会（备份表保留在同一个 .db 文件里）
                result = await conn.execute(text("PRAGMA table_info(chat_memory_records)"))
                existing_cols = {row[1] for row in result.fetchall()}
                if existing_cols and "llm_status" not in existing_cols:
                    import time as _time
                    backup_name = f"chat_memory_records_backup_{int(_time.time())}"
                    await conn.execute(text(
                        f"ALTER TABLE chat_memory_records RENAME TO {backup_name}"
                    ))
                    # 备份表的旧索引会跟着改名或保留（SQLite 行为），这里不主动清理
                    existing_cols = set()  # 触发下面 CREATE 新表
                elif existing_cols and "persona_id" not in existing_cols:
                    # v2.3.3 老库：已有 llm_status 但缺 persona_id，纯增列补上
                    await conn.execute(text(
                        "ALTER TABLE chat_memory_records ADD COLUMN persona_id TEXT"
                    ))
                await conn.execute(text(_CREATE_TABLE_SQL))
                for idx_sql in _CREATE_INDEX_SQL:
                    await conn.execute(text(idx_sql))
            self._initialized = True

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
    ) -> None:
        await self.init_db()
        kind_json = json.dumps(content_kind or [], ensure_ascii=False)
        async with self.async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO chat_memory_records "
                    "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
                    "llm_status, content_kind, "
                    "platform_id, platform_name, message_type, session_id, self_id, "
                    "group_id, sender_nickname, raw_timestamp, "
                    "at_id, reply_id, forward_id, persona_id, created_at) "
                    "VALUES (:umo, :cid, :uid, :role, :content, :mid, :pid, "
                    ":lstatus, :ckind, "
                    ":pid_plat, :pname, :mtype, :sid, :self_id, "
                    ":gid, :nick, :rts, :at_id, :reply_id, :fwd_id, :per_id, :now)"
                ),
                {
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
                },
            )
            await session.commit()

    async def update_llm_status(
        self, umo: str, conversation_id: str, message_id: str, new_status: str
    ) -> int:
        """更新某条 user 记录的 llm_status（按 message_id 定位）。返回受影响行数。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "UPDATE chat_memory_records SET llm_status = :status "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND message_id = :mid AND role = 'user'"
                ),
                {"status": new_status, "umo": umo, "cid": conversation_id, "mid": message_id},
            )
            await session.commit()
            return result.rowcount

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
    ) -> list[dict]:
        """查询最近 N 条记录（按时间升序返回）。

        ``user_id`` 为 None / 空字符串时不按用户过滤，返回该会话下所有用户的混合记录。
        ``llm_status`` 支持 str 或 list[str]：按 LLM 状态过滤（list 用 IN）。
        ``content_kind`` 支持 str 或 list[str]：返回 content_kind JSON 数组中**任一包含**这些值的记录。
        ``role_filter`` 给定时仅返回 role 匹配的记录。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``（与 takeover 对齐）。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤时间窗口（含端点）；
        ``datetime`` 为 tz-aware 时自动转 UTC naive，naive 假定已是 UTC（与落库 ``CURRENT_TIMESTAMP`` 对齐）。

        ``limit`` 钳到 ``[1, 1000]``：防第三方调用方传 -1 触发 SQLite ``LIMIT -1``（=不限制）导致全库返回。
        """
        await self.init_db()
        limit = max(1, min(1000, int(limit)))
        async with self.async_session() as session:
            conditions = ["umo = :umo", "conversation_id = :cid"]
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
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value IN :kinds)"
                    )
                    params["kinds"] = kind_list
                    expanding_binds.append("kinds")

            since_norm = _normalize_dt(since)
            until_norm = _normalize_dt(until)
            if since_norm:
                conditions.append("created_at >= :since")
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

            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           "ORDER BY created_at DESC, id DESC LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, params)
            rows = result.fetchall()
            return [_row_to_dict(r, self._tz) for r in reversed(rows)]

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
    ) -> list[list[dict]]:
        """按配对返回对话轮次。每轮保证 ``[user_dict, assistant_dict]`` 两条。

        仅返回**有 assistant 配对**的 user（用 EXISTS 子查询过滤单边），
        因此 ``limit_rounds`` 轮对应 ``2 * limit_rounds`` 条记录。

        ``llm_status`` / ``content_kind`` 仅过滤 user 侧（assistant 仍按配对字段返回）。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``。user + assistant
        都加条件，保证配对同 persona。EXISTS 子查不加 persona 条件，保持"有配对"语义——若配对
        assistant 的 persona 与 ``persona_id`` 不一致，该 user 仍会被选中但 assistant 查询
        返回空，结果是 ``[user_dict]`` 一条。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤（user + assistant 都加，保证返回
        记录都在时间窗口内；EXISTS 子查不限时间）；``datetime`` tz-aware 自动转 UTC naive。

        ``limit_rounds`` 钳到 ``[1, 1000]``：防第三方调用方传 -1 触发 SQLite ``LIMIT -1``（=不限制）。
        """
        await self.init_db()
        limit_rounds = max(1, min(1000, int(limit_rounds)))
        async with self.async_session() as session:
            conditions = [
                "umo = :umo",
                "conversation_id = :cid",
                "role = 'user'",
                (
                    "EXISTS (SELECT 1 FROM chat_memory_records a "
                    "WHERE a.umo = chat_memory_records.umo "
                    "AND a.conversation_id = chat_memory_records.conversation_id "
                    "AND a.role = 'assistant' AND a.pair_id = chat_memory_records.message_id)"
                ),
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
                    conditions.append(
                        "EXISTS (SELECT 1 FROM json_each(content_kind) "
                        "WHERE value IN :kinds)"
                    )
                    user_params["kinds"] = kind_list
                    expanding_binds.append("kinds")

            since_norm = _normalize_dt(since)
            until_norm = _normalize_dt(until)
            if since_norm:
                conditions.append("created_at >= :since")
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

            where = " AND ".join(conditions)
            user_params["lim"] = limit_rounds

            sql_text = text(_SELECT_COLS + f" FROM chat_memory_records WHERE {where} "
                                           "ORDER BY created_at DESC, id DESC LIMIT :lim")
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, user_params)
            user_rows = list(reversed(result.fetchall()))  # 升序

            if not user_rows:
                return []

            user_msg_ids = [r[3] for r in user_rows]
            assistant_map: dict[str, list[dict]] = {}
            if user_msg_ids:
                asst_conditions = [
                    "umo = :umo",
                    "conversation_id = :cid",
                    "role = 'assistant'",
                    "pair_id IN :pids",
                ]
                asst_params: dict = {
                    "umo": umo, "cid": conversation_id, "pids": user_msg_ids,
                }
                if since_norm:
                    asst_conditions.append("created_at >= :since")
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
                    text(asst_sql).bindparams(bindparam("pids", expanding=True)),
                    asst_params,
                )
                for r in asst_result.fetchall():
                    # key 用 pair_id（r[4]），而非 message_id（r[3]，assistant 恒为 None）
                    assistant_map.setdefault(r[4], []).append(_row_to_dict(r, self._tz))

            rounds: list[list[dict]] = []
            for r in user_rows:
                entry = [_row_to_dict(r, self._tz)]
                entry.extend(assistant_map.get(r[3], []))
                rounds.append(entry)
            return rounds

    async def delete_by_conversation(self, umo: str, conversation_id: str) -> int:
        """清除某个 conversation_id 下的所有记录（用于 /reset）。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "DELETE FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            await session.commit()
            return result.rowcount

    async def count_by_conversation(self, umo: str, conversation_id: str) -> int:
        """统计某 conversation_id 下的记录数（用于 /reset 删除前审计痕迹）。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid"
                ),
                {"umo": umo, "cid": conversation_id},
            )
            return result.scalar() or 0

    async def delete_old(self, before: datetime) -> int:
        """删除 created_at 早于 ``before`` 的所有记录（用于 auto_cleanup_days）。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text("DELETE FROM chat_memory_records WHERE created_at < :before"),
                {"before": before},
            )
            await session.commit()
            return result.rowcount

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
        user/assistant 在同一 umo 内配对（依赖 message_id 全平台唯一）。

        ``include_kinds``：白名单语义，空集合 = 不过滤；非空时配合 ``all_match``：
        - ``all_match=False`` (ANY)：record 的 content_kind 与白名单**任一交集**即保留
        - ``all_match=True`` (ALL)：record 的 content_kind **全部**属于白名单（且非空）才保留
        仅在 user 查询时过滤（assistant 不过滤，与配对语义一致）。

        ``filter_by_persona``：开启后按 ``persona_id`` 严格过滤（user + assistant
        两处都加条件，保证配对同 persona；EXISTS 子查不加，保持"有配对"语义）；
        ``persona_id`` 为空时过滤 ``IS NULL OR ''`` 的记录（匹配老库补列后的旧行）。
        """
        await self.init_db()
        async with self.async_session() as session:
            scope_cond, scope_param = _scope_filter(umo, user_id, cross_umo, full_group)
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
                "AND a.pair_id = chat_memory_records.message_id)",
            ])
            conditions = [
                scope_cond,
                "role = 'user'",
                " ".join(exists_parts),
            ]
            params: dict = {**scope_param, "lim": limit_rounds}
            expanding_binds: list[str] = []

            if conversation_id:
                conditions.append("conversation_id = :cid")
                params["cid"] = conversation_id

            # persona 过滤：user + assistant 两处一致（EXISTS 不加，保持"有配对"语义）
            persona_cond = None
            if filter_by_persona:
                if persona_id:
                    persona_cond = "persona_id = :persona_id"
                    conditions.append(persona_cond)
                    params["persona_id"] = persona_id
                else:
                    persona_cond = "(persona_id IS NULL OR persona_id = '')"
                    conditions.append(persona_cond)

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

            user_msg_ids = [r[3] for r in user_rows]  # message_id 在第 4 列
            assistant_map: dict[str, list[dict]] = {}
            if user_msg_ids:
                # assistant 查询：与 user 同 scope（保证配对在范围内），按 pair_id 配对
                asst_conditions = [scope_cond, "role = 'assistant'", "pair_id IN :pids"]
                asst_params = {**scope_param, "pids": user_msg_ids}
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
                    text(asst_sql).bindparams(bindparam("pids", expanding=True)),
                    asst_params,
                )
                for r in asst_result.fetchall():
                    # key 用 pair_id（r[4]）：assistant.message_id 恒为 None，用 r[3] 会全部 miss
                    assistant_map.setdefault(r[4], []).append(_row_to_dict(r, self._tz))

            rounds: list[list[dict]] = []
            for r in user_rows:
                entry = [_row_to_dict(r, self._tz)]
                entry.extend(assistant_map.get(r[3], []))
                rounds.append(entry)
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
        """
        await self.init_db()
        async with self.async_session() as session:
            scope_cond, scope_param = _scope_filter(umo, user_id, cross_umo, full_group)
            conditions = [
                scope_cond,
                "role IN ('user', 'assistant')",
            ]
            params: dict = {**scope_param, "lim": limit_messages}
            expanding_binds: list[str] = []

            if conversation_id:
                conditions.append("conversation_id = :cid")
                params["cid"] = conversation_id

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


# SELECT 列顺序固定，_row_to_dict 按位置映射
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


def _scope_filter(
    umo: str,
    user_id: Optional[str],
    cross_umo: bool,
    full_group: bool,
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
      → ``((umo = :scope_umo) OR (platform_id = :scope_pid AND umo != :scope_umo AND user_id = :scope_uid))``

    ``user_id`` 为空时：所有 ``user_id`` 限制退化为不加（等价于整群/整 platform），
    与 2.3.3 之前的行为一致（兜底处理无 sender_id 的边缘平台）。
    """
    pid = umo.split(":", 1)[0] if umo else ""
    has_uid = bool(user_id)

    if cross_umo and full_group:
        if has_uid:
            cond = (
                "((umo = :scope_umo) "
                "OR (platform_id = :scope_pid AND umo != :scope_umo "
                "AND user_id = :scope_uid))"
            )
            return cond, {"scope_umo": umo, "scope_pid": pid, "scope_uid": user_id}
        return "platform_id = :scope_pid", {"scope_pid": pid}

    if cross_umo:
        if has_uid:
            return (
                "platform_id = :scope_pid AND user_id = :scope_uid",
                {"scope_pid": pid, "scope_uid": user_id},
            )
        return "platform_id = :scope_pid", {"scope_pid": pid}

    if full_group:
        return "umo = :scope_umo", {"scope_umo": umo}

    if has_uid:
        return (
            "umo = :scope_umo AND user_id = :scope_uid",
            {"scope_umo": umo, "scope_uid": user_id},
        )
    return "umo = :scope_umo", {"scope_umo": umo}


_SELECT_COLS = (
    "SELECT role, content, user_id, message_id, pair_id, llm_status, content_kind, "
    "platform_id, platform_name, message_type, session_id, self_id, "
    "group_id, sender_nickname, raw_timestamp, at_id, reply_id, forward_id, "
    "persona_id, created_at"
)

# 列位置索引（与 _SELECT_COLS 一一对应）
# 0 role | 1 content | 2 user_id | 3 message_id | 4 pair_id
# 5 llm_status | 6 content_kind | 7 platform_id | 8 platform_name | 9 message_type
# 10 session_id | 11 self_id | 12 group_id | 13 sender_nickname | 14 raw_timestamp
# 15 at_id | 16 reply_id | 17 forward_id | 18 persona_id | 19 created_at


def _row_to_dict(r, tz: Optional[ZoneInfo] = None) -> dict:
    """把 SELECT 出来的行映射为 dict，content_kind 解析回 list。

    ``tz`` 给定时把 ``created_at`` 从 UTC naive 转为配置时区 naive 输出；
    None 则原样返回 UTC naive 字符串（与落库 ``CURRENT_TIMESTAMP`` 一致）。
    """
    try:
        kind_list = json.loads(r[6]) if r[6] else []
    except (json.JSONDecodeError, TypeError):
        kind_list = []
    created_at_raw = r[19]
    if tz is not None and created_at_raw:
        try:
            dt = datetime.fromisoformat(str(created_at_raw).replace("T", " "))
            created_at_str = (
                dt.replace(tzinfo=timezone.utc)
                .astimezone(tz)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except (ValueError, TypeError):
            created_at_str = str(created_at_raw)
    else:
        created_at_str = str(created_at_raw)
    return {
        "role": r[0],
        "content": r[1],
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
    }
