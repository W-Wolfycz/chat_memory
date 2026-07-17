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
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    asyncio.run(_run())
    print("真实 SQLAlchemy/aiosqlite 存储集成验证通过")
