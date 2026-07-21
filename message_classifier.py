"""ChatMemory 消息组件分类。

该模块只负责 AstrBot MessageChain → ChatMemory 字段的转换，不参与数据库、会话或
上下文接管，便于在没有完整 AstrBot 运行时的情况下单独测试。
"""

from typing import Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    Plain,
    Image,
    Video,
    Record,
    File,
    Face,
    At,
    AtAll,
    Reply,
    Forward,
)
from .models import (
    K_TEXT,
    K_IMAGE,
    K_VIDEO,
    K_VOICE,
    K_FILE,
    K_FACE,
    K_FORWARD,
    K_SYSTEM,
)
from .relation_codec import (
    RELATION_VERSION,
    at_token,
    escape_plain_text,
    truncate_reply_snapshot,
)


def extract_text(event: AstrMessageEvent) -> str:
    try:
        chain = event.get_messages() or []
    except Exception:
        chain = []
    if chain:
        parts = [comp.text for comp in chain if isinstance(comp, Plain)]
        text = "".join(parts).strip()
        if text:
            return text
    return getattr(event, "message_str", "") or ""


def extract_user_template(event: AstrMessageEvent) -> tuple[str, list[dict], Optional[dict]]:
    """按 MessageChain 顺序构建正文模板、At 参数和 Reply 快照种子。"""
    try:
        chain = event.get_messages() or []
    except Exception:
        chain = []
    if not chain:
        return escape_plain_text(getattr(event, "message_str", "") or ""), [], None

    parts: list[str] = []
    mentions: list[dict] = []
    reply_seed: Optional[dict] = None
    for comp in chain:
        if isinstance(comp, Plain):
            parts.append(escape_plain_text(comp.text or ""))
        elif isinstance(comp, AtAll):
            index = len(mentions)
            mentions.append({"all": True})
            parts.append(at_token(index))
        elif isinstance(comp, At):
            user_id = str(getattr(comp, "qq", None) or "").strip()
            nickname = str(getattr(comp, "name", None) or "").strip()
            index = len(mentions)
            mentions.append({"user_id": user_id, "nickname": nickname})
            parts.append(at_token(index))
        elif isinstance(comp, Reply) and reply_seed is None:
            sender_id = str(
                getattr(comp, "sender_id", None)
                or getattr(comp, "qq", None)
                or ""
            ).strip()
            snapshot_text = str(
                getattr(comp, "message_str", None)
                or getattr(comp, "text", None)
                or ""
            )
            reply_seed = {
                "source_id": str(getattr(comp, "id", None) or "").strip(),
                "target_user_id": sender_id,
                "target_nickname": str(
                    getattr(comp, "sender_nickname", None) or ""
                ).strip(),
                "fallback_text": truncate_reply_snapshot(snapshot_text),
            }
    return "".join(parts).strip(), mentions, reply_seed


def build_relation_seed(event: AstrMessageEvent) -> tuple[str, Optional[dict]]:
    template, mentions, reply_seed = extract_user_template(event)
    if not mentions and not reply_seed:
        return template, None
    return template, {
        "v": RELATION_VERSION,
        "mentions": mentions,
        "reply": reply_seed,
    }


def classify_content(
    event: AstrMessageEvent,
) -> tuple[list[str], Optional[str], Optional[str], Optional[str]]:
    """返回 ``(content_kind, at_id, reply_id, forward_id)``。"""
    try:
        chain = event.get_messages() or []
    except Exception:
        chain = []
    kind: list[str] = []
    at_id: Optional[str] = None
    reply_id: Optional[str] = None
    forward_id: Optional[str] = None

    def push(value: str) -> None:
        if value not in kind:
            kind.append(value)

    for comp in chain:
        if isinstance(comp, Plain):
            if (comp.text or "").strip():
                push(K_TEXT)
        elif isinstance(comp, Image):
            push(K_IMAGE)
        elif isinstance(comp, Video):
            push(K_VIDEO)
        elif isinstance(comp, Record):
            push(K_VOICE)
        elif isinstance(comp, File):
            push(K_FILE)
        elif isinstance(comp, Face):
            push(K_FACE)
        elif isinstance(comp, Forward):
            push(K_FORWARD)
            if forward_id is None:
                value = getattr(comp, "id", None)
                if value:
                    forward_id = str(value)
        elif isinstance(comp, AtAll):
            pass
        elif isinstance(comp, At):
            if at_id is None:
                value = getattr(comp, "qq", None)
                if value:
                    at_id = str(value)
        elif isinstance(comp, Reply):
            if reply_id is None:
                value = getattr(comp, "id", None)
                if value:
                    reply_id = str(value)

    if not kind:
        message_str = (getattr(event, "message_str", "") or "").strip()
        if message_str:
            kind.append(K_TEXT)

    try:
        message_type = event.get_message_type()
        message_type_value = getattr(message_type, "value", str(message_type))
        if message_type_value == "OtherMessage":
            kind = [K_SYSTEM]
    except Exception:
        pass

    return kind, at_id, reply_id, forward_id


def classify_assistant_chain(chain) -> tuple[list[str], str]:
    """从 BOT 回复组件链提取 ``(content_kind, text)``。"""
    kind: list[str] = []
    parts: list[str] = []

    def push(value: str) -> None:
        if value not in kind:
            kind.append(value)

    for comp in chain:
        if isinstance(comp, Plain):
            if (comp.text or "").strip():
                push(K_TEXT)
                parts.append(comp.text)
        elif isinstance(comp, Image):
            push(K_IMAGE)
        elif isinstance(comp, Video):
            push(K_VIDEO)
        elif isinstance(comp, Record):
            push(K_VOICE)
        elif isinstance(comp, File):
            push(K_FILE)
        elif isinstance(comp, Face):
            push(K_FACE)
        elif isinstance(comp, Forward):
            push(K_FORWARD)
    return kind, "".join(parts).strip()


def content_placeholder(kind: list[str]) -> str:
    if not kind:
        return ""
    return f"[{kind[0]}]"
