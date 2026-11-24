"""查询台读合并存储的用例：一条消息只出现一次。

守的静默失效：三个 BOT 看同一个群时若按实例各存一行去读，页面会把同一条消息显示
三次；去重键写错、或代表实例选错，用户看到的就是重复内容（页面上"看得见"，但只有
对照过才知道是重复——属于数据呈现错误，值得一条用例）。
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

SHADOW_DDL = """
CREATE TABLE chat_memory_shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chat_memory_messages (
    msg_uid TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, seq INTEGER NOT NULL,
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

# 双写起点只作元信息保留（查询台不再按它决定读哪张表）。
CUT = "2025-01-01 00:00:00.000000"
AFTER = "2025-01-05 09:00:00.000000"


def _build(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SHADOW_DDL)
    con.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) VALUES ('schema_version', '6')"
    )
    con.execute(
        "INSERT INTO chat_memory_shadow_meta (key, value) "
        "VALUES ('dual_write_started_at', ?)",
        (CUT,),
    )
    # 一条物理消息被三个实例看到：messages 只有一行，参与行三条（不同 umo / 会话 id）
    con.execute(
        "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, "
        "platform_name, message_type, session_id, sender_id, sender_nickname, "
        "platform_timestamp, content, content_kind, platform_seq, created_at) "
        "VALUES ('p:111', 'fp', 1, 'aiocqhttp', 'GroupMessage', 'g1', "
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
            self.db_path, timezone=ZoneInfo("Asia/Shanghai")
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temp_dir.cleanup()

    def test_one_row_per_physical_message(self) -> None:
        """一条物理消息只出现一行，umo 归到代表实例。"""
        page = asyncio.run(self.repository.query({"page_size": 20}))
        self.assertEqual(page["total"], 1, page["total"])
        contents = [item["content"] for item in page["items"]]
        self.assertEqual(contents, ["合并存储里的同一句话"])
        merged = page["items"][0]
        self.assertGreaterEqual(merged["record_id"], _MERGED_ID_OFFSET)
        # 三个实例看到的同一条消息：umo 归到代表实例
        self.assertEqual(merged["umo"], "BOT1:GroupMessage:g1")


    def test_locally_keyed_events_stay_visible(self) -> None:
        """守的静默失效：`l:` 是"平台没有给 id 的真实消息/事件"的本地键（表情回应、
        戳一戳、群名片变更…），不是发送前的计划全文。视图若把它过滤掉，事件会整类消失。
        """
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO chat_memory_messages (msg_uid, fingerprint, seq, "
            "platform_name, message_type, session_id, sender_id, sender_nickname, "
            "platform_timestamp, content, content_kind, created_at) "
            "VALUES ('l:eventfingerprint:0', 'fp2', 9, 'aiocqhttp', "
            "'GroupMessage', 'g1', '10001', '阿二', 1760000000000, '[表情回应]', "
            "'[\"event\"]', ?)",
            (AFTER,),
        )
        con.commit()
        con.close()

        page = asyncio.run(self.repository.query({"page_size": 50}))
        contents = [item["content"] for item in page["items"]]
        self.assertIn("[表情回应]", contents, "本地键（l:）的事件消息必须出现在列表里")

    def test_keep_open_soft_delete_is_visible(self) -> None:
        """守的静默失效：长连接（keep_open）上的物化缓存不失效。

        生产里查询台是一条复用的只读连接 + 一张 TEMP 物化副本，并带 2 秒重建节流
        （活跃群聊里几乎每条消息都在变，不节流每次查询要付 565ms）。写路径若只清
        `_merged_key` 而不清 `_merged_ready`，`present` 仍为真、`since_build < 2s`
        会直接命中旧缓存——删完再查还是旧行、点进详情 `deleted` 仍是 False，
        页面看起来像"删除没生效"。非 keep_open 的连接每请求重建，永远测不到这条。
        """
        import time

        temp_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(temp_dir.name) / "keep_open.db"
            _build(path)
            repo = ChatMemoryRepository(
                path,
                writable_path=path,
                timezone=ZoneInfo("Asia/Shanghai"),
                keep_open=True,
            )
            try:
                # 走一次查询把物化副本建起来（生产里由预热/首次查询完成）
                before = asyncio.run(repo.query({"page_size": 20}))
                self.assertEqual(before["total"], 1)
                record_id = before["items"][0]["record_id"]

                asyncio.run(repo.soft_delete_record(record_id, "record"))

                # 前提校验：必须仍在重建节流窗口内，否则测的是另一条路径
                age = time.time() - repo._merged_built_at
                self.assertLess(
                    age, 2.0,
                    f"用例前提被破坏：删除时距上次重建已 {age:.2f}s，超出节流窗口",
                )

                after = asyncio.run(repo.query({"page_size": 20}))
                self.assertEqual(after["total"], 0, "软删后仍返回了旧缓存行")
                detail = asyncio.run(repo.record(record_id))
                self.assertTrue(detail["record"]["deleted"], "详情仍显示未删除")

                asyncio.run(repo.restore_record(record_id, "record"))
                restored = asyncio.run(repo.query({"page_size": 20}))
                self.assertEqual(restored["total"], 1, "恢复后仍看不到记录")
                self.assertFalse(
                    asyncio.run(repo.record(record_id))["record"]["deleted"]
                )
            finally:
                repo.close()
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
