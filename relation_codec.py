"""ChatMemory 消息关系模板的编解码工具。"""

import json
import re
from typing import Any, Optional


RELATION_VERSION = 1
AT_TOKEN_RE = re.compile(r"⟦CM_AT:(\d+)⟧")
AT_TOKEN_LITERAL = "⟦CM_AT:"
AT_TOKEN_ESCAPED = "⟦CM_LITERAL_AT:"
MAX_REPLY_SNAPSHOT_CHARS = 300


def escape_plain_text(text: str) -> str:
    """避免用户原文伪造 ChatMemory 内部 At placeholder。"""
    return (text or "").replace(AT_TOKEN_LITERAL, AT_TOKEN_ESCAPED)


def at_token(index: int) -> str:
    return f"⟦CM_AT:{index}⟧"


def parse_relation_data(value: Any) -> Optional[dict]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        data = value
    else:
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict) or data.get("v") != RELATION_VERSION:
        return None
    mentions = data.get("mentions")
    if not isinstance(mentions, list):
        data["mentions"] = []
    if data.get("reply") is not None and not isinstance(data.get("reply"), dict):
        data["reply"] = None
    return data


def dump_relation_data(data: Optional[dict]) -> Optional[str]:
    if not data:
        return None
    mentions = data.get("mentions") or []
    reply = data.get("reply")
    if not mentions and not reply:
        return None
    payload = {"v": RELATION_VERSION, "mentions": mentions, "reply": reply}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def mention_label(mention: Any) -> str:
    if not isinstance(mention, dict):
        return "未知成员"
    if mention.get("all"):
        return "全体成员"
    nickname = str(mention.get("nickname") or "").strip()
    return nickname or "未知成员"


def render_content_template(template: str, relation_data: Any) -> str:
    data = parse_relation_data(relation_data)
    if not data:
        return template or ""
    mentions = data.get("mentions", [])

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        if index >= len(mentions):
            return "[提及:未知成员]"
        return f"[提及:{mention_label(mentions[index])}]"

    rendered = AT_TOKEN_RE.sub(replace, template or "")
    return rendered.replace(AT_TOKEN_ESCAPED, AT_TOKEN_LITERAL)


def truncate_reply_snapshot(text: str) -> str:
    value = (text or "").strip()
    if len(value) <= MAX_REPLY_SNAPSHOT_CHARS:
        return value
    return value[: MAX_REPLY_SNAPSHOT_CHARS - 1].rstrip() + "…"


def truncate_content_template(template: str, max_chars: int) -> str:
    """按字符预算截断，但绝不留下半个内部 At placeholder。"""
    if max_chars <= 0 or len(template) <= max_chars:
        return template
    value = template[:max_chars]
    for marker in (AT_TOKEN_LITERAL, AT_TOKEN_ESCAPED):
        start = value.rfind(marker)
        if start >= 0 and value.find("⟧", start) < 0:
            value = value[:start]
    return value.rstrip()
