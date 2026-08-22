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
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import text


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
        assert "公开 API 问题" in contexts[0]["content"]
        assert contexts[0]["_no_save"] is True
        assert contexts[1]["role"] == "assistant"
        assert "公开 API 回答" in contexts[1]["content"]
        assert contexts[1].get("_no_save") is True
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
        assert "当前群问题" in group_contexts[0]["content"]
        # 记录未提供昵称：不回退 user_id（账号 ID 不进入 LLM 上下文），用中性 ?
        assert "<cm_nickname>?</cm_nickname>" in group_contexts[0]["content"]
        assert "10002" not in group_contexts[0]["content"]
        assert "当前群回答" in group_contexts[1]["content"]

        # 工具调用上下文（方案 B）：落库 → 回放渲染成 OpenAI 格式 → 追加到接管结果。
        # 复位 cross_session/full_group：主表记录未写 platform_id，跨会话查询会匹配不到。
        plugin.ct_cross_session = False
        plugin.ct_full_group = False
        await plugin.db.insert_tool_record(
            umo, cid, "turn_tool_demo", 1, "draw", '{"prompt": "猫"}',
            "任务 demo_task 已创建，不要再次调用",
        )
        await plugin.db.insert_tool_record(
            umo, cid, "turn_tool_demo", 2, "query", "{}", "运行中：任务 demo_task",
        )
        # 幂等：重复 (turn_id, call_index) 不产生新行
        await plugin.db.insert_tool_record(
            umo, cid, "turn_tool_demo", 1, "draw", '{"prompt": "猫"}',
            "任务 demo_task 已创建，不要再次调用",
        )
        assert await plugin.db.query_tool_records(umo, cid, turn_limit=0) == []

        records = await plugin.db.query_tool_records(umo, cid, turn_limit=2)
        assert [(r["call_index"], r["tool_name"]) for r in records] == [
            (1, "draw"), (2, "query"),
        ]
        rendered = plugin._build_tool_contexts(records)
        assert [m["role"] for m in rendered] == ["assistant", "tool", "tool"]
        assert [c["id"] for c in rendered[0]["tool_calls"]] == [
            "cm_tool_turn_too_0", "cm_tool_turn_too_1",
        ]
        assert rendered[1]["tool_call_id"] == "cm_tool_turn_too_0"
        assert "demo_task" in rendered[1]["content"]

        # 接管结果：turn_tool_demo 轮次不在主表历史中 → 工具段直接丢弃
        contexts = await plugin.build_takeover_contexts(
            umo=umo, user_id="10001", conversation_id=cid,
        )
        assert [m["role"] for m in contexts] == ["user", "assistant"]
        assert not any("cm_tool" in str(m) for m in contexts)

        # turn 匹配主表配对轮次时：工具段插入该轮 user 之后、最终回复之前
        await plugin.db.insert_tool_record(
            umo, cid, "turn_public_api", 1, "draw", "{}", "轮内任务已创建",
        )
        in_round = await plugin.build_takeover_contexts(
            umo=umo, user_id="10001", conversation_id=cid,
        )
        assert [m["role"] for m in in_round] == [
            "user", "assistant", "tool", "assistant",
        ]
        assert in_round[1]["tool_calls"][0]["id"] == "cm_tool_turn_pub_0"
        assert in_round[2]["content"] == "轮内任务已创建"
        assert "公开 API 回答" in in_round[3]["content"]

        # /reset 联动：工具表、媒体归档与主表同 CID 记录一起清除
        media_id = "aa" * 16
        await plugin.db.insert_media_archive(
            media_id, umo, cid, "turn_public_api", "image",
            f"{media_id}.png", "png", "image/png", 128,
        )
        deleted, media_rows = await plugin.db.delete_by_conversation(umo, cid)
        assert deleted == 2  # turn_public_api 的 user + assistant
        assert await plugin.db.query_tool_records(umo, cid, turn_limit=2) == []
        assert await plugin.db.query_media_archive_by_ids([media_id]) == {}
        assert [row["file_name"] for row in media_rows] == [f"{media_id}.png"]

        # 日志配置迁移：旧 log_config 组并入顶层并移除旧组（dict config 仅内存迁移）
        migrate_cfg = {
            "log_with_bot_id": None,
            "log_config": {"log_with_bot_id": True, "debug_to_info": True},
        }
        plugin._config = migrate_cfg
        await plugin._migrate_log_config()
        assert migrate_cfg.get("log_with_bot_id") is True
        assert "log_config" not in migrate_cfg
        assert plugin.log_with_bot_id is True

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
                # 旧 kind 值 face:迁移后应变为 emoji(1.2.5 数据迁移)
                conn.execute(
                    "INSERT INTO chat_memory_records "
                    "(umo, conversation_id, user_id, role, content, content_kind, turn_id) "
                    "VALUES ('platform_demo:FriendMessage:10001', 'legacy', '10001', "
                    "'user', '旧表情', '[\"face\"]', 'legacy_turn_face')"
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
            kinds = {tuple(item["content_kind"]) for item in legacy_rows}
            assert ("emoji",) in kinds
            assert not any("face" in (item["content_kind"] or []) for item in legacy_rows)
            await legacy_db.engine.dispose()
            with closing(sqlite3.connect(legacy_path)) as conn:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
                assert "relation_data" in {
                    row[1] for row in conn.execute("PRAGMA table_info(chat_memory_records)")
                }
                # v2 → v5 迁移应同时建立工具表与媒体归档表
                assert {"chat_memory_tool_records", "chat_memory_media_archive"} <= {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
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

                # 同时间戳跨页必须依靠 (created_at, id) 严格推进，不能用 +1 微秒
                # 跳过仍位于同一时间戳的后续记录。
                for index, kinds in enumerate(
                    (["text"], ["text", "image"], ["text"], ["text"]), 1
                ):
                    await db.insert(
                        "platform_demo:FriendMessage:10001",
                        "conversation_keyset",
                        "10001",
                        "user",
                        f"游标记录{index}",
                        llm_status="llm_success",
                        content_kind=kinds,
                        turn_id=f"turn_keyset_{index}",
                    )
                async with db.async_session() as session:
                    await session.execute(
                        text(
                            "UPDATE chat_memory_records "
                            "SET created_at = '2026-07-23 10:00:00' "
                            "WHERE conversation_id = 'conversation_keyset'"
                        )
                    )
                    await session.commit()

                first_page = await db.query_latest(
                    "platform_demo:FriendMessage:10001",
                    "conversation_keyset",
                    "10001",
                    limit=2,
                    from_oldest=True,
                )
                second_page = await db.query_latest(
                    "platform_demo:FriendMessage:10001",
                    "conversation_keyset",
                    "10001",
                    limit=10,
                    since=datetime(2026, 7, 23, 10, 0, 0),
                    after_id=first_page[-1]["record_id"],
                    from_oldest=True,
                )
                combined_ids = [
                    item["record_id"] for item in first_page + second_page
                ]
                assert len(combined_ids) == len(set(combined_ids)) == 4
                assert [item["content"] for item in first_page + second_page] == [
                    "游标记录1",
                    "游标记录2",
                    "游标记录3",
                    "游标记录4",
                ]

                strict_all = await db.query_latest(
                    "platform_demo:FriendMessage:10001",
                    "conversation_keyset",
                    "10001",
                    limit=10,
                    content_kind=["text"],
                    content_kind_all_match=True,
                    from_oldest=True,
                )
                assert [item["content"] for item in strict_all] == [
                    "游标记录1",
                    "游标记录3",
                    "游标记录4",
                ]

                mixed = await db.query_messages_raw(
                    "platform_demo:FriendMessage:10001",
                    "conversation_demo",
                    "10001",
                    10,
                    {"llm_success", "llm_pending"},
                    exclude_turn_id="turn_current",
                )
                assert [item["content"] for item in mixed] == ["历史问题", "历史回答"]

                # ── 媒体归档表（schema v5）──────────────────────────
                mid1, mid2 = "ab" * 16, "cd" * 16
                await db.insert_media_archive(
                    mid1, "platform_demo:FriendMessage:10001",
                    "conversation_demo", "turn_history", "image",
                    f"{mid1}.jpg", "jpg", "image/jpeg", 111,
                )
                await db.insert_media_archive(
                    mid2, "platform_demo:FriendMessage:10001",
                    "conversation_demo", "turn_history", "file",
                    f"{mid2}.pdf", "pdf", "application/pdf", 222,
                )
                archive_rows = await db.query_media_archive_by_ids(
                    [mid1, mid2, "ee" * 16]
                )
                assert set(archive_rows) == {mid1, mid2}
                assert archive_rows[mid1]["mime_type"] == "image/jpeg"
                assert archive_rows[mid1]["size_bytes"] == 111
                assert await db.media_archive_total_size() == 333
                cleanup_rows = await db.query_media_archive_for_cleanup(limit=1)
                assert [row["media_id"] for row in cleanup_rows] == [mid1]
                assert await db.delete_media_archive_by_ids([mid1]) == 1
                assert await db.media_archive_total_size() == 222

                # 超期级联：旧记录与其媒体行一起被 delete_old 清除（不影响 2026 年记录）
                old_mid = "ef" * 16
                await db.insert(
                    "platform_demo:FriendMessage:10001",
                    "conversation_old_media",
                    "10001",
                    "user",
                    "老媒体消息",
                    llm_status="",
                    content_kind=["image"],
                    turn_id="turn_old_media",
                )
                await db.insert_media_archive(
                    old_mid, "platform_demo:FriendMessage:10001",
                    "conversation_old_media", "turn_old_media", "image",
                    f"{old_mid}.png", "png", "image/png", 99,
                )
                async with db.async_session() as session:
                    await session.execute(
                        text(
                            "UPDATE chat_memory_records SET created_at = "
                            "'2020-01-01 00:00:00' "
                            "WHERE conversation_id = 'conversation_old_media'"
                        )
                    )
                    await session.execute(
                        text(
                            "UPDATE chat_memory_media_archive SET created_at = "
                            "'2020-01-01 00:00:00' WHERE media_id = :media_id"
                        ),
                        {"media_id": old_mid},
                    )
                    await session.commit()
                deleted_old, deleted_old_media = await db.delete_old(
                    datetime(2021, 1, 1)
                )
                assert deleted_old == 1
                assert [row["file_name"] for row in deleted_old_media] == [
                    f"{old_mid}.png"
                ]
                assert await db.query_media_archive_by_ids([old_mid]) == {}

                await db.engine.dispose()
                with closing(sqlite3.connect(db.db_path)) as conn:
                    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
                    assert conn.execute(
                        "SELECT COUNT(*) FROM chat_memory_records"
                    ).fetchone()[0] == 11
            finally:
                await db.engine.dispose()
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    asyncio.run(_run())
    print("真实 SQLAlchemy/aiosqlite 存储集成验证通过")
