"""chat_memory — 轮次成员表（新存储层的辅助表）。

**表的分工**（message 表的理念：每一行都是**群里真实看得见的那条消息**）

- ``chat_memory_messages`` / ``chat_memory_message_bots``：平台事实与每实例参与行。
- ``chat_memory_reply_messages``：**本轮计划内容**（发送前抓到的正文与轮次状态），
  一轮一行（主键 ``turn_id``：发送侧生成的 uuid4，全局唯一）。
  组键 ``rg:<turn_id>`` 现推，不再单独落列（结构线 6 删掉 ``messages.reply_group``）。
- 「哪条消息属于哪一轮」不存在单独的表：就是 ``chat_memory_message_bots`` 上
  assistant 行的 ``turn_id``（一条消息只有一个 assistant 行，天然唯一）。
- ``chat_memory_tool_calls``：工具调用（接管时回放 tool 段），幂等键
  ``(turn_id, call_index)``。
- ``chat_memory_shadow_meta``：``schema_version`` / ``dual_write_started_at`` /
  ``shadow_migrate``（键白名单由 CHECK 约束，拼错的键会被库直接拒绝）。

撤回补标不建表：撤回通知本身就是一条真实事件行（``relation_data.event`` 里带被撤回的 mid），
"待补标"由"有通知、目标行还没标上"派生。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_TABLES = (
    """CREATE TABLE IF NOT EXISTS chat_memory_reply_messages (
    turn_id         TEXT PRIMARY KEY,               -- turn_id 是发送侧生成的 uuid4，全局唯一
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    persona_id      TEXT NOT NULL DEFAULT '',
    created_at      DATETIME NOT NULL,
    content         TEXT NOT NULL DEFAULT '',   -- 发送前抓到的计划正文
    content_kind    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(content_kind)),
    llm_status      TEXT NOT NULL DEFAULT '',
    send_status     TEXT NOT NULL DEFAULT ''
                    CHECK (send_status IN ('', 'prepared', 'send_attempted')),
    deleted_at      DATETIME
)""",
)

_INDEXES = (
    # 按会话查计划行的索引。**唯一性**由主键 ``(umo, turn_id)`` 保证（一轮一条计划）；
    # 这张表没有 msg_uid 列，也不再有任何"一条消息只属于一轮"的约束——那条不变量由
    # ``chat_memory_message_bots`` 的 assistant 行 turn_id 表达（应用层保证，先到者赢）。
    "CREATE INDEX IF NOT EXISTS ix_cmrm_cid "
    "ON chat_memory_reply_messages (umo, conversation_id)",
)


def diag_schema_statements() -> list[str]:
    """建表在前、建索引在后（与影子表同一约定）。"""
    return [*_TABLES, *_INDEXES]


def diag_drop_statements() -> list[str]:
    """重建时删掉本层自建的表（含已废弃的诊断表残骸）。"""
    return [
        "DROP TABLE IF EXISTS chat_memory_reply_messages",
        "DROP TABLE IF EXISTS chat_memory_shadow_stats",
        "DROP TABLE IF EXISTS chat_memory_shadow_events",
        "DROP TABLE IF EXISTS chat_memory_shadow_anomalies",
        "DROP TABLE IF EXISTS chat_memory_echo_channels",
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def note_reply_plan(
    session: AsyncSession,
    *,
    participation: dict[str, Any],
    content: str,
    content_kind: str = "[]",
    llm_status: str = "",
    send_status: str = "",
    persona_id: str = "",
) -> None:
    """写"这一轮的计划内容"（发送前抓到的正文 + 轮次状态），一轮一行。

    只补空字段：同一轮先到的计划正文赢，后到的只填空白项。
    主键是 ``turn_id``（全局唯一），不再带 ``umo``——所以不同会话不会混淆 id，
    按会话取计划走 ``ix_cmrm_cid (umo, conversation_id)``。
    """
    turn_id = str(participation.get("turn_id") or "")
    umo = str(participation.get("umo") or "")
    cid = str(participation.get("conversation_id") or "")
    if not (turn_id and umo):
        return
    await session.execute(
        text(
            "INSERT INTO chat_memory_reply_messages "
            "(turn_id, umo, conversation_id, persona_id, created_at, "
            " content, content_kind, llm_status, send_status) "
            "VALUES (:turn_id, :umo, :cid, :persona_id, :now, "
            " :content, :content_kind, :llm_status, :send_status) "
            "ON CONFLICT (turn_id) DO UPDATE SET "
            "  content = CASE WHEN chat_memory_reply_messages.content = '' "
            "                 THEN excluded.content ELSE chat_memory_reply_messages.content END, "
            "  content_kind = CASE WHEN chat_memory_reply_messages.content_kind IN ('', '[]') "
            "                 THEN excluded.content_kind "
            "                 ELSE chat_memory_reply_messages.content_kind END, "
            "  persona_id = CASE WHEN chat_memory_reply_messages.persona_id = '' "
            "                 THEN excluded.persona_id "
            "                 ELSE chat_memory_reply_messages.persona_id END, "
            "  llm_status = CASE WHEN chat_memory_reply_messages.llm_status = '' "
            "                 THEN excluded.llm_status "
            "                 ELSE chat_memory_reply_messages.llm_status END, "
            "  send_status = CASE WHEN excluded.send_status = '' "
            "                 THEN chat_memory_reply_messages.send_status "
            "                 ELSE excluded.send_status END"
        ),
        {
            "turn_id": turn_id,
            "umo": umo,
            "cid": cid,
            "persona_id": str(persona_id or ""),
            "now": _now(),
            "content": str(content or ""),
            "content_kind": str(content_kind or "[]"),
            "llm_status": str(llm_status or ""),
            "send_status": str(send_status or ""),
        },
    )


async def link_owner_turn(session: AsyncSession, *, umo: str, msg_uid: str) -> str:
    """这条物理消息当前登记在哪一轮（没有则空串）。

    认领路径用它判断"是不是被别的轮次先认领了"：一条消息只能属于一轮，先到者赢。
    查询范围与参与行的唯一键 ``(msg_uid, platform_id, self_id, conversation_id)``
    **一致**（这里按 ``umo`` 圈会话、``role='assistant'`` 圈发送方）——两者不同域时，
    后写的一方会静默丢掉前者的归属，读侧就少一段。
    """
    if not (umo and msg_uid):
        return ""
    row = (
        await session.execute(
            text(
                # 归属就在参与行上：一条消息只会有一个 assistant 行（谁发的），
                # 它的 turn_id 就是这条消息所属的轮次。
                "SELECT turn_id FROM chat_memory_message_bots "
                "WHERE umo = :umo AND msg_uid = :msg_uid AND role = 'assistant' "
                "AND COALESCE(turn_id, '') <> '' LIMIT 1"
            ),
            {"umo": str(umo), "msg_uid": str(msg_uid)},
        )
    ).first()
    return str(row[0]) if row is not None else ""
