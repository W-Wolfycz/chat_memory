"""ChatMemory 消息关系模板的编解码工具。"""

from html import escape as xml_escape
import json
import re
from typing import Any, Optional


RELATION_VERSION = 1
AT_TOKEN_RE = re.compile(r"⟦CM_AT:(\d+)⟧")
AT_TOKEN_LITERAL = "⟦CM_AT:"
# 用户正文转义哨兵（成对）：用户文本中的每个 ``⟦``/``⟧`` 都替换为对应哨兵
# （比 ⟦⟧ 更罕见、键盘/IME 几乎无法输入），使内部 token（一律以 ⟦ 开头、
# ⟧ 结尾）与用户文字绝无碰撞。展示侧 unescape_plain_text 反向还原，任意含
# ⟦/⟧ 的输入均双射。
_SENTINEL_LEFT = "⦑"
_SENTINEL_RIGHT = "⦒"
# 媒体/动作位置 token（relation v1 新增 media 数组，schema 零迁移）：
# 统一规则——凡与文字同时出现的媒体/动作类型，一律按原始位置写类型化
# token（如 ⟦CM_IMAGE:0⟧），索引为 media 数组中的统一下标；纯媒体/纯动作
# 消息（消息链中无文本组件，判定看组件 kind）不走 token，正文直接存占位符
# （media 数组仍保留元信息）。
_MEDIA_PREFIXES = ("IMAGE", "VIDEO", "VOICE", "FILE", "EMOJI", "FORWARD", "POKE")
MEDIA_TOKEN_RE = re.compile(
    r"⟦CM_(?:IMAGE|VIDEO|VOICE|FILE|EMOJI|FORWARD|POKE):(\d+)⟧"
)
# kind → 上下文占位标签（与 context_builder.MEDIA_PLACEHOLDER_LABELS 保持一致）
MEDIA_KIND_LABELS = {
    "image": "<cm_image/>",
    "video": "<cm_video/>",
    "voice": "<cm_voice/>",
    "file": "<cm_file/>",
    "emoji": "<cm_emoji/>",
    "forward": "<cm_forward/>",
    "poke": "<cm_poke/>",
}
MAX_REPLY_SNAPSHOT_CHARS = 300


def escape_plain_text(text: str) -> str:
    """把用户文本里的每个 ``⟦``/``⟧`` 换成哨兵字符，杜绝伪造内部 At / 媒体 token。"""
    return (text or "").replace("⟦", _SENTINEL_LEFT).replace("⟧", _SENTINEL_RIGHT)


def unescape_plain_text(text: str) -> str:
    """``escape_plain_text`` 的逆操作。"""
    return (text or "").replace(_SENTINEL_LEFT, "⟦").replace(_SENTINEL_RIGHT, "⟧")


def at_token(index: int) -> str:
    return f"⟦CM_AT:{index}⟧"


def media_token(kind: str, index: int) -> str:
    prefix = _MEDIA_KIND_PREFIXES.get(kind, "IMAGE")
    return f"⟦CM_{prefix}:{index}⟧"


_MEDIA_KIND_PREFIXES = {
    "image": "IMAGE",
    "video": "VIDEO",
    "voice": "VOICE",
    "file": "FILE",
    "emoji": "EMOJI",
    "forward": "FORWARD",
    "poke": "POKE",
}


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
    media = data.get("media")
    if not isinstance(media, list):
        data["media"] = []
    poke_target = data.get("poke_target")
    if poke_target is not None and not isinstance(poke_target, str):
        data["poke_target"] = ""
    if data.get("reply") is not None and not isinstance(data.get("reply"), dict):
        data["reply"] = None
    return data


def dump_relation_data(data: Optional[dict]) -> Optional[str]:
    if not data:
        return None
    mentions = data.get("mentions") or []
    media = data.get("media") or []
    poke_target = data.get("poke_target") or ""
    reply = data.get("reply")
    if not mentions and not media and not poke_target and not reply:
        return None
    payload = {"v": RELATION_VERSION, "mentions": mentions, "reply": reply}
    if media:
        payload["media"] = media
    if poke_target:
        payload["poke_target"] = poke_target
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
    media = data.get("media", [])
    value = template or ""
    parts: list[str] = []
    cursor = 0

    def media_label(index: int) -> str:
        if index >= len(media) or not isinstance(media[index], dict):
            return "<cm_image/>"
        return MEDIA_KIND_LABELS.get(str(media[index].get("kind") or ""), "<cm_image/>")

    # At 与媒体 token 可能交错出现：合并后按位置顺序渲染
    matches = [(m.start(), m.end(), "at", int(m.group(1))) for m in AT_TOKEN_RE.finditer(value)] + [
        (m.start(), m.end(), "media", int(m.group(1))) for m in MEDIA_TOKEN_RE.finditer(value)
    ]
    matches.sort(key=lambda item: item[0])
    for start, end, token_kind, index in matches:
        parts.append(xml_escape(unescape_plain_text(value[cursor:start]), quote=True))
        if token_kind == "at":
            if index >= len(mentions):
                parts.append('<cm_mention target="未知成员"/>')
            else:
                label = mention_label(mentions[index])
                parts.append(f'<cm_mention target="{xml_escape(label, quote=True)}"/>')
        else:
            parts.append(media_label(index))
        cursor = end
    tail = value[cursor:]
    parts.append(xml_escape(unescape_plain_text(tail), quote=True))
    result = "".join(parts)
    if not result.strip() and media:
        # 纯媒体消息（正文无 token 且无文本）：按 media 数组顺序输出占位标签
        labels = [
            MEDIA_KIND_LABELS.get(str(item.get("kind") or ""), "")
            for item in media
            if isinstance(item, dict)
        ]
        result = " ".join(label for label in labels if label)
    return result


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
        plain = unescape_plain_text(value[cursor:match.start()])
        parts.append(xml_escape(plain, quote=True))
        index = int(match.group(1))
        mention = mentions[index] if index < len(mentions) else None
        parts.append(_current_mention_xml(mention, self_id))
        cursor = match.end()
    tail = unescape_plain_text(value[cursor:])
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
    """按字符预算截断，但绝不留下半个内部 At / 媒体 placeholder。"""
    if max_chars <= 0 or len(template) <= max_chars:
        return template
    value = template[:max_chars]
    markers = [AT_TOKEN_LITERAL]
    for prefix in _MEDIA_PREFIXES:
        markers.append(f"⟦CM_{prefix}:")
    for marker in markers:
        start = value.rfind(marker)
        if start >= 0 and value.find("⟧", start) < 0:
            value = value[:start]
    # 截断点落在 token 前缀字面中间（如 ⟦CM_IMAG）时剥掉未闭合的 ⟦ 残片。
    # 用户正文的 ⟦ 已全部被哨兵转义，此处只会命中真实 token 前缀形态。
    start = value.rfind("⟦")
    if start >= 0 and value.find("⟧", start) < 0:
        fragment = value[start:]
        if (
            fragment == "⟦"
            or fragment == "⟦C"
            or fragment == "⟦CM"
            or fragment.startswith("⟦CM_")
        ):
            value = value[:start]
    return value.rstrip()
