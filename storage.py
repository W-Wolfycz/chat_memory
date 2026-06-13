"""chat_memory — 异步 SQLite 存储层。"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS chat_memory_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_cm_umo_cid_user ON chat_memory_records (umo, conversation_id, user_id)",
    "CREATE INDEX IF NOT EXISTS ix_cm_created ON chat_memory_records (created_at)",
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
                for idx_sql in _CREATE_INDEX_SQL:
                    await conn.execute(text(idx_sql))
            self._initialized = True

    async def insert(self, umo: str, conversation_id: str, user_id: str, role: str, content: str):
        await self.init_db()
        async with self.async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO chat_memory_records (umo, conversation_id, user_id, role, content, created_at) "
                    "VALUES (:umo, :cid, :uid, :role, :content, :now)"
                ),
                {"umo": umo, "cid": conversation_id, "uid": user_id, "role": role, "content": content, "now": datetime.now()},
            )
            await session.commit()

    async def query_latest(
        self, umo: str, conversation_id: str, user_id: str, limit: int = 20
    ) -> list[dict]:
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT role, content, created_at FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid AND user_id = :uid "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"umo": umo, "cid": conversation_id, "uid": user_id, "lim": limit},
            )
            rows = result.fetchall()
            return [
                {"role": r[0], "content": r[1], "created_at": str(r[2])}
                for r in reversed(rows)
            ]

    async def query(
        self,
        umo: str,
        conversation_id: str,
        user_id: str,
        limit: int = 20,
        role: Optional[str] = None,
        before: Optional[datetime] = None,
        after: Optional[datetime] = None,
    ) -> list[dict]:
        await self.init_db()
        conditions = ["umo = :umo", "conversation_id = :cid", "user_id = :uid"]
        params: dict = {"umo": umo, "cid": conversation_id, "uid": user_id, "lim": limit}
        if role:
            conditions.append("role = :role")
            params["role"] = role
        if before:
            conditions.append("created_at < :before")
            params["before"] = before
        if after:
            conditions.append("created_at > :after")
            params["after"] = after
        where = " AND ".join(conditions)
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    f"SELECT role, content, created_at FROM chat_memory_records "
                    f"WHERE {where} ORDER BY created_at DESC LIMIT :lim"
                ),
                params,
            )
            rows = result.fetchall()
            return [
                {"role": r[0], "content": r[1], "created_at": str(r[2])}
                for r in reversed(rows)
            ]

    async def count_conversation(self, umo: str, conversation_id: str) -> int:
        """统计某个 conversation_id 下的总记录数（不限用户）。"""
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

    async def count(self, umo: str, conversation_id: str, user_id: str) -> int:
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM chat_memory_records "
                    "WHERE umo = :umo AND conversation_id = :cid AND user_id = :uid"
                ),
                {"umo": umo, "cid": conversation_id, "uid": user_id},
            )
            return result.scalar() or 0

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
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text("DELETE FROM chat_memory_records WHERE created_at < :before"),
                {"before": before},
            )
            await session.commit()
            return result.rowcount

    async def delete_user(self, umo: str, user_id: str) -> int:
        await self.init_db()
        async with self.async_session() as session:
            result = await session.execute(
                text(
                    "DELETE FROM chat_memory_records "
                    "WHERE umo = :umo AND user_id = :uid"
                ),
                {"umo": umo, "uid": user_id},
            )
            await session.commit()
            return result.rowcount
