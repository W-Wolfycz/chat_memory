"""ChatMemory 消息关系模板的编解码工具。"""

from html import escape as xml_escape
import json
import re
from typing import Any, Callable, Optional

from .qq_face import face_name


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
# kind → 上下文占位标签（poke/emoji/file 由 _media_label_of 特判，
# 其余与 context_builder.MEDIA_PLACEHOLDER_LABELS 同名）
MEDIA_KIND_LABELS = {
    "image": "<cm_image/>",
    "video": "<cm_video/>",
    "voice": "<cm_voice/>",
    "file": "<cm_file/>",
    "emoji": "<cm_emoji/>",
    "forward": "<cm_forward/>",
}
MAX_REPLY_SNAPSHOT_CHARS = 300


def strip_relation_tokens(text: str) -> str:
    """把残留的 ``⟦CM_AT:n⟧`` / ``⟦CM_*:n⟧`` token 换成占位标签。

    计划行（``chat_memory_reply_messages``）没有 ``relation_data``，渲染时无法按
    media/mentions 下标还原位置 token；不清理的话裸 token 会直接进 LLM 上下文
    （模型会照着⟦CM_AT:0⟧格式模仿输出）。有 relation 的行走这里也是安全的：
    位置 token 早已被 ``render_content_template`` 替换掉，只剩用户正文。
    """
    value = str(text or "")
    if not value:
        return value
    value = AT_TOKEN_RE.sub('<cm_mention target="未知成员"/>', value)
    return MEDIA_TOKEN_RE.sub("<cm_unknown/>", value)


def escape_plain_text(text: str) -> str:
    """把用户文本里的每个 ``⟦``/``⟧`` 换成哨兵字符，杜绝伪造内部 At / 媒体 token。

    保证范围：含 ``⟦``/``⟧`` 的输入往返双射；字面哨兵字符 ``⦑``/``⦒`` 本身
    不参与转义（它们只会在展示层被还原成 ⟦/⟧，不会被解析成内部 token，
    因此不构成注入，但往返不等价）。
    """
    return (text or "").replace("⟦", _SENTINEL_LEFT).replace("⟧", _SENTINEL_RIGHT)


def unescape_plain_text(text: str) -> str:
    """``escape_plain_text`` 的逆操作。"""
    return (text or "").replace(_SENTINEL_LEFT, "⟦").replace(_SENTINEL_RIGHT, "⟧")


def build_event_sentence(
    event: dict,
    *,
    name_format: Optional[Callable[[str], str]] = None,
    text_format: Optional[Callable[[str], str]] = None,
) -> str:
    """把 system_event 的结构化字段组装成自然语言句子。

    与 QQ 群聊系统消息展示一致（如「长风 被 小明 禁言了 10分钟」）。
    ``name_format`` 用于包装昵称（上下文渲染传 <cm_nickname> 包装器，
    UI 展示不传）；``text_format`` 用于转义正文片段（detail 用户可控，
    上下文渲染传 XML 转义，UI 展示不传）。
    """
    fmt = name_format or (lambda name: name)
    tfmt = text_format or (lambda text: text)
    target = str(event.get("target") or "").strip()
    operator = str(event.get("operator") or "").strip()
    action = tfmt(str(event.get("action") or "").strip() or "系统事件")
    detail = tfmt(str(event.get("detail") or "").strip())
    t = fmt(target) if target else ""
    o = fmt(operator) if operator else ""

    def join(*parts: str) -> str:
        return " ".join(part for part in parts if part)

    generic = action + (f"：{detail}" if detail else "")
    if not t and not o:
        # 无任何角色信息（如撤回未关联/客户端状态/未知类型）：退回动作文本
        return generic
    if action == "群禁言":
        base = join(t, "被", o, "禁言") if o else f"{t} 被禁言"
        return base + (f"了 {detail}" if detail else "")
    if action == "解除禁言":
        return join(t, "被", o, "解除禁言") if o else f"{t} 被解除禁言"
    if action == "设置群管理员":
        return join(t, "被设为群管理员")
    if action == "取消群管理员":
        return join(t, "被取消群管理员")
    if action == "群成员增加":
        return join(t, "被", o, "邀请加入群聊") if o else join(t, "加入了群聊")
    if action == "群成员减少":
        return join(t, "被", o, "移出群聊") if o else join(t, "退出了群聊")
    if action == "群名片变更":
        return join(t, "修改了群名片") if t else generic
    if action == "设置群头衔":
        return join(t, "被授予群头衔") + (f"：{detail}" if detail else "")
    if action == "群运气王":
        return join(t, "成为群运气王") if t else generic
    if action == "群荣誉变更":
        return join(t, "获得群荣誉") if t else generic
    if action == "撤回消息":
        base = join(t, "的消息被", o, "撤回") if o else join(t, "的消息被撤回")
        return base + (f"：{detail}" if detail else "")
    if action == "群文件上传":
        return join(o, "上传了群文件") + (f"：{detail}" if detail else "") if o else generic
    if action == "离线文件":
        return join(o, "发送了离线文件") + (f"：{detail}" if detail else "") if o else generic
    if action == "戳一戳":
        if t:
            return join(o, "戳了戳", t)
        return join(o, "戳一戳") if o else "戳一戳"
    if action == "表情回应":
        emoji = face_name(str(event.get("emoji_id") or ""))
        if t:
            return (
                join(o, "用", emoji, "回应了", t, "的消息")
                if emoji else join(o, "回应了", t, "的消息")
            )
        return (
            join(o, "用", emoji, "回应了一条消息")
            if emoji else join(o, "回应了一条消息")
        )
    if action == "取消表情回应":
        if t:
            return join(o, "取消了对", t, "消息的回应")
        return join(o, "取消了一条消息的回应")
    return generic


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
    event = data.get("event")
    if not isinstance(event, dict):
        event = None
    if not mentions and not media and not poke_target and not reply and not event:
        return None
    payload = {"v": RELATION_VERSION, "mentions": mentions, "reply": reply}
    if media:
        payload["media"] = media
    if poke_target:
        payload["poke_target"] = poke_target
    if event:
        payload["event"] = event
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def mention_label(mention: Any) -> str:
    if not isinstance(mention, dict):
        return "未知成员"
    if mention.get("all"):
        return "全体成员"
    nickname = str(mention.get("nickname") or "").strip()
    return nickname or "未知成员"


def _media_label_of(item: Any) -> str:
    """单个 media 条目 → 上下文占位标签（emoji 带名称、file 带文件名、poke 走事件）。"""
    kind = str(item.get("kind") or "") if isinstance(item, dict) else ""
    if kind == "poke":
        # 戳一戳并入事件协议；有已解析的目标昵称时带上（无操作者，由该条消息的
        # <cm_nickname> 承担发言者身份）
        name = str(item.get("name") or "").strip()
        if name:
            return (
                '<cm_event type="poke">戳了戳 '
                f'<cm_nickname>{xml_escape(name, quote=True)}</cm_nickname></cm_event>'
            )
        return '<cm_event type="poke">戳一戳</cm_event>'
    if kind == "emoji":
        face_id = item.get("id")
        name = face_name("" if face_id is None else str(face_id).strip())
        if name:
            return f'<cm_emoji>{xml_escape(name, quote=True)}</cm_emoji>'
    if kind == "file":
        name = str(item.get("name") or "").strip()
        if name:
            return f'<cm_file name="{xml_escape(name, quote=True)}"/>'
    return MEDIA_KIND_LABELS.get(kind, "<cm_unknown/>")


def render_content_template(template: str, relation_data: Any) -> str:
    """渲染 At 模板：正文部分做 XML 转义，At token 换成 <cm_mention> 结构标签。

    只转义用户可控正文，避免把生成的 <cm_mention> 二次转义成文本。
    """
    data = parse_relation_data(relation_data)
    if not data:
        # 无关系数据时正文里没有真 token（有的话也在 strip_relation_tokens 里处理），
        # 但写入时做过的 ⟦⟧→哨兵 转义必须在这里还原，否则 `⟦` 会以 `⦑` 形态落进上下文。
        return unescape_plain_text(template or "")
    mentions = data.get("mentions", [])
    media = data.get("media", [])
    value = template or ""
    parts: list[str] = []
    cursor = 0

    def media_label(index: int) -> str:
        if index >= len(media) or not isinstance(media[index], dict):
            return "<cm_unknown/>"
        return _media_label_of(media[index])

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
            _media_label_of(item)
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
