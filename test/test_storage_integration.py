"""使用真实 SQLAlchemy/aiosqlite 的 ChatMemory 存储集成验证。

推荐使用 AstrBot 自带 Python 执行：
    python test/test_storage_integration.py

全部数据写入系统临时目录，不读取或修改 AstrBot 的 plugin_data。
"""

import asyncio
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
astrbot_app_dir = Path(sys.executable).resolve().parent.parent / "app"
if astrbot_app_dir.is_dir():
    sys.path.insert(0, str(astrbot_app_dir))

from chat_memory.storage import DBManager  # noqa: E402


async def _verify_plugin_lifecycle(tmp_root: Path) -> None:
    """使用真实 AstrBot Star 类加载插件，但把数据目录重定向到临时目录。"""
    from chat_memory.main import ChatMemoryPlugin

    class _Context:
        def get_config(self, *args, **kwargs):
            return {"timezone": "UTC"}

    lifecycle_dir = tmp_root / "lifecycle"
    with patch(
        "chat_memory.main.StarTools.get_data_dir",
        return_value=lifecycle_dir,
    ):
        plugin = ChatMemoryPlugin(_Context(), {})
        await plugin.initialize()
        assert plugin.db._initialized is True
        assert plugin.db.db_path.exists()
        await plugin.terminate()


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chat_memory_integration_") as tmp:
        tmp_root = Path(tmp)
        await _verify_plugin_lifecycle(tmp_root)

        db = DBManager(tmp_root / "storage", tz=ZoneInfo("UTC"))
        try:
            await db.init_db()

            await db.insert(
                "platform_demo:FriendMessage:10001",
                "conversation_demo",
                "10001",
                "user",
                "历史问题",
                llm_status="llm_pending",
                content_kind=["text"],
                turn_id="turn_history",
            )
            await db.insert(
                "platform_demo:FriendMessage:10001",
                "conversation_demo",
                "10001",
                "assistant",
                "历史回答",
                llm_status="llm_success",
                content_kind=["text"],
                turn_id="turn_history",
                send_status="prepared",
                update_user_llm_status="llm_success",
            )
            await db.insert(
                "platform_demo:FriendMessage:10001",
                "conversation_demo",
                "10001",
                "user",
                "当前问题",
                llm_status="llm_pending",
                content_kind=["text"],
                turn_id="turn_current",
            )

            rounds = await db.query_rounds(
                "platform_demo:FriendMessage:10001",
                "conversation_demo",
                "10001",
                limit_rounds=10,
                llm_status="llm_success",
            )
            assert len(rounds) == 1
            assert [item["content"] for item in rounds[0]] == ["历史问题", "历史回答"]

            mixed = await db.query_messages_raw(
                "platform_demo:FriendMessage:10001",
                "conversation_demo",
                "10001",
                10,
                {"llm_success", "llm_pending"},
                exclude_turn_id="turn_current",
            )
            assert [item["content"] for item in mixed] == ["历史问题", "历史回答"]

            await db.engine.dispose()
            with closing(sqlite3.connect(db.db_path)) as conn:
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
                assert conn.execute(
                    "SELECT COUNT(*) FROM chat_memory_records"
                ).fetchone()[0] == 3
        finally:
            await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
    print("真实 SQLAlchemy/aiosqlite 存储集成验证通过")
