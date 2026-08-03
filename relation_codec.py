"""ChatMemory 消息关系模板的编解码工具。"""

from html import escape as xml_escape
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
    """渲染 At 模板：正文部分做 XML 转义，At token 换成 <cm_mention> 结构标签。

    只转义用户可控正文，避免把生成的 <cm_mention> 二次转义成文本。
    """
    data = parse_relation_data(relation_data)
    if not data:
        return template or ""
    mentions = data.get("mentions", [])
    value = template or ""
    parts: list[str] = []
    cursor = 0
    for match in AT_TOKEN_RE.finditer(value):
        plain = value[cursor:match.start()].replace(
            AT_TOKEN_ESCAPED, AT_TOKEN_LITERAL
        )
        parts.append(xml_escape(plain, quote=True))
        index = int(match.group(1))
        if index >= len(mentions):
            parts.append('<cm_mention target="未知成员"/>')
        else:
            label = mention_label(mentions[index])
            parts.append(f'<cm_mention target="{xml_escape(label, quote=True)}"/>')
        cursor = match.end()
    tail = value[cursor:].replace(AT_TOKEN_ESCAPED, AT_TOKEN_LITERAL)
    parts.append(xml_escape(tail, quote=True))
    return "".join(parts)


def _current_mention_xml(mention: Any, self_id: str) -> str:
    if isinstance(mention, dict) and mention.get("all"):
        target = "all"
    elif isinstance(mention, dict):
        target_user_id = str(mention.get("user_id") or "").strip()
        if self_id and target_user_id == self_id:
            target = "assistant"
        else:
            target = mention_label(mention)
    else:
        target = "未知成员"
    return f'<cm_mention target="{xml_escape(target, quote=True)}"/>'


def _current_message_xml(template: str, mentions: list[Any], self_id: str) -> str:
    """把正文模板转成 XML mixed content，同时保持 At 的原始位置。"""
    value = template or ""
    parts: list[str] = []
    cursor = 0
    found = False
    for match in AT_TOKEN_RE.finditer(value):
        found = True
        plain = value[cursor:match.start()].replace(AT_TOKEN_ESCAPED, AT_TOKEN_LITERAL)
        parts.append(xml_escape(plain, quote=True))
        index = int(match.group(1))
        mention = mentions[index] if index < len(mentions) else None
        parts.append(_current_mention_xml(mention, self_id))
        cursor = match.end()
    tail = value[cursor:].replace(AT_TOKEN_ESCAPED, AT_TOKEN_LITERAL)
    parts.append(xml_escape(tail, quote=True))

    # 正常的新消息一定有带索引 placeholder；这里仅为损坏/第三方构造数据保底，
    # 不猜位置，只按关系数组顺序保留提及对象。
    if mentions and not found:
        parts.extend(_current_mention_xml(item, self_id) for item in mentions)
    return "".join(parts)


def build_current_turn_xml(
    template: str,
    relation_data: Any,
    self_id: str,
    speaker_nickname: str = "",
) -> str:
    """构建本轮焦点锚：当前发言者身份 + Reply/At；只输出昵称或 assistant，不暴露账号 ID。"""
    speaker_nickname = str(speaker_nickname or "").strip()
    speaker_line = (
        f'<cm_speaker current="1">{xml_escape(speaker_nickname, quote=True)}</cm_speaker>'
        if speaker_nickname
        else ""
    )

    data = parse_relation_data(relation_data)
    if not data:
        return speaker_line or "<cm_current/>"

    mentions = data.get("mentions") or []
    reply = data.get("reply")
    if not mentions and not isinstance(reply, dict):
        return speaker_line or "<cm_current/>"

    lines = ["<cm_current>"]
    if speaker_line:
        lines.append(speaker_line)
    if isinstance(reply, dict):
        target_user_id = str(reply.get("target_user_id") or "").strip()
        if reply.get("target_role") == "assistant" or (
            self_id and target_user_id == self_id
        ):
            target = "assistant"
        else:
            target = str(reply.get("target_nickname") or "").strip() or "未知成员"
        lines.append(f'<cm_reply target="{xml_escape(target, quote=True)}"/>')

    message = _current_message_xml(template, mentions, str(self_id or "").strip())
    if message:
        lines.append(f"<message>{message}</message>")
    lines.append("</cm_current>")
    return "\n".join(lines)


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
