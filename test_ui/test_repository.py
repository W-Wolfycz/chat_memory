from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from chat_memory.ui.repository import ChatMemoryRepository, DatabaseUnavailableError


SCHEMA = """
CREATE TABLE chat_memory_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    pair_id TEXT,
    llm_status TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '[]',
    platform_id TEXT,
    platform_name TEXT,
    message_type TEXT,
    session_id TEXT,
    self_id TEXT,
    group_id TEXT,
    sender_nickname TEXT,
    raw_timestamp INTEGER,
    at_id TEXT,
    reply_id TEXT,
    forward_id TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    persona_id TEXT,
    turn_id TEXT,
    send_status TEXT NOT NULL DEFAULT '',
    relation_data TEXT
)
"""


TOOL_SCHEMA = """
CREATE TABLE chat_memory_tool_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tool_args TEXT NOT NULL DEFAULT '',
    tool_result TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def make_database(path: Path, include_tools: bool = True) -> None:
    connection = sqlite3.connect(path)
    connection.execute(SCHEMA)
    connection.execute(f"PRAGMA user_version = {4 if include_tools else 3}")
    rows = [
        (
            "bot:FriendMessage:u1", "cid-a", "u1", "user",
            "你好 ⟦CM_AT:0⟧", "m1", None, "llm_success", '["text"]',
            "bot", "aiocqhttp", "FriendMessage", "u1", "bot-id", "", "Zulu-old",
            1, None, None, None, "2026-08-10 01:00:00", "persona-a", "turn-a", "",
            json.dumps({"v": 1, "mentions": [{"nickname": "Bob"}], "reply": None}, ensure_ascii=False),
        ),
        (
            "bot:FriendMessage:u1", "cid-a", "u1", "assistant",
            "你好，Alice", None, "m1", "llm_success", '["text"]',
            "bot", "aiocqhttp", "FriendMessage", "u1", "bot-id", "", "Alpha-new",
            2, None, None, None, "2026-08-10 01:00:01", "persona-a", "turn-a", "send_attempted", None,
        ),
        (
            "bot:GroupMessage:g1", "cid-b", "u2", "user",
            "只有图片", "m2", None, "", '["image"]',
            "bot", "aiocqhttp", "GroupMessage", "g1", "bot-id", "g1", "Carol",
            3, None, None, None, "2026-08-11 02:00:00", None, "turn-b", "", None,
        ),
        (
            "bot:GroupMessage:g1", "cid-b", "u2", "assistant",
            "主动消息", None, None, "proactive", '["text"]',
            "bot", "aiocqhttp", "GroupMessage", "g1", "bot-id", "g1", "Carol",
            4, None, None, None, "2026-08-12 03:00:00", None, "turn-c", "prepared", None,
        ),
    ]
    connection.executemany(
        """
        INSERT INTO chat_memory_records (
            umo, conversation_id, user_id, role, content, message_id, pair_id,
            llm_status, content_kind, platform_id, platform_name, message_type,
            session_id, self_id, group_id, sender_nickname, raw_timestamp,
            at_id, reply_id, forward_id, created_at, persona_id, turn_id,
            send_status, relation_data
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    if include_tools:
        connection.execute(TOOL_SCHEMA)
        connection.executemany(
            """
            INSERT INTO chat_memory_tool_records (
                umo, conversation_id, turn_id, call_index, tool_name,
                tool_args, tool_result, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "bot:FriendMessage:u1", "cid-a", "turn-a", 1, "draw",
                    '{"prompt": "猫"}', "任务 x 已创建", "2026-08-10 01:00:02",
                ),
                (
                    "bot:FriendMessage:u1", "cid-a", "turn-a", 2, "query",
                    "{}", "运行中：任务 x", "2026-08-10 01:00:04",
                ),
                (
                    "bot:GroupMessage:g1", "cid-b", "turn-b", 1, "draw",
                    '{"prompt": "狗"}', "第二任务已创建", "2026-08-11 02:00:02",
                ),
            ],
        )
    connection.commit()
    connection.close()


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat_memory.db"
        make_database(self.db_path)
        self.repository = ChatMemoryRepository(
            self.db_path,
            timezone=timezone(timedelta(hours=8), name="Asia/Shanghai"),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_overview_and_health(self):
        overview = asyncio.run(self.repository.overview())
        self.assertEqual(overview["summary"]["total_records"], 4)
        self.assertEqual(overview["summary"]["total_conversations"], 2)
        self.assertEqual(overview["summary"]["total_users"], 2)
        self.assertEqual(overview["database"]["user_version"], 4)
        self.assertEqual(overview["database"]["mode"], "wal_aware")
        self.assertTrue(overview["database"]["tool_table_present"])
        self.assertTrue(overview["tools"]["present"])
        self.assertEqual(overview["tools"]["total_calls"], 3)
        self.assertEqual(overview["tools"]["tool_names"]["draw"], 2)

        health = asyncio.run(self.repository.health())
        self.assertTrue(health["healthy"])
        self.assertEqual(health["total_records"], 4)

    def test_tool_records_query_and_filters(self):
        result = asyncio.run(
            self.repository.tool_records(
                {"tool_name": "draw", "page": 1, "page_size": 20}
            )
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {item["tool_name"] for item in result["items"]}, {"draw"}
        )
        first = result["items"][0]
        self.assertEqual(first["tool_args"], '{"prompt": "狗"}')
        self.assertEqual(first["created_at"], "2026-08-11 10:00:02")

        by_turn = asyncio.run(
            self.repository.tool_records(
                {"turn_id": "turn-a", "page_size": 50}
            )
        )
        self.assertEqual([item["call_index"] for item in by_turn["items"]], [2, 1])

        scoped = asyncio.run(
            self.repository.tool_records(
                {"umo": "bot:GroupMessage:g1", "conversation_id": "cid-b",
                 "page_size": 50}
            )
        )
        self.assertEqual(scoped["total"], 1)

    def test_tool_records_degrade_without_tool_table(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        make_database(legacy_path, include_tools=False)
        legacy_repo = ChatMemoryRepository(
            legacy_path,
            timezone=timezone(timedelta(hours=8), name="Asia/Shanghai"),
        )
        overview = asyncio.run(legacy_repo.overview())
        self.assertFalse(overview["tools"]["present"])
        self.assertFalse(overview["database"]["tool_table_present"])
        result = asyncio.run(legacy_repo.tool_records({"page_size": 20}))
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])

    def test_query_filters_and_renders_relations(self):
        result = asyncio.run(
            self.repository.query(
                {
                    "keyword": "Bob",
                    "llm_status": "llm_success",
                    "content_kind": "text",
                    "page": 1,
                    "page_size": 20,
                }
            )
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["content"], "你好 @Bob")
        self.assertEqual(result["items"][0]["created_at"], "2026-08-10 09:00:00")

    def test_empty_status_persona_and_paired_only(self):
        no_llm = asyncio.run(
            self.repository.query({"llm_status": "__empty__", "page_size": 50})
        )
        self.assertEqual([item["record_id"] for item in no_llm["items"]], [3])

        legacy_persona = asyncio.run(
            self.repository.query({"persona_id": "__empty__", "page_size": 50})
        )
        self.assertEqual(legacy_persona["total"], 2)

        paired = asyncio.run(
            self.repository.query({"paired_only": True, "page_size": 50})
        )
        self.assertEqual(paired["total"], 2)
        self.assertEqual({item["turn_id"] for item in paired["items"]}, {"turn-a"})

    def test_facets_are_scoped(self):
        facets = asyncio.run(
            self.repository.facets(
                {"umo": "bot:FriendMessage:u1", "conversation_id": "cid-a"}
            )
        )
        self.assertEqual(len(facets["users"]), 1)
        self.assertEqual(facets["users"][0]["nickname"], "Alpha-new")
        self.assertEqual(facets["conversations"][0]["value"], "cid-a")

    def test_facets_are_mutually_scoped_and_nickname_tracks_latest_match(self):
        user_facets = asyncio.run(
            self.repository.facets(
                {"umo": "bot:FriendMessage:u1", "role": "user"}
            )
        )
        self.assertEqual(user_facets["users"][0]["nickname"], "Zulu-old")
        self.assertEqual(
            {item["value"] for item in user_facets["roles"]},
            {"user", "assistant"},
        )
        self.assertEqual(
            {item["value"] for item in user_facets["statuses"]},
            {"llm_success"},
        )

        assistant_facets = asyncio.run(
            self.repository.facets(
                {"umo": "bot:FriendMessage:u1", "role": "assistant"}
            )
        )
        self.assertEqual(assistant_facets["users"][0]["nickname"], "Alpha-new")
        self.assertEqual(
            {item["value"] for item in assistant_facets["kinds"]},
            {"text"},
        )

    def test_corrupt_wal_fallback_is_reported(self):
        original_connect = self.repository._connect

        def fake_connect(*, immutable: bool):
            if not immutable:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return original_connect(immutable=True)

        with patch.object(self.repository, "_connect", side_effect=fake_connect):
            result = asyncio.run(self.repository.overview())
        self.assertEqual(result["database"]["mode"], "main_snapshot")
        self.assertIn("WAL/SHM", result["database"]["warning"])

    def test_corrupt_database_without_fallback_fails(self):
        repository = ChatMemoryRepository(
            self.db_path, allow_immutable_fallback=False
        )

        def fail_connect(*, immutable: bool):
            raise sqlite3.DatabaseError("database disk image is malformed")

        with patch.object(repository, "_connect", side_effect=fail_connect):
            with self.assertRaisesRegex(DatabaseUnavailableError, "不可读"):
                asyncio.run(repository.health())

    def test_query_rejects_bad_time_range(self):
        with self.assertRaisesRegex(ValueError, "起始时间"):
            asyncio.run(
                self.repository.query(
                    {
                        "since": "2026-08-12T10:00",
                        "until": "2026-08-11T10:00",
                    }
                )
            )

    def test_reply_view_turn_and_snapshot(self):
        path = Path(self.temp_dir.name) / "reply.db"
        make_database(path)
        connection = sqlite3.connect(path)
        connection.execute(
            """
            INSERT INTO chat_memory_records (
                umo, conversation_id, user_id, role, content, llm_status,
                content_kind, platform_id, created_at, turn_id, sender_nickname,
                send_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "bot:FriendMessage:u1", "cid-reply", "u2", "user", "被引用的那句话",
                "", '["text"]', "bot", "2026-08-13 05:00:00", "turn-target", "目标昵称", "",
            ),
        )
        connection.execute(
            """
            INSERT INTO chat_memory_records (
                umo, conversation_id, user_id, role, content, llm_status,
                content_kind, platform_id, created_at, turn_id, relation_data,
                send_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "bot:FriendMessage:u1", "cid-reply", "u1", "user", "我同意",
                "", '["text"]', "bot", "2026-08-13 05:01:00", "turn-reply",
                json.dumps({"v": 1, "mentions": [], "reply": {
                    "resolution": "turn", "target_turn_id": "turn-target",
                    "target_role": "user"}}, ensure_ascii=False),
                "",
            ),
        )
        connection.execute(
            """
            INSERT INTO chat_memory_records (
                umo, conversation_id, user_id, role, content, llm_status,
                content_kind, platform_id, created_at, turn_id, relation_data,
                send_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "bot:FriendMessage:u1", "cid-reply", "u1", "user", "快照回复",
                "", '["text"]', "bot", "2026-08-13 05:02:00", "turn-snap",
                json.dumps({"v": 1, "mentions": [], "reply": {
                    "resolution": "snapshot", "target_nickname": "快照昵称",
                    "fallback_text": "快照原文"}}, ensure_ascii=False),
                "",
            ),
        )
        connection.commit()
        connection.close()

        repo = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        result = asyncio.run(
            repo.query({"umo": "bot:FriendMessage:u1", "page_size": 50})
        )
        by_turn = {item["turn_id"]: item for item in result["items"]}
        turn_view = by_turn["turn-reply"]["reply_view"]
        self.assertEqual(turn_view["resolution"], "turn")
        self.assertEqual(turn_view["target"], "目标昵称")
        self.assertEqual(turn_view["text"], "被引用的那句话")
        snap_view = by_turn["turn-snap"]["reply_view"]
        self.assertEqual(snap_view["resolution"], "snapshot")
        self.assertEqual(snap_view["target"], "快照昵称")
        self.assertEqual(snap_view["text"], "快照原文")
        # 无 relation 的记录 reply_view 为 None
        self.assertIsNone(by_turn["turn-a"]["reply_view"])

        # 详情接口同样返回 reply_view(单条目标查询)
        detail = asyncio.run(repo.record(result["items"][0]["record_id"]))
        self.assertIn("reply_view", detail["record"])

    def test_media_view_and_archive_join(self):
        path = Path(self.temp_dir.name) / "media.db"
        make_database(path)
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE chat_memory_media_archive ("
            "media_id TEXT PRIMARY KEY, umo TEXT NOT NULL, "
            "conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, file_name TEXT NOT NULL, ext TEXT NOT NULL, "
            "mime_type TEXT, size_bytes INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        )
        media_id = "ab" * 16
        connection.execute(
            "INSERT INTO chat_memory_media_archive VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                media_id, "bot:GroupMessage:g1", "cid-b", "turn-b", "image",
                f"{media_id}.jpg", "jpg", "image/jpeg", 12345, "2026-08-11 02:00:00",
            ),
        )
        relation = json.dumps({
            "v": 1, "mentions": [],
            "media": [
                {"kind": "image", "id": media_id},
                {"kind": "file", "id": "cd" * 16, "name": "课程表.pdf"},
                {"kind": "poke", "id": "10001", "type": "666"},
                {"kind": "emoji", "id": "123"},
            ],
        }, ensure_ascii=False)
        connection.execute(
            "UPDATE chat_memory_records SET relation_data = ? WHERE turn_id = ?",
            (relation, "turn-b"),
        )
        connection.commit()
        connection.close()

        repo = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        result = asyncio.run(
            repo.query({"umo": "bot:GroupMessage:g1", "page_size": 50})
        )
        by_turn = {item["turn_id"]: item for item in result["items"]}
        view = by_turn["turn-b"]["media_view"]
        self.assertEqual([item["kind"] for item in view],
                         ["image", "file", "poke", "emoji"])
        image = view[0]
        self.assertTrue(image["archived"])
        self.assertEqual(image["media_id"], media_id)
        self.assertEqual(image["mime"], "image/jpeg")
        self.assertEqual(image["size"], 12345)
        # 未归档文件：archived=False，但 name 保留
        file_item = view[1]
        self.assertFalse(file_item["archived"])
        self.assertEqual(file_item["name"], "课程表.pdf")
        # poke 标签映射；未知类型显示原始编号
        self.assertEqual(view[2]["poke_label"], "比心")
        self.assertEqual(view[2]["id"], "10001")
        # media_file 单查：命中与未命中
        row = asyncio.run(repo.media_file(media_id))
        self.assertEqual(row["kind"], "image")
        with self.assertRaises(ValueError):
            asyncio.run(repo.media_file("ff" * 16))

    def test_media_overview_presence(self):
        path = Path(self.temp_dir.name) / "media_ov.db"
        make_database(path)
        repo = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        overview = asyncio.run(repo.overview())
        self.assertFalse(overview["media"]["present"])
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE chat_memory_media_archive ("
            "media_id TEXT PRIMARY KEY, umo TEXT NOT NULL, "
            "conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, file_name TEXT NOT NULL, ext TEXT NOT NULL, "
            "mime_type TEXT, size_bytes INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.execute(
            "INSERT INTO chat_memory_media_archive VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ab" * 16, "umo_demo", "cid_demo", "turn_demo", "voice",
             "ab" * 16 + ".amr", "amr", "audio/amr", 2048, "2026-08-11 02:00:00"),
        )
        connection.commit()
        connection.close()
        overview = asyncio.run(repo.overview())
        self.assertTrue(overview["media"]["present"])
        self.assertEqual(overview["media"]["total_files"], 1)
        self.assertEqual(overview["media"]["total_bytes"], 2048)

    def test_pure_media_english_placeholder_localized(self):
        """纯媒体英文占位本地化：media 非空才翻，用户真输入 '[image]' 不翻。"""
        path = Path(self.temp_dir.name) / "localize.db"
        make_database(path)
        connection = sqlite3.connect(path)
        rows = [
            # 纯媒体（media 非空）：占位应本地化为 ⟦图片⟧
            ("bot:FriendMessage:u1", "cid-loc", "u1", "user", "[image]",
             '["image"]', "turn-pure",
             json.dumps({"v": 1, "mentions": [], "media": [
                 {"kind": "image", "id": "ab" * 16}]}, ensure_ascii=False)),
            # 用户真的输入 "[image]"（media 空）：保持原样
            ("bot:FriendMessage:u1", "cid-loc", "u1", "user", "[image]",
             '["text"]', "turn-typed",
             json.dumps({"v": 1, "mentions": [], "media": []}, ensure_ascii=False)),
            # 无 relation 的旧记录：历史纯媒体占位同样本地化
            ("bot:FriendMessage:u1", "cid-loc", "u1", "user", "[image]",
             '["image"]', "turn-legacy", None),
            # 无 relation 的旧记录 + text kind：用户手打的 "[image]" 不得误翻
            ("bot:FriendMessage:u1", "cid-loc", "u1", "user", "[image]",
             '["text"]', "turn-legacy-text", None),
            # media 非空但全是 poke：字面 "[image]" 不得误翻（按占位 kind 精确判断）
            ("bot:FriendMessage:u1", "cid-loc", "u1", "user", "[image]",
             '["text", "poke"]', "turn-mixed-poke",
             json.dumps({"v": 1, "mentions": [], "media": [
                 {"kind": "poke", "id": "10001", "type": "666"}]}, ensure_ascii=False)),
        ]
        for row in rows:
            connection.execute(
                "INSERT INTO chat_memory_records (umo, conversation_id, user_id, "
                "role, content, content_kind, turn_id, relation_data) "
                "VALUES (?,?,?,?,?,?,?,?)",
                row,
            )
        connection.commit()
        connection.close()
        repo = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        result = asyncio.run(repo.query({"page_size": 50}))
        by_turn = {item["turn_id"]: item["content"] for item in result["items"]}
        self.assertEqual(by_turn["turn-pure"], "⟦图片⟧")
        self.assertEqual(by_turn["turn-typed"], "[image]")
        self.assertEqual(by_turn["turn-legacy"], "⟦图片⟧")
        self.assertEqual(by_turn["turn-legacy-text"], "[image]")
        self.assertEqual(by_turn["turn-mixed-poke"], "[image]")

    def test_media_file_missing_table_and_detail_view(self):
        """media_file 表缺失 → ValueError（400 语义）；详情接口同样带 media_view。"""
        path = Path(self.temp_dir.name) / "media_detail.db"
        make_database(path)
        connection = sqlite3.connect(path)
        media_id = "ab" * 16
        relation = json.dumps({"v": 1, "mentions": [], "media": [
            {"kind": "image", "id": media_id}]}, ensure_ascii=False)
        connection.execute(
            "UPDATE chat_memory_records SET relation_data = ? WHERE turn_id = ?",
            (relation, "turn-b"),
        )
        connection.commit()
        connection.close()
        repo = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        # 表不存在：不报 DatabaseUnavailableError，而是 ValueError（媒体未归档语义）
        with self.assertRaises(ValueError):
            asyncio.run(repo.media_file(media_id))
        # 详情接口返回 media_view（未归档）
        result = asyncio.run(repo.query({"page_size": 50}))
        item = next(x for x in result["items"] if x["turn_id"] == "turn-b")
        detail = asyncio.run(repo.record(item["record_id"]))
        self.assertEqual(len(detail["record"]["media_view"]), 1)
        self.assertFalse(detail["record"]["media_view"][0]["archived"])


if __name__ == "__main__":
    unittest.main()
