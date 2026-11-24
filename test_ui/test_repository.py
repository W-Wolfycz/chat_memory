"""查询台（repository）读路径：合并存储投影、过滤、软删、媒体、损坏回退。

2.1 起旧表不再参与读取，夹具只造合并存储表。
"""

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

# 合并存储结构（2.1 起唯一存储）：查询台只读这几张表。
SHADOW_DDL = """\
CREATE TABLE IF NOT EXISTS chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_memory_messages (
    msg_uid TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, seq INTEGER NOT NULL,
    platform_name TEXT NOT NULL, message_type TEXT NOT NULL, session_id TEXT NOT NULL,
    sender_id TEXT NOT NULL, sender_nickname TEXT NOT NULL DEFAULT '',
    platform_timestamp INTEGER NOT NULL, content TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '[]', relation_data TEXT, recalled_at DATETIME,
    deleted_at DATETIME, platform_seq INTEGER, created_at DATETIME NOT NULL);
CREATE TABLE IF NOT EXISTS chat_memory_message_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_uid TEXT NOT NULL,
    platform_id TEXT NOT NULL, self_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL, umo TEXT NOT NULL, role TEXT NOT NULL,
    platform_message_id TEXT, turn_id TEXT, llm_status TEXT NOT NULL DEFAULT '',
    send_status TEXT NOT NULL DEFAULT '', persona_id TEXT, target_id TEXT,
    raw_timestamp INTEGER, created_at DATETIME NOT NULL,
    UNIQUE (msg_uid, platform_id, self_id, conversation_id));
"""

TOOL_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chat_memory_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_uid TEXT, umo TEXT NOT NULL,
    conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, call_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL, tool_args TEXT NOT NULL DEFAULT '',
    tool_result TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL,
    deleted_at DATETIME, UNIQUE (turn_id, call_index));
"""

MEDIA_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chat_memory_media_archive (
    media_id TEXT PRIMARY KEY, umo TEXT NOT NULL, conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL, kind TEXT NOT NULL, file_name TEXT NOT NULL,
    content_hash TEXT, ext TEXT NOT NULL, mime_type TEXT, size_bytes INTEGER NOT NULL,
    created_at DATETIME NOT NULL);
"""

# 归档 id 必须是 32 位 hex（查询台只按这个形态查归档表）。
MEDIA_ID_IMAGE = "a1" * 16


def make_database(path: Path, include_tools: bool = True) -> None:
    """造一份只有合并存储的库（与线上 2.1 形态一致）。"""
    connection = sqlite3.connect(path)
    connection.executescript(SHADOW_DDL)
    connection.executescript(MEDIA_SCHEMA)
    connection.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) VALUES ('schema_version', '6')"
    )
    connection.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) "
        "VALUES ('dual_write_started_at', '2025-01-01 00:00:00.000000')"
    )
    messages = [
        # (msg_uid, seq, platform, mtype, session, sender, nick, ts, content,
        #  kind, relation, created_at, platform_seq)
        ("p:a1", 1, "aiocqhttp", "FriendMessage", "u1", "u1", "Zulu-old", 1,
         "你好 ⟦CM_AT:0⟧", '["text"]',
         json.dumps({"v": 1, "mentions": [{"user_id": "u2", "nickname": "Bob"}],
                     "reply": {"resolution": "turn", "target_turn_id": "turn-b"}},
                    ensure_ascii=False),
         "2026-08-10 01:00:00", 1),
        ("p:a2", 2, "aiocqhttp", "FriendMessage", "u1", "bot-id", "Alpha-new", 2,
         "你好，Alice", '["text"]',
         json.dumps({"v": 1, "reply": {"resolution": "snapshot",
                                       "target_nickname": "快照昵称",
                                       "fallback_text": "快照原文"}},
                    ensure_ascii=False),
         "2026-08-10 01:00:01", 2),
        ("p:b1", 3, "aiocqhttp", "GroupMessage", "g1", "u2", "Carol", 3,
         "只有图片", '["image"]',
         json.dumps({"v": 1, "media": [
             {"kind": "image", "id": MEDIA_ID_IMAGE},
             {"kind": "file", "id": "not-hex32", "name": "报告.pdf"},
         ]}, ensure_ascii=False),
         "2026-08-11 02:00:00", 3),
        ("p:c1", 4, "aiocqhttp", "GroupMessage", "g1", "bot-id", "Carol", 4,
         "主动消息", '["text"]', None, "2026-08-12 03:00:00", 4),
    ]
    connection.executemany(
        "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, platform_name, "
        "message_type, session_id, sender_id, sender_nickname, platform_timestamp, "
        "content, content_kind, relation_data, created_at, platform_seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(m[0], m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], m[10],
          m[11], m[12]) for m in messages],
    )
    bots = [
        # (msg_uid, umo, cid, role, mid, turn, llm, send, persona, created_at)
        ("p:a1", "bot:FriendMessage:u1", "cid-a", "user", "m1", "turn-a",
         "llm_success", "", "persona-a", "2026-08-10 01:00:00"),
        ("p:a2", "bot:FriendMessage:u1", "cid-a", "assistant", None, "turn-a",
         "llm_success", "send_attempted", "persona-a", "2026-08-10 01:00:01"),
        ("p:b1", "bot:GroupMessage:g1", "cid-b", "user", "m2", "turn-b", "", "",
         None, "2026-08-11 02:00:00"),
        ("p:c1", "bot:GroupMessage:g1", "cid-b", "assistant", None, "turn-c",
         "proactive", "prepared", None, "2026-08-12 03:00:00"),
    ]
    connection.executemany(
        "INSERT INTO chat_memory_message_bots (msg_uid, platform_id, self_id, "
        "conversation_id, umo, role, platform_message_id, turn_id, llm_status, "
        "send_status, persona_id, raw_timestamp, created_at) "
        "VALUES (?, 'bot', 'bot-id', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        [(b[0], b[2], b[1], b[3], b[4], b[5], b[6], b[7], b[8], b[9])
         for b in bots],
    )
    # 媒体归档行：图片已归档（用于断言 media_view 的归档字段），file 那条故意不归档。
    connection.execute(
        "INSERT INTO chat_memory_media_archive (media_id, umo, conversation_id, "
        "turn_id, kind, file_name, ext, mime_type, size_bytes, created_at) "
        "VALUES (?, 'bot:GroupMessage:g1', 'cid-b', 'turn-b', 'image', "
        "'202608/cat.jpg', 'jpg', 'image/jpeg', 12345, '2026-08-11 02:00:05')",
        (MEDIA_ID_IMAGE,),
    )
    if include_tools:
        connection.execute(TOOL_SCHEMA)
        connection.executemany(
            "INSERT INTO chat_memory_tool_calls (msg_uid, umo, conversation_id, "
            "turn_id, call_index, tool_name, tool_args, tool_result, created_at) "
            "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("bot:FriendMessage:u1", "cid-a", "turn-a", 1, "draw",
                 '{"prompt": "猫"}', "任务 x 已创建", "2026-08-10 01:00:02"),
                ("bot:FriendMessage:u1", "cid-a", "turn-a", 2, "query",
                 "{}", "运行中：任务 x", "2026-08-10 01:00:04"),
                ("bot:GroupMessage:g1", "cid-b", "turn-b", 1, "draw",
                 '{"prompt": "狗"}', "第二任务已创建", "2026-08-11 02:00:02"),
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
        # 查询台复用一条只读连接（旧历史去重缓存在它的 TEMP 表里）：
        # Windows 上不先关掉，临时目录就删不掉。
        self.repository.close()
        self.temp_dir.cleanup()

    def test_soft_delete_filter_and_restore(self):
        """守的静默失效：软删除三态筛选与写入口。

        ① 默认列表若不排除 `deleted_at` 非空的行，被删的消息照旧出现——用户以为删了；
        ② 工具行 id 没有合并存储的 `_MERGED_ID_OFFSET` 偏移，写路径若照记录那套减偏移
           去查会算成负 rowid，删除按钮永远报"记录不存在"；
        ③ 写入口只应对实时库开放，快照库上能改就等于改了个假象。
        """
        path = Path(self.temp_dir.name) / "soft_delete.db"
        make_database(path)
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE chat_memory_messages SET deleted_at = '2026-08-12 00:00:00' "
            "WHERE msg_uid = 'p:b1'"
        )
        connection.commit()
        connection.close()
        repo = ChatMemoryRepository(
            path,
            timezone=timezone(timedelta(hours=8), name="Asia/Shanghai"),
            writable_path=path,
        )

        # 默认（缺省/visible）只看未删除；all 全看；deleted 只看已删除
        result = asyncio.run(repo.query({"page_size": 50}))
        self.assertEqual(result["total"], 3)
        self.assertNotIn("turn-b", [item["turn_id"] for item in result["items"]])

        result = asyncio.run(repo.query({"page_size": 50, "deleted_state": "visible"}))
        self.assertEqual(result["total"], 3)

        result = asyncio.run(repo.query({"page_size": 50, "deleted_state": "all"}))
        self.assertEqual(result["total"], 4)
        by_turn = {item["turn_id"]: item for item in result["items"]}
        self.assertTrue(by_turn["turn-b"]["deleted"])
        self.assertEqual(by_turn["turn-b"]["deleted_at"], "2026-08-12 08:00:00")
        self.assertFalse(by_turn["turn-a"]["deleted"])

        result = asyncio.run(repo.query({"page_size": 50, "deleted_state": "deleted"}))
        self.assertEqual([item["turn_id"] for item in result["items"]], ["turn-b"])
        with self.assertRaises(ValueError):
            asyncio.run(repo.query({"page_size": 50, "deleted_state": "unknown"}))

        # 概览/健康：总数只算可见记录，软删数量单独给出
        overview = asyncio.run(repo.overview())
        self.assertEqual(overview["summary"]["total_records"], 3)
        self.assertEqual(overview["summary"]["deleted_records"], 1)
        health = asyncio.run(repo.health())
        self.assertEqual(health["total_records"], 3)
        self.assertEqual(health["deleted_records"], 1)

        # 恢复（写入口）：记录重新进入默认列表；重复恢复 / 不存在的 ID → ValueError
        restored = asyncio.run(repo.restore_record(by_turn["turn-b"]["record_id"]))
        self.assertFalse(restored["deleted"])
        self.assertEqual(asyncio.run(repo.query({"page_size": 50}))["total"], 4)
        with self.assertRaises(ValueError):
            asyncio.run(repo.restore_record(by_turn["turn-b"]["record_id"]))
        with self.assertRaises(ValueError):
            asyncio.run(repo.restore_record(1_000_000_999))

        # 单条软删除：标记后立刻从默认列表消失，并可再恢复
        marked = asyncio.run(
            repo.soft_delete_record(by_turn["turn-a"]["record_id"])
        )
        self.assertTrue(marked["deleted"])
        self.assertTrue(marked["deleted_at"])
        self.assertEqual(asyncio.run(repo.query({"page_size": 50}))["total"], 3)
        self.assertEqual(
            [item["turn_id"] for item in asyncio.run(
                repo.query({"page_size": 50, "deleted_state": "deleted"})
            )["items"]],
            ["turn-a"],
        )
        with self.assertRaises(ValueError):
            asyncio.run(repo.soft_delete_record(by_turn["turn-a"]["record_id"]))
        asyncio.run(repo.restore_record(by_turn["turn-a"]["record_id"]))
        self.assertEqual(asyncio.run(repo.query({"page_size": 50}))["total"], 4)

        # 数据源不是插件实时库（未传 writable_path）→ 拒绝写入
        readonly = ChatMemoryRepository(
            path, timezone=timezone(timedelta(hours=8), name="Asia/Shanghai")
        )
        self.assertFalse(readonly.writable)
        with self.assertRaises(PermissionError):
            asyncio.run(readonly.restore_record(1_000_000_001))
        with self.assertRaises(PermissionError):
            asyncio.run(readonly.soft_delete_record(1_000_000_001))

        # 工具调用表同一套逻辑（id 无偏移）：三态筛选 + 单条软删除/恢复
        tools = asyncio.run(repo.tool_records({"page": 1, "page_size": 20}))
        self.assertEqual(len(tools["items"]), 3)
        self.assertFalse(any(item["deleted"] for item in tools["items"]))

        marked_tool = asyncio.run(
            repo.soft_delete_record(tools["items"][0]["tool_id"], "tool")
        )
        self.assertEqual(marked_tool["target"], "tool")
        self.assertTrue(marked_tool["deleted"])
        self.assertTrue(marked_tool["deleted_at"])  # 按配置时区返回本地时间
        visible_tools = asyncio.run(repo.tool_records({"page": 1, "page_size": 20}))
        self.assertEqual(len(visible_tools["items"]), 2)
        self.assertNotIn(
            marked_tool["id"],
            [item["tool_id"] for item in visible_tools["items"]],
        )
        deleted_tools = asyncio.run(repo.tool_records({
            "page": 1, "page_size": 20, "deleted_state": "deleted",
        }))
        self.assertEqual([item["tool_id"] for item in deleted_tools["items"]],
                         [marked_tool["id"]])
        self.assertTrue(deleted_tools["items"][0]["deleted"])
        all_tools = asyncio.run(repo.tool_records({
            "page": 1, "page_size": 20, "deleted_state": "all",
        }))
        self.assertEqual(len(all_tools["items"]), 3)
        # 概览：工具调用同样按可见口径 + 已删除计数
        overview = asyncio.run(repo.overview())
        self.assertEqual(overview["tools"]["total_calls"], 2)
        self.assertEqual(overview["tools"]["deleted_calls"], 1)
        # 被软删的是最新一条（turn-b 的 draw）：draw 计数从 2 降到 1，query 不受影响
        self.assertEqual(overview["tools"]["tool_names"]["draw"], 1)
        self.assertEqual(overview["tools"]["tool_names"]["query"], 1)
        restored_tool = asyncio.run(repo.restore_record(marked_tool["id"], "tool"))
        self.assertFalse(restored_tool["deleted"])
        self.assertEqual(
            len(asyncio.run(repo.tool_records({"page": 1, "page_size": 20}))["items"]),
            3,
        )
        with self.assertRaises(ValueError):
            asyncio.run(repo.soft_delete_record(1, "unknown"))

    def test_reply_view_turn_and_snapshot(self):
        """守的静默失效：引用块解析错会静默降级成「普通消息」。

        ① `resolution="turn"` 走目标轮次查询，查不到目标时昵称回落「未知成员」、
           正文变空——卡片长得和"这条没引用别人"一模一样，只有对照过才看得出引用丢了；
        ② `resolution="snapshot"` 直接取快照字段，取错字段会显示空引用原文；
        ③ 两条路径的 `target` / `text` 都必须断言到实际值（只查键存在等于没测）。
        """
        page = asyncio.run(self.repository.query({"page_size": 50}))
        # turn-a 有两条（user + assistant），按 role 区分
        turn_rows = [i for i in page["items"] if i["turn_id"] == "turn-a"]
        self.assertEqual(len(turn_rows), 2)
        turn_row = next(i for i in turn_rows if i["role"] == "user")

        self.assertEqual(turn_row["reply_view"], {
            "resolution": "turn",
            # turn-b 的 user 行是 p:b1（Carol），内容"只有图片"
            "target": "Carol",
            "text": "只有图片",
        })

        snapshot_row = next(i for i in page["items"] if i["turn_id"] == "turn-a"
                            and i["role"] == "assistant")
        self.assertEqual(snapshot_row["reply_view"], {
            "resolution": "snapshot",
            "target": "快照昵称",
            "text": "快照原文",
        })

        # 没有 reply 结构的记录不得凭空造出引用块
        proactive = next(i for i in page["items"] if i["turn_id"] == "turn-c")
        self.assertIsNone(proactive["reply_view"])

        # 详情必须与列表同一口径（否则列表有引用、点进去变没有）
        detail = asyncio.run(self.repository.record(turn_row["record_id"]))
        self.assertEqual(detail["record"]["reply_view"]["target"], "Carol")
        self.assertEqual(detail["record"]["reply_view"]["text"], "只有图片")

        # 目标轮次不存在 → 「未知成员」+ 空原文（这是"引用解析失败"的可见形态，
        # 不是静默空值；查询本身抛错的情况由 DatabaseUnavailableError 承担）
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE chat_memory_messages SET relation_data = ? WHERE msg_uid = 'p:a1'",
            (json.dumps({"v": 1, "reply": {"resolution": "turn",
                                           "target_turn_id": "turn-missing"}}),),
        )
        connection.commit()
        connection.close()
        repo = ChatMemoryRepository(
            self.db_path,
            timezone=timezone(timedelta(hours=8), name="Asia/Shanghai"),
        )
        try:
            orphan = asyncio.run(repo.query({"page_size": 50}))
            view = next(i for i in orphan["items"] if i["turn_id"] == "turn-a"
                        and i["role"] == "user")
            self.assertEqual(view["reply_view"]["target"], "未知成员")
            self.assertEqual(view["reply_view"]["text"], "")
        finally:
            repo.close()

    def test_media_view_and_archive(self):
        """守的静默失效：归档 join 错会让已归档媒体显示成「未归档」。

        后果是音视频的播放/下载入口整块消失、语音气泡被禁用，而卡片正文照旧，
        页面上看不出是"查询坏了"还是"本来就没存文件"。
        """
        page = asyncio.run(self.repository.query({"page_size": 50}))
        row = next(i for i in page["items"] if i["turn_id"] == "turn-b")
        view = {item["kind"]: item for item in row["media_view"]}

        # 已归档的图片：归档状态与元信息必须逐字段对上
        image = view["image"]
        self.assertTrue(image["archived"])
        self.assertEqual(image["media_id"], MEDIA_ID_IMAGE)
        self.assertEqual(image["mime"], "image/jpeg")
        self.assertEqual(image["size"], 12345)
        self.assertEqual(image["file"], "202608/cat.jpg")
        self.assertEqual(image["label"], "⟦图片⟧")

        # 未归档的 file 条目：保持 archived=False，原始文件名不能丢
        file_entry = view["file"]
        self.assertFalse(file_entry["archived"])
        self.assertEqual(file_entry["name"], "报告.pdf")
        self.assertNotIn("media_id", file_entry)

        # 媒体端点必须能按同一个 id 真取到归档行
        fetched = asyncio.run(self.repository.media_file(MEDIA_ID_IMAGE))
        self.assertEqual(fetched["media_id"], MEDIA_ID_IMAGE)
        self.assertEqual(fetched["file_name"], "202608/cat.jpg")
        self.assertEqual(fetched["size_bytes"], 12345)
        with self.assertRaises(ValueError):
            asyncio.run(self.repository.media_file("b2" * 16))



    def test_read_failures_are_not_disguised_as_empty(self):
        """守的静默失效：把 SQL 失败伪装成"没有数据"。

        每一条被吞掉都会让页面显示一个**看起来正常**的空结果，且与"真的没有数据"
        无法区分：昵称全是号码、高级注解整块消失、persona 下拉变空、已归档媒体显示
        未归档（播放/下载入口消失）、引用显示「未知成员」、工具调用显示无归属、
        工具表缺表显示零次调用。全部必须变成可诊断的不可用。
        """
        cases = [
            # (标签, 建表语句, 需要断言的错因关键词)
            (
                "昵称补全",
                "CREATE TABLE chat_memory_messages (sender_id TEXT, content TEXT);",
            ),
            (
                "高级注解",
                "CREATE TABLE chat_memory_message_bots ("
                " msg_uid TEXT, turn_id TEXT, llm_status TEXT, persona_id TEXT,"
                " role TEXT);",
            ),
        ]

        def broken(ddl):
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.executescript(ddl)
            return connection

        # 1) 昵称补全：sender_nickname 列不可用
        connection = broken(cases[0][1])
        try:
            with self.assertRaisesRegex(DatabaseUnavailableError, "昵称"):
                self.repository._fetch_sender_names(
                    connection, [{"sender_id": "10001", "sender_nickname": "10001"}],
                )
        finally:
            connection.close()

        # 2) 高级注解：send_status 列不可用
        connection = broken(cases[1][1])
        try:
            with self.assertRaisesRegex(DatabaseUnavailableError, "注解"):
                self.repository._advanced_notes(
                    connection, [{"msg_uid": "p:a1"}], "persona-a",
                )
        finally:
            connection.close()

        # 3) persona 候选：先过可用性校验，再让内层查询失败
        connection = broken(
            "CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO chat_memory_shadow_meta VALUES ('schema_version', '6');"
            "CREATE TABLE chat_memory_messages (msg_uid TEXT);"
            "CREATE TABLE chat_memory_message_bots (msg_uid TEXT, umo TEXT);"
        )
        try:
            with self.assertRaisesRegex(DatabaseUnavailableError, "persona"):
                self.repository._persona_facets_query(connection)
        finally:
            connection.close()

        # 4) 媒体归档：归档表缺列
        connection = broken("CREATE TABLE chat_memory_media_archive (media_id TEXT);")
        try:
            relation = json.dumps({"v": 1, "media": [
                {"kind": "image", "id": MEDIA_ID_IMAGE},
            ]})
            with self.assertRaisesRegex(DatabaseUnavailableError, "归档"):
                self.repository._fetch_media_archive(
                    connection, [{"relation_data": relation}],
                )
        finally:
            connection.close()

        # 5) 引用目标：messages.content 不可用
        connection = broken(
            "CREATE TABLE chat_memory_message_bots ("
            " msg_uid TEXT, turn_id TEXT, role TEXT, umo TEXT);"
            "CREATE TABLE chat_memory_messages (msg_uid TEXT, content_kind TEXT,"
            " relation_data TEXT, sender_nickname TEXT, created_at TEXT);"
        )
        try:
            relation = json.dumps({"v": 1, "reply": {
                "resolution": "turn", "target_turn_id": "t1",
            }})
            with self.assertRaisesRegex(DatabaseUnavailableError, "引用"):
                self.repository._fetch_reply_targets(
                    connection,
                    [{"umo": "bot:GroupMessage:g1", "relation_data": relation}],
                )
        finally:
            connection.close()

        # 6) 工具归属：message_bots.self_id 不可用
        connection = broken(
            "CREATE TABLE chat_memory_message_bots ("
            " umo TEXT, turn_id TEXT, persona_id TEXT);"
        )
        try:
            with self.assertRaisesRegex(DatabaseUnavailableError, "归属"):
                self.repository._fetch_tool_callers(
                    connection,
                    [{"umo": "bot:FriendMessage:u1", "turn_id": "turn-a"}],
                )
        finally:
            connection.close()

        # 7) 工具表整体缺失：契约里它是必需的，不能被报成"零次调用"
        path = Path(self.temp_dir.name) / "no_tools.db"
        make_database(path, include_tools=False)
        repo = ChatMemoryRepository(
            path,
            writable_path=path,
            timezone=timezone(timedelta(hours=8), name="Asia/Shanghai"),
        )
        try:
            with self.assertRaisesRegex(DatabaseUnavailableError, "工具调用表"):
                asyncio.run(repo.tool_records({"page": 1, "page_size": 20}))
        finally:
            repo.close()

    def test_corrupt_database_fallback(self):
        # 小节：WAL 损坏时 immutable 回退并报告 main_snapshot
        original_connect = self.repository._connect

        def fake_connect(*, immutable: bool):
            if not immutable:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return original_connect(immutable=True)

        with patch.object(self.repository, "_connect", side_effect=fake_connect):
            result = asyncio.run(self.repository.overview())
        self.assertEqual(result["database"]["mode"], "main_snapshot")
        self.assertIn("WAL/SHM", result["database"]["warning"])

        # 小节：禁用回退后直接失败
        repository = ChatMemoryRepository(
            self.db_path, allow_immutable_fallback=False
        )

        def fail_connect(*, immutable: bool):
            raise sqlite3.DatabaseError("database disk image is malformed")

        with patch.object(repository, "_connect", side_effect=fail_connect):
            with self.assertRaisesRegex(DatabaseUnavailableError, "不可读"):
                asyncio.run(repository.health())

