"""使用真实 SQLAlchemy/aiosqlite 的 ChatMemory 存储集成验证。

推荐使用 AstrBot 自带 Python 执行：
    python test/test_storage_integration.py

全部数据写入系统临时目录，不读取或修改 AstrBot 的 plugin_data。
"""

import asyncio
import os
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
        plugin = ChatMemoryPlugin(
            _Context(),
            {
                "context_takeover": {
                    "enable": True,
                    # 旧配置即使仍残留也必须被忽略，身份前缀不可关闭。
                    "prefix_enhance": "off",
                    "llm_status_filter": ["llm_success"],
                    "include_content_kinds": ["text"],
                }
            },
        )
        await plugin.initialize()
        assert plugin.db._initialized is True
        assert plugin.db.db_path.exists()

        umo = "platform_demo:FriendMessage:10001"
        cid = "conversation_lifecycle"
        await plugin.db.insert(
            umo,
            cid,
            "10001",
            "user",
            "公开 API 问题",
            llm_status="llm_pending",
            content_kind=["text"],
            turn_id="turn_public_api",
        )
        await plugin.db.insert(
            umo,
            cid,
            "10001",
            "assistant",
            "公开 API 回答",
            llm_status="llm_success",
            content_kind=["text"],
            turn_id="turn_public_api",
            send_status="prepared",
            update_user_llm_status="llm_success",
        )
        contexts = await plugin.build_takeover_contexts(
            umo=umo,
            user_id="10001",
            conversation_id=cid,
        )
        assert [item["role"] for item in contexts] == ["user", "assistant"]
        assert contexts[0]["content"].endswith("10001: 公开 API 问题")
        assert contexts[0]["_no_save"] is True
        assert contexts[1] == {
            "role": "assistant",
            "content": "公开 API 回答",
            "_no_save": True,
        }
        assert await plugin.build_takeover_contexts(
            umo=umo,
            user_id="",
            conversation_id=cid,
        ) == []

        plugin.ct_cross_session = True
        plugin.ct_full_group = True
        current_group = "platform_demo:GroupMessage:group_demo"
        other_group = "platform_demo:GroupMessage:group_other"
        for group_umo, group_cid, turn_id, question, answer in (
            (
                current_group,
                "conversation_group",
                "turn_group_current",
                "当前群问题",
                "当前群回答",
            ),
            (
                current_group,
                "conversation_group_old",
                "turn_group_old_cid",
                "当前群旧 CID 问题",
                "当前群旧 CID 回答",
            ),
            (
                other_group,
                "conversation_other_group",
                "turn_group_other",
                "其他群问题",
                "其他群回答",
            ),
        ):
            await plugin.db.insert(
                group_umo,
                group_cid,
                "10002",
                "user",
                question,
                llm_status="llm_pending",
                content_kind=["text"],
                turn_id=turn_id,
            )
            await plugin.db.insert(
                group_umo,
                group_cid,
                "10002",
                "assistant",
                answer,
                llm_status="llm_success",
                content_kind=["text"],
                turn_id=turn_id,
                send_status="prepared",
                update_user_llm_status="llm_success",
            )

        # P1 回归：cross_session + full_group + 空 user_id 只能读取当前 UMO + CID。
        group_contexts = await plugin.build_takeover_contexts(
            umo=current_group,
            user_id="",
            conversation_id="conversation_group",
        )
        assert [item["role"] for item in group_contexts] == ["user", "assistant"]
        assert group_contexts[0]["content"].endswith(
            "[发言者] 10002: 当前群问题"
        )
        assert group_contexts[1]["content"] == "当前群回答"
        await plugin.terminate()


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chat_memory_integration_") as tmp:
        tmp_root = Path(tmp)
        previous_cwd = Path.cwd()
        # AstrBot 核心导入时可能按 cwd 创建 data/ 模板；先切到临时目录，保证仓库零副产物。
        os.chdir(tmp_root)
        try:
            from chat_memory.storage import DBManager

            await _verify_plugin_lifecycle(tmp_root)

            legacy_dir = tmp_root / "legacy_v2"
            legacy_dir.mkdir()
            legacy_path = legacy_dir / "chat_memory.db"
            with closing(sqlite3.connect(legacy_path)) as conn:
                conn.execute(
                    "CREATE TABLE chat_memory_records ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, "
                    "conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, "
                    "content TEXT NOT NULL DEFAULT '', message_id TEXT, pair_id TEXT, "
                    "llm_status TEXT NOT NULL DEFAULT '', content_kind TEXT NOT NULL DEFAULT '[]', "
                    "platform_id TEXT, platform_name TEXT, message_type TEXT, session_id TEXT, "
                    "self_id TEXT, group_id TEXT, sender_nickname TEXT, raw_timestamp INTEGER, "
                    "at_id TEXT, reply_id TEXT, forward_id TEXT, persona_id TEXT, "
                    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, turn_id TEXT, "
                    "send_status TEXT NOT NULL DEFAULT '')"
                )
                conn.execute(
                    "INSERT INTO chat_memory_records "
                    "(umo, conversation_id, user_id, role, content, turn_id) "
                    "VALUES ('platform_demo:FriendMessage:10001', 'legacy', '10001', "
                    "'user', '旧记录', 'legacy_turn')"
                )
                conn.execute("PRAGMA user_version = 2")
                conn.commit()
            legacy_db = DBManager(legacy_dir, tz=ZoneInfo("UTC"))
            await legacy_db.init_db()
            legacy_rows = await legacy_db.query_latest(
                "platform_demo:FriendMessage:10001", "legacy", "10001"
            )
            assert legacy_rows[0]["content"] == "旧记录"
            assert legacy_rows[0]["relation_data"] is None
            await legacy_db.engine.dispose()
            with closing(sqlite3.connect(legacy_path)) as conn:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
                assert "relation_data" in {
                    row[1] for row in conn.execute("PRAGMA table_info(chat_memory_records)")
                }

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

                oldest = await db.query_latest(
                    "platform_demo:FriendMessage:10001",
                    "conversation_demo",
                    "10001",
                    limit=1,
                    from_oldest=True,
                )
                latest = await db.query_latest(
                    "platform_demo:FriendMessage:10001",
                    "conversation_demo",
                    "10001",
                    limit=1,
                )
                assert [item["content"] for item in oldest] == ["历史问题"]
                assert [item["content"] for item in latest] == ["当前问题"]

                rounds = await db.query_rounds(
                    "platform_demo:FriendMessage:10001",
                    "conversation_demo",
                    "10001",
                    limit_rounds=10,
                    llm_status="llm_success",
                )
                assert len(rounds) == 1
                assert [item["content"] for item in rounds[0]] == ["历史问题", "历史回答"]

                for turn_id, question, answer in (
                    ("turn_round_old", "最旧轮问题", "最旧轮回答"),
                    ("turn_round_new", "最新轮问题", "最新轮回答"),
                ):
                    await db.insert(
                        "platform_demo:FriendMessage:10001",
                        "conversation_round_order",
                        "10001",
                        "user",
                        question,
                        llm_status="llm_pending",
                        content_kind=["text"],
                        turn_id=turn_id,
                    )
                    await db.insert(
                        "platform_demo:FriendMessage:10001",
                        "conversation_round_order",
                        "10001",
                        "assistant",
                        answer,
                        llm_status="llm_success",
                        content_kind=["text"],
                        turn_id=turn_id,
                        send_status="prepared",
                        update_user_llm_status="llm_success",
                    )
                oldest_round = await db.query_rounds(
                    "platform_demo:FriendMessage:10001",
                    "conversation_round_order",
                    "10001",
                    limit_rounds=1,
                    llm_status="llm_success",
                    from_oldest=True,
                )
                latest_round = await db.query_rounds(
                    "platform_demo:FriendMessage:10001",
                    "conversation_round_order",
                    "10001",
                    limit_rounds=1,
                    llm_status="llm_success",
                )
                assert oldest_round[0][0]["content"] == "最旧轮问题"
                assert latest_round[0][0]["content"] == "最新轮问题"

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
                    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
                    assert conn.execute(
                        "SELECT COUNT(*) FROM chat_memory_records"
                    ).fetchone()[0] == 7
            finally:
                await db.engine.dispose()
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    asyncio.run(_run())
    print("真实 SQLAlchemy/aiosqlite 存储集成验证通过")
