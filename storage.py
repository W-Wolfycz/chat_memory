"""chat_memory — 异步 SQLite 存储层。"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS chat_memory_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    pair_id TEXT,
    tag TEXT NOT NULL DEFAULT 'non_llm',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_cm_umo_cid_user ON chat_memory_records (umo, conversation_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_cm_created ON chat_memory_records (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_cm_pair_id ON chat_memory_records (pair_id)",
)

# 旧库（v1.x 无 message_id/pair_id/tag 列）启动时按需补列。SQLite 不支持 ADD COLUMN IF NOT EXISTS，
# 用 PRAGMA table_info 检测。
_MIGRATION_ADD_COLUMNS = (
    ("message_id", "TEXT"),
    ("pair_id", "TEXT"),
    ("tag", "TEXT NOT NULL DEFAULT 'non_llm'"),
)


class DBManager:
    def __init__(self, data_dir: Path):
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

    async def init_db(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with self.engine.begin() as conn:
                await conn.execute(text(_CREATE_TABLE_SQL))
                await self._apply_migrations(conn)
                for idx_sql in _CREATE_INDEX_SQL:
                    await conn.execute(text(idx_sql))
            self._initialized = True

    @staticmethod
    async def _apply_migrations(conn) -> None:
        """检测旧库缺哪些列，按需 ADD COLUMN。"""
        result = await conn.execute(text("PRAGMA table_info(chat_memory_records)"))
        existing = {row[1] for row in result.fetchall()}
        for col_name, col_type in _MIGRATION_ADD_COLUMNS:
            if col_name not in existing:
                await conn.execute(
                    text(
                        f"ALTER TABLE chat_memory_records ADD COLUMN {col_name} {col_type}"
                    )
                )

    async def insert(
        self,
        umo: str,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        message_id: Optional[str] = None,
        pair_id: Optional[str] = None,
        tag: str = "non_llm",
    ) -> None:
        await self.init_db()
        async with self.async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO chat_memory_records "
                    "(umo, conversation_id, user_id, role, content, message_id, pair_id, tag, created_at) "
                    "VALUES (:umo, :cid, :uid, :role, :content, :mid, :pid, :tag, :now)"
                ),
                {
                    "umo": umo,
                    "cid": conversation_id,
                    "uid": user_id,
                    "role": role,
                    "content": content,
                    "mid": message_id,
                    "pid": pair_id,
                    "tag": tag,
                    "now": datetime.now(),
                },
            )
            await session.commit()

    async def update_tag(
        self, umo: str, conversation_id: str, message_id: str, new_tag: str
    ) -> int:
        """更新某条 user 记录的 tag（按 message_id 定位）。返回受影响行数。"""
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "UPDATE chat_memory_records SET tag = :tag "
                    "WHERE umo = :umo AND conversation_id = :cid "
                    "AND message_id = :mid AND role = 'user'"
                ),
                {"tag": new_tag, "umo": umo, "cid": conversation_id, "mid": message_id},
            )
            await session.commit()
            return result.rowcount

    async def query_latest(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        tag_filter: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
    ) -> list[dict]:
        """查询最近 N 条记录（按时间升序返回）。

        ``user_id`` 为 None / 空字符串时不按用户过滤，返回该会话下**所有用户**的混合记录。
        ``tag_filter`` 支持 str 或 list[str]：给定时仅返回 tag 匹配的记录（list 用 IN）。
        ``role_filter`` 给定时仅返回 role 匹配的记录（``'user'`` / ``'assistant'``）。
        """
        await self.init_db()
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
            if tag_filter:
                if isinstance(tag_filter, str):
                    tag_list = [tag_filter]
                else:
                    tag_list = list(tag_filter)
                if tag_list:
                    conditions.append("tag IN :tags")
                    params["tags"] = tag_list
                    expanding_binds.append("tags")

            where = " AND ".join(conditions)
            params["lim"] = limit

            sql_text = text(
                "SELECT role, content, user_id, message_id, pair_id, tag, created_at "
                f"FROM chat_memory_records WHERE {where} "
                "ORDER BY created_at DESC LIMIT :lim"
            )
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, params)
            rows = result.fetchall()
            return [
                {
                    "role": r[0],
                    "content": r[1],
                    "user_id": r[2],
                    "message_id": r[3],
                    "pair_id": r[4],
                    "tag": r[5],
                    "created_at": str(r[6]),
                }
                for r in reversed(rows)
            ]

    async def query_rounds(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        tag_filter: Optional[Union[str, list[str]]] = None,
    ) -> list[list[dict]]:
        """按配对返回对话轮次。每轮保证 ``[user_dict, assistant_dict]`` 两条。

        仅返回**有 assistant 配对**的 user（用 EXISTS 子查询过滤单边），
        因此 ``limit_rounds`` 轮对应 ``2 * limit_rounds`` 条记录。

        ``tag_filter`` 支持 str 或 list[str]：给定时仅保留 user.tag 匹配的轮次
        （如 ``"llm_pending"`` 或 ``["llm_pending", "non_llm"]``）。
        单边 user（无 assistant 配对）请用 ``query_latest`` 查。
        """
        await self.init_db()
        async with self.async_session() as session:
            # 1. 取最近 N 条「有 assistant 配对」的 user
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
            if tag_filter:
                if isinstance(tag_filter, str):
                    tag_list = [tag_filter]
                else:
                    tag_list = list(tag_filter)
                if tag_list:
                    conditions.append("tag IN :tags")
                    user_params["tags"] = tag_list
                    expanding_binds.append("tags")
            where = " AND ".join(conditions)
            user_params["lim"] = limit_rounds

            sql_text = text(
                "SELECT role, content, user_id, message_id, pair_id, tag, created_at "
                f"FROM chat_memory_records WHERE {where} "
                "ORDER BY created_at DESC LIMIT :lim"
            )
            for name in expanding_binds:
                sql_text = sql_text.bindparams(bindparam(name, expanding=True))

            result = await session.execute(sql_text, user_params)
            user_rows = list(reversed(result.fetchall()))  # 升序

            if not user_rows:
                return []

            user_msg_ids = [r[3] for r in user_rows]
            assistant_map: dict[str, list[dict]] = {}
            if user_msg_ids:
                # 2. 一次查所有配对的 assistant（pair_id IN user_msg_ids）
                asst_sql = (
                    "SELECT role, content, user_id, message_id, pair_id, tag, created_at "
                    "FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid AND role = 'assistant' "
                    "AND pair_id IN :pids ORDER BY created_at ASC"
                )
                asst_result = await session.execute(
                    text(asst_sql).bindparams(bindparam("pids", expanding=True)),
                    {"umo": umo, "cid": conversation_id, "pids": user_msg_ids},
                )
                for r in asst_result.fetchall():
                    assistant_map.setdefault(r[4], []).append(
                        {
                            "role": r[0],
                            "content": r[1],
                            "user_id": r[2],
                            "message_id": r[3],
                            "pair_id": r[4],
                            "tag": r[5],
                            "created_at": str(r[6]),
                        }
                    )

            def _row_to_dict(r) -> dict:
                return {
                    "role": r[0],
                    "content": r[1],
                    "user_id": r[2],
                    "message_id": r[3],
                    "pair_id": r[4],
                    "tag": r[5],
                    "created_at": str(r[6]),
                }

            rounds: list[list[dict]] = []
            for r in user_rows:
                entry = [_row_to_dict(r)]
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
