"""查询台读合并存储（schema 2）的用例：一条消息只出现一次。

守的静默失效：三个 BOT 看同一个群时旧表按 (umo, cid) 各存一行，页面把同一条消息
显示三次；切到合并存储后如果仍按旧表读、或者去重键写错，用户看到的就是重复内容
（页面上"看得见"，但只有对照过才知道是重复——属于数据呈现错误，值得一条用例）。
"""
import asyncio
import sqlite3
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from chat_memory.ui.repository import (  # noqa: E402
    ChatMemoryRepository,
    _MERGED_ID_OFFSET,
)

LEGACY_DDL = """
CREATE TABLE chat_memory_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user', content TEXT NOT NULL DEFAULT '',
    message_id TEXT, pair_id TEXT, llm_status TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '[]', platform_id TEXT, platform_name TEXT,
    message_type TEXT, session_id TEXT, self_id TEXT, group_id TEXT,
    sender_nickname TEXT, raw_timestamp INTEGER, at_id TEXT, reply_id TEXT,
    forward_id TEXT, created_at TIMESTAMP NOT NULL, persona_id TEXT, turn_id TEXT,
    send_status TEXT, relation_data TEXT, recalled_at TEXT, deleted_at TEXT)
"""

SHADOW_DDL = """
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chat_memory_messages (
    msg_uid TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, seq INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'inbound', reply_group TEXT,
    platform_name TEXT NOT NULL, message_type TEXT NOT NULL, session_id TEXT NOT NULL,
    sender_id TEXT NOT NULL, sender_nickname TEXT NOT NULL DEFAULT '',
    platform_timestamp INTEGER NOT NULL, content TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '[]', relation_data TEXT, recalled_at DATETIME,
    deleted_at DATETIME, platform_seq INTEGER, created_at DATETIME NOT NULL);
CREATE TABLE chat_memory_message_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_uid TEXT NOT NULL,
    platform_id TEXT NOT NULL, self_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL, umo TEXT NOT NULL, role TEXT NOT NULL,
    platform_message_id TEXT, turn_id TEXT, llm_status TEXT NOT NULL DEFAULT '',
    send_status TEXT NOT NULL DEFAULT '', persona_id TEXT, target_id TEXT,
    raw_timestamp INTEGER, created_at DATETIME NOT NULL,
    UNIQUE (msg_uid, platform_id, self_id, conversation_id));
"""

# 双写起点必须早于"现在 - SHADOW_READ_MIN_DAYS"：查询台与上下文共用同一道门槛，
# 起点写成最近的话读源会判成"还没到日子"（仍读旧表），这些合并存储用例就都不成立。
CUT = "2025-01-01 00:00:00.000000"
BEFORE = "2024-12-25 08:00:00.000000"
AFTER = "2025-01-05 09:00:00.000000"


def _build(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(LEGACY_DDL)
    con.executescript(SHADOW_DDL)
    con.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) VALUES ('schema_version', '2')"
    )
    con.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) "
        "VALUES ('dual_write_started_at', ?)",
        (CUT,),
    )
    # 双写之前的旧历史：同一条群消息被两个实例各存一份（不同 umo / 不同会话 id）
    for umo, cid in (
        ("BOT1:GroupMessage:g1", "cid-1"),
        ("BOT2:GroupMessage:g1", "cid-2"),
    ):
        con.execute(
            "INSERT INTO chat_memory_records (umo, conversation_id, user_id, role, content, "
            "content_kind, platform_name, message_type, session_id, raw_timestamp, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (umo, cid, "10001", "user", "旧历史里的同一句话", '["text"]', "aiocqhttp",
             "GroupMessage", "g1", 1760000000000, BEFORE),
        )
    # 双写之后：一条消息被三个实例看到（物理消息只有一行）
    con.execute(
        "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, origin, "
        "platform_name, message_type, session_id, sender_id, sender_nickname, "
        "platform_timestamp, content, content_kind, platform_seq, created_at) "
        "VALUES ('p:111', 'fp', 1, 'inbound', 'aiocqhttp', 'GroupMessage', 'g1', "
        "'10001', '阿二', 1760000000000, '合并存储里的同一句话', '[\"text\"]', 111, ?)",
        (AFTER,),
    )
    for platform_id in ("BOT1", "BOT2", "BOT3"):
        con.execute(
            "INSERT INTO chat_memory_message_bots (msg_uid, platform_id, self_id, "
            "conversation_id, umo, role, platform_message_id, raw_timestamp, created_at) "
            "VALUES ('p:111', ?, '99999', ?, ?, 'user', 'mid-111', ?, ?)",
            (platform_id, f"cid-{platform_id}", f"{platform_id}:GroupMessage:g1",
             1760000000000, AFTER),
        )
    con.commit()
    con.close()


class MergedSourceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat_memory.db"
        _build(self.db_path)
        self.repository = ChatMemoryRepository(
            self.db_path, timezone=ZoneInfo("Asia/Shanghai"), shadow_read_enabled=True
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def test_one_row_per_physical_message_and_legacy_dedup(self) -> None:
        page = asyncio.run(self.repository.query({"page_size": 20}))
        # 旧历史两条实例副本 → 一条；合并存储一条 → 一条
        self.assertEqual(page["total"], 2, page["total"])
        contents = [item["content"] for item in page["items"]]
        self.assertEqual(contents, ["合并存储里的同一句话", "旧历史里的同一句话"])
        merged, legacy = page["items"]
        self.assertGreaterEqual(merged["record_id"], _MERGED_ID_OFFSET)
        self.assertLess(legacy["record_id"], _MERGED_ID_OFFSET)
        # 三个实例看到的同一条消息：umo 归到代表实例，但列表里只有一个 umo 值可显示
        self.assertEqual(merged["umo"], "BOT1:GroupMessage:g1")

    def test_chat_type_and_sender_filters(self) -> None:
        # 聊天类型：群消息两条（合并存储那条 + 去重后的旧历史那条）
        group = asyncio.run(self.repository.query({"chat_type": "group"}))
        self.assertEqual(group["total"], 2, group["total"])
        private = asyncio.run(self.repository.query({"chat_type": "private"}))
        self.assertEqual(private["total"], 0, private["total"])

        # QQ 号 = 会话号（群号）：命中这个群的两条
        by_session = asyncio.run(self.repository.query({"session_id": "g1"}))
        self.assertEqual(by_session["total"], 2, by_session["total"])

        # 发言者按 QQ 号精确匹配（旧历史里 user_id 顶上）
        by_id = asyncio.run(self.repository.query({"sender": "10001"}))
        self.assertEqual(
            {item["content"] for item in by_id["items"]},
            {"合并存储里的同一句话", "旧历史里的同一句话"},
        )

    def test_record_detail_and_soft_delete_on_merged_rows(self) -> None:
        page = asyncio.run(self.repository.query({"page_size": 20}))
        merged_id = page["items"][0]["record_id"]
        detail = asyncio.run(self.repository.record(merged_id))
        self.assertEqual(detail["record"]["content"], "合并存储里的同一句话")

        # 读源是合并存储时，删除必须落到 chat_memory_messages（写旧表会改到别的行）
        writable = ChatMemoryRepository(
            self.db_path,
            writable_path=self.db_path,
            timezone=ZoneInfo("Asia/Shanghai"),
            shadow_read_enabled=True,
        )
        try:
            asyncio.run(writable.soft_delete_record(merged_id, "record"))
            after = asyncio.run(writable.query({"page_size": 20}))
            self.assertEqual(after["total"], 1, after["total"])
            con = sqlite3.connect(self.db_path)
            marked = con.execute(
                "SELECT deleted_at FROM chat_memory_messages WHERE msg_uid = 'p:111'"
            ).fetchone()[0]
            legacy_untouched = con.execute(
                "SELECT COUNT(*) FROM chat_memory_records WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            con.close()
            self.assertTrue(marked)
            self.assertEqual(legacy_untouched, 0, "旧表那两行不该被这次删除碰到")
        finally:
            writable.close()

    def test_window_not_full_reads_legacy_only(self) -> None:
        """守的静默失效：查询台自己按"有没有 6 表"决定读源时，会与上下文/API 的
        7 天门槛脱钩——面板切了、记忆没切（或反过来）。三者必须同一个判据。
        """
        path = Path(self.temp_dir.name) / "fresh.db"
        _build(path)
        con = sqlite3.connect(path)
        con.execute(
            "UPDATE chat_memory_shadow_meta SET value = ? WHERE key = 'dual_write_started_at'",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
        con.commit()
        con.close()

        repo = ChatMemoryRepository(path, shadow_read_enabled=True)
        page = asyncio.run(repo.query({"page_size": 50}))
        # 影子里的那条消息不该出现：窗口没满 → 只读旧表
        merged = [item for item in page["items"] if str(item["record_id"]).startswith("1000")]
        self.assertEqual(merged, [], "窗口没满时查询台不得读合并存储")
        self.assertTrue(page["total"] > 0, "旧表照旧可读")

    def test_locally_keyed_events_stay_visible(self) -> None:
        """守的静默失效：`l:` 是"平台没有给 id 的真实消息/事件"的本地键（表情回应、
        戳一戳、群名片变更…），不是发送前的计划全文。视图若把它过滤掉，事件会整类消失。
        """
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, origin, "
            "platform_name, message_type, session_id, sender_id, sender_nickname, "
            "platform_timestamp, content, content_kind, created_at) "
            "VALUES ('l:eventfingerprint:0', 'fp2', 9, 'inbound', 'aiocqhttp', "
            "'GroupMessage', 'g1', '10001', '阿二', 1760000000000, '[表情回应]', "
            "'[\"event\"]', ?)",
            (AFTER,),
        )
        con.commit()
        con.close()

        page = asyncio.run(self.repository.query({"page_size": 50}))
        contents = [item["content"] for item in page["items"]]
        self.assertIn("[表情回应]", contents, "本地键（l:）的事件消息必须出现在列表里")

    def test_nickname_stored_as_number_is_filled(self) -> None:
        """守的静默失效：sender_nickname 被写成号码本身时，同一 QQ 会时而显示名字、
        时而显示一串数字；同页要用该发送者出现过的名字补上。
        """
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, origin, "
            "platform_name, message_type, session_id, sender_id, sender_nickname, "
            "platform_timestamp, content, content_kind, created_at) "
            "VALUES ('p:222', 'fp2', 2, 'inbound', 'aiocqhttp', 'GroupMessage', 'g1', "
            "'10001', '10001', 1760000000000, '昵称被存成号码的那条', '[\"text\"]', ?)",
            (AFTER,),
        )
        con.commit()
        con.close()

        page = asyncio.run(self.repository.query({"page_size": 50}))
        target = [i for i in page["items"] if i["content"] == "昵称被存成号码的那条"]
        self.assertTrue(target, "新插入的消息应当出现")
        self.assertEqual(target[0]["sender_nickname"], "阿二", "应当补成同类消息里的名字")

    def test_turn_role_index_is_declared(self) -> None:
        """守的静默失效：`ix_cmmb_turn_role` 被删掉后功能完全正常，只是「仅看配对」从
        0.8 秒退化到 195 秒（81k × 30k 的相关子查询）——不看耗时根本发现不了。
        """
        from chat_memory import shared_store as store

        # 真正执行的是 shadow_schema_statements()（建表 + 建索引），校验这一份
        ddl = " ".join(store.shadow_schema_statements())
        normalized = " ".join(ddl.split())
        self.assertIn(
            "ix_cmmb_turn_role ON chat_memory_message_bots (turn_id, role)",
            normalized,
            "配对筛选依赖 (turn_id, role) 索引，缺了会退化成分钟级",
        )

    def test_keep_open_soft_delete_is_visible(self) -> None:
        """守的静默失效：keep_open 连接把旧历史去重结果缓存在 TEMP 表里（含 deleted_at），
        软删除只改库、页面永远读到旧快照 —— 表现是"删了还在、再点报已被删除"。

        夹具里旧历史是**两个实例各一行、折叠成一组**，所以要整组删掉才等于"这条消息没了"。
        """
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE chat_memory_shadow_meta SET value = '2' "
                    "WHERE key = 'schema_version'")
        legacy_ids = [
            int(row[0]) for row in con.execute(
                "SELECT id FROM chat_memory_records ORDER BY id"
            ).fetchall()
        ]
        con.commit()
        con.close()

        repo = ChatMemoryRepository(
            self.db_path,
            timezone=ZoneInfo("Asia/Shanghai"),
            keep_open=True,
            expected_shadow_version="2",
            shadow_read_enabled=True,
            writable_path=self.db_path,
        )
        try:
            before = asyncio.run(repo.query({"page_size": 20}))
            assert before["total"] == 2, before["total"]
            for legacy_id in legacy_ids:
                asyncio.run(repo.soft_delete_record(legacy_id))
            after = asyncio.run(repo.query({"page_size": 20}))
            assert after["total"] == 1, f"删完旧历史仍看到 {after['total']} 条（缓存没重建）"
            contents = [item.get("content") for item in after["items"]]
            assert all("旧历史" not in str(text) for text in contents), contents
        finally:
            repo.close()

    def test_without_shadow_reads_legacy_only(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM chat_memory_shadow_meta WHERE key = 'schema_version'")
        con.execute(
            "INSERT INTO chat_memory_shadow_meta (key, value) VALUES ('schema_version', '1')"
        )
        con.commit()
        con.close()
        # 期望版本＝写入侧当前版本；库里是 '1'（不匹配）→ 必须退回旧表。
        # 这条守的是"结构线升级后查询台悄悄读旧表/或反过来读结构不符的新表"这类静默失效。
        repo = ChatMemoryRepository(
            self.db_path,
            timezone=ZoneInfo("Asia/Shanghai"),
            expected_shadow_version="2",
            shadow_read_enabled=True,
        )
        try:
            page = asyncio.run(repo.query({"page_size": 20}))
            # 退回旧表语义：两个实例的两行都在（不做合并去重）
            self.assertEqual(page["total"], 2, page["total"])
            self.assertTrue(all(i["record_id"] < _MERGED_ID_OFFSET for i in page["items"]))
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
