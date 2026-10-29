"""ChatMemory 消息组件分类。

该模块只负责 AstrBot MessageChain → ChatMemory 字段的转换，不参与数据库、会话或
上下文接管，便于在没有完整 AstrBot 运行时的情况下单独测试。
"""

import os
import re
from typing import Optional
from urllib.parse import unquote, urlparse

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    Plain,
    Image,
    Video,
    Record,
    File,
    Face,
    Poke,
    At,
    AtAll,
    Reply,
    Forward,
    Unknown,
)
from .media_archive import mint_media_id
from .models import (
    K_TEXT,
    K_IMAGE,
    K_VIDEO,
    K_VOICE,
    K_FILE,
    K_EMOJI,
    K_POKE,
    K_FORWARD,
    K_SYSTEM,
)
from .relation_codec import (
    RELATION_VERSION,
    at_token,
    escape_plain_text,
    media_token,
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


def sanitize_file_name(name: str) -> str:
    """只保留安全 basename：去路径、去控制字符、去非法字符、限长 255（文件系统上限）。"""
    if not name:
        return ""
    normalized = str(name).replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].replace("\x00", "").strip()
    for char in ':*?"<>|':
        basename = basename.replace(char, "_")
    if basename in {"", ".", ".."}:
        return ""
    return basename[:255]


def _file_uri_to_path(uri: str) -> str:
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return ""
        path = unquote(parsed.path)
        # file:///C:/... 在 posix 端解析为 /C:/...，剥掉前导斜杠
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return path
    except Exception:
        return ""


def _media_source_ref(comp, kind: str) -> Optional[dict]:
    """解析媒体组件可落盘的来源；只做判断，不做任何 IO/下载。

    返回 ``{"local": path}`` / ``{"url": url}`` / ``{"base64": payload}`` /
    ``{"data": data_uri}``；无法确定时返回 None。
    """
    if kind == "file":
        # File 组件的 ``file`` 是 property，访问可能触发同步下载；只用 file_/url
        url = str(getattr(comp, "url", "") or "").strip()
        path = str(getattr(comp, "file_", "") or "").strip()
    else:
        url = str(getattr(comp, "url", "") or "").strip()
        path = str(getattr(comp, "path", "") or "").strip()
        file_ref = str(getattr(comp, "file", "") or "").strip()
        if file_ref:
            if file_ref.startswith(("http://", "https://")):
                return {"url": file_ref}
            if file_ref.startswith("file://"):
                local = _file_uri_to_path(file_ref)
                if local and os.path.exists(local):
                    return {"local": local}
                return None
            if file_ref.startswith("base64://"):
                return {"base64": file_ref[len("base64://"):]}
            if file_ref.startswith("data:"):
                return {"data": file_ref}
            if os.path.exists(file_ref):
                return {"local": file_ref}
    if path and os.path.exists(path):
        return {"local": path}
    if url:
        if url.startswith("file://"):
            local = _file_uri_to_path(url)
            if local and os.path.exists(local):
                return {"local": local}
            return None
        if url.startswith(("http://", "https://")):
            return {"url": url}
        if url.startswith("base64://"):
            return {"base64": url[len("base64://"):]}
        if url.startswith("data:"):
            return {"data": url}
    return None


def extract_user_template(
    event: AstrMessageEvent,
) -> tuple[str, list[dict], list, Optional[dict], list]:
    """按 MessageChain 顺序构建正文模板、media 条目（含元信息/归档 id）、
    媒体来源引用和 Reply 快照种子。"""
    try:
        chain = event.get_messages() or []
    except Exception:
        chain = []
    if not chain:
        return (
            escape_plain_text(getattr(event, "message_str", "") or ""),
            [],
            [],
            [],
            None,
        )

    parts: list[str] = []
    mentions: list[dict] = []
    media: list[dict] = []
    media_refs: list = []
    reply_seed: Optional[dict] = None
    # 纯媒体判定直接看组件 kind：链中是否存在带可见文本的组件
    # （Plain / Unknown 的 text 非空）。
    has_text = False

    def push_media(
        kind_value: str,
        media_id: str = "",
        name: str = "",
        poke_type: str = "",
        ref: Optional[dict] = None,
    ) -> None:
        # 统一规则：媒体/动作先占位；纯媒体/纯动作（无文本组件）时下方会清空 token
        index = len(media)
        entry: dict = {"kind": kind_value}
        if media_id:
            entry["id"] = media_id
        if name:
            entry["name"] = name
        if poke_type:
            entry["type"] = poke_type
        media.append(entry)
        media_refs.append(ref)
        parts.append(media_token(kind_value, index))

    for comp in chain:
        if isinstance(comp, Plain):
            text = comp.text or ""
            if text.strip():
                has_text = True
            parts.append(escape_plain_text(text))
        elif isinstance(comp, Unknown):
            # 平台无法识别的段:若带文本则按正文保留
            text = str(getattr(comp, "text", "") or "").strip()
            if text:
                has_text = True
                parts.append(escape_plain_text(text))
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
        elif isinstance(comp, Image):
            push_media(K_IMAGE, media_id=mint_media_id(),
                       ref=_media_source_ref(comp, K_IMAGE))
        elif isinstance(comp, Face):
            # QQ 表情:存 id;unicode emoji 本就位于 Plain 文本中
            push_media(K_EMOJI, str(getattr(comp, "id", "") or "").strip())
        elif isinstance(comp, Video):
            push_media(K_VIDEO, media_id=mint_media_id(),
                       ref=_media_source_ref(comp, K_VIDEO))
        elif isinstance(comp, Record):
            push_media(K_VOICE, media_id=mint_media_id(),
                       ref=_media_source_ref(comp, K_VOICE))
        elif isinstance(comp, File):
            name = sanitize_file_name(str(getattr(comp, "name", "") or ""))
            push_media(K_FILE, media_id=mint_media_id(), name=name,
                       ref=_media_source_ref(comp, K_FILE))
        elif isinstance(comp, Forward):
            push_media(K_FORWARD, str(getattr(comp, "id", "") or "").strip())
        elif isinstance(comp, Poke):
            target = getattr(comp, "target_id", None)
            if callable(target):
                target = target()
            poke_type = str(getattr(comp, "_type", "") or "").strip() or "126"
            push_media(K_POKE, str(target or "").strip(), poke_type=poke_type)
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
    template = "".join(parts).strip()
    # 纯媒体/纯动作判定看组件 kind（has_text）：链中没有任何带可见文本的
    # 组件时，不保留位置 token，正文直接由占位符逻辑生成；media 数组仍保留
    # （表情 id、戳一戳目标等元信息）。
    if not has_text:
        template = ""
    return template, mentions, media, media_refs, reply_seed


def build_relation_seed(event: AstrMessageEvent) -> tuple[str, Optional[dict]]:
    template, relation, _media, _refs = build_relation_seed_full(event)
    return template, relation


def build_relation_seed_full(
    event: AstrMessageEvent,
) -> tuple[str, Optional[dict], list[dict], list]:
    """与 ``build_relation_seed`` 相同，额外返回 media 条目与来源引用（归档用）。"""
    template, mentions, media, media_refs, reply_seed = extract_user_template(event)
    if not mentions and not media and not reply_seed:
        return template, None, [], []
    relation: dict = {
        "v": RELATION_VERSION,
        "mentions": mentions,
        "reply": reply_seed,
    }
    if media:
        relation["media"] = media
    return template, relation, media, media_refs


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
        elif isinstance(comp, Unknown):
            if (getattr(comp, "text", "") or "").strip():
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
            push(K_EMOJI)
        elif isinstance(comp, Poke):
            push(K_POKE)
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
        if message_type_value == "OtherMessage" and not kind:
            # 仅在未识别出任何内容时兜底为事件，避免丢弃同链已识别的 text/image
            kind = [K_SYSTEM]
    except Exception:
        pass

    # notice / request 类事件被 AstrBot 转成空消息（chain 空、message_str 空），
    # 但原始 JSON 仍在 message_obj.raw_message；归类为系统事件避免漏存。
    if not kind and system_event_summary(event):
        kind = [K_SYSTEM]

    # 挂载表情（mface）等被平台适配器丢弃的消息段：chain 为空但原始 JSON 里仍有
    # 消息段，按 emoji 归档，避免纯挂件消息完全漏存。
    if not kind and _raw_has_mface(event):
        kind = [K_EMOJI]

    return kind, at_id, reply_id, forward_id


def _raw_has_mface(event: AstrMessageEvent) -> bool:
    """原始 JSON 的消息段里是否含 mface（挂载表情，适配器会丢弃该段）。"""
    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None)
    if not isinstance(raw, dict):
        return False
    segments = raw.get("message")
    if not isinstance(segments, list):
        return False
    return any(
        isinstance(seg, dict) and str(seg.get("type") or "") == "mface"
        for seg in segments
    )


_NOTICE_LABELS = {
    "group_recall": "撤回消息",
    "friend_recall": "撤回消息",
    "group_upload": "群文件上传",
    "group_admin": "群管理员变动",
    "group_increase": "群成员增加",
    "group_decrease": "群成员减少",
    "group_ban": "群禁言",
    "group_card": "群名片变更",
    "offline_file": "离线文件",
    "client_status": "客户端状态变更",
}

_NOTIFY_SUB_LABELS = {
    "title": "设置群头衔",
    "lucky_king": "群运气王",
    "honor": "群荣誉变更",
}


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}分钟" if seconds % 60 == 0 else f"{minutes}分{seconds % 60}秒"
    hours = seconds // 3600
    return f"{hours}小时"


def _clean_event_text(value: str, limit: int) -> str:
    """净化 notice 携带的用户文本（头衔/文件名）：去控制字符、转义 token 前缀、限长。"""
    text = "".join(
        ch for ch in str(value or "") if ch >= " " or ch == "\t"
    ).strip()
    if not text:
        return ""
    return escape_plain_text(text)[:limit]


def system_event_summary(event: AstrMessageEvent) -> str:
    """从 notice/request 原始事件提取可读摘要（不含账号，仅类别 + 关键信息）。

    AstrBot 把 OneBot notice/request 转成空消息，CM 据此补存系统事件；
    摘要用于落库 content（kind 仍为 system_event，不写成 text 类型）。
    """
    parts = _system_event_parts(event)
    return parts[0] if parts else ""


def _system_event_parts(
    event: AstrMessageEvent,
) -> Optional[tuple[str, str, str, str, str, str]]:
    """返回 ``(summary, action, detail, target_id, operator_id, event_type)``。

    summary 与 ``system_event_summary`` 文本完全一致（落库/UI 展示）；
    action/detail/target/operator/type 供 ``<cm_event>`` 上下文渲染使用。
    target_id=被操作对象，operator_id=操作者（OneBot v11 仅部分事件提供，
    拿不到为空串）；event_type 为事件大类（ban/member/title/recall/resource/
    poke/system）。
    """
    raw = None
    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        raw = getattr(message_obj, "raw_message", None)
    if not isinstance(raw, dict):
        return None
    post_type = str(raw.get("post_type") or "")
    if post_type == "request":
        # 好友/加群请求类通知：取消归档（与戳一戳同策略，不入 system_event）
        return None
    if post_type != "notice":
        return None
    notice_type = str(raw.get("notice_type") or "")
    uid = str(raw.get("user_id") or "")
    op = str(raw.get("operator_id") or "")
    if notice_type == "group_ban":
        if str(raw.get("sub_type") or "") == "lift_ban":
            return ("[解除禁言]", "解除禁言", "", uid, op, "ban")
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 0:
            label = _fmt_duration(duration)
            return (f"[群禁言 {label}]", "群禁言", label, uid, op, "ban")
        return ("[群禁言]", "群禁言", "", uid, op, "ban")
    if notice_type == "group_admin":
        if str(raw.get("sub_type") or "") == "set":
            return ("[设置群管理员]", "设置群管理员", "", uid, "", "member")
        return ("[取消群管理员]", "取消群管理员", "", uid, "", "member")
    if notice_type == "group_upload":
        # 上传类事件只有操作者（上传者），没有被操作对象
        file_info = raw.get("file") or {}
        name = _clean_event_text(file_info.get("name"), 40) if isinstance(
            file_info, dict
        ) else ""
        if name:
            return (f"[群文件上传] {name}", "群文件上传", name, "", uid, "resource")
        return ("[群文件上传]", "群文件上传", "", "", uid, "resource")
    if notice_type == "notify":
        sub = str(raw.get("sub_type") or "")
        if sub == "title":
            title = _clean_event_text(raw.get("title"), 50)
            if title:
                return (f"[设置群头衔] {title}", "设置群头衔", title, uid, "", "title")
            return ("[设置群头衔]", "设置群头衔", "", uid, "", "title")
        if sub == "poke":
            # 戳一戳走消息路径（适配器把带 target_id 的 poke 转成 Poke 组件），
            # 不作为系统事件归档；无 target_id 的实现差异也一律不归档。
            return None
        label = _NOTIFY_SUB_LABELS.get(sub)
        if label:
            return (f"[{label}]", label, "", uid, "", "title")
        return ("[系统通知]", "系统通知", "", uid, "", "system")
    if notice_type == "poke":
        # 老实现（go-cqhttp）的独立 poke notice：同样按非系统事件处理
        return None
    if notice_type == "group_msg_emoji_like":
        # NapCat 扩展 notice：表情回应（给某条消息添加/取消 emoji 回应）。
        # 被回应消息的作者由捕获侧按 message_id 本地关联补齐（同撤回）。
        if bool(raw.get("is_add")):
            return ("[表情回应]", "表情回应", "", "", uid, "reaction")
        return ("[取消表情回应]", "取消表情回应", "", "", uid, "reaction")
    if notice_type == "group_recall":
        return ("[撤回消息]", "撤回消息", "", uid, op, "recall")
    if notice_type == "friend_recall":
        return ("[撤回消息]", "撤回消息", "", uid, "", "recall")
    if notice_type in ("group_increase", "group_decrease"):
        label = _NOTICE_LABELS.get(notice_type)
        # v11：operator_id 为邀请者/同意者（增加）或踢人者（减少）
        return (f"[{label}]", label, "", uid, op, "member")
    if notice_type == "offline_file":
        # 传输类事件只有操作者（发送者），没有被操作对象
        file_info = raw.get("file") or {}
        name = _clean_event_text(file_info.get("name"), 40) if isinstance(
            file_info, dict
        ) else ""
        if name:
            return (f"[离线文件] {name}", "离线文件", name, "", uid, "resource")
        return ("[离线文件]", "离线文件", "", "", uid, "resource")
    if notice_type == "friend_add":
        # 好友添加通知：取消归档（好友相关通知不进 system_event）
        return None
    if notice_type == "client_status":
        # 无 user_id：客户端状态变更不针对具体成员
        return ("[客户端状态变更]", "客户端状态变更", "", "", "", "system")
    label = _NOTICE_LABELS.get(notice_type)
    # 撤回类只有 message_id，原文片段由捕获侧本地关联补齐（见 main._recall_snippet）
    if label:
        return (f"[{label}]", label, "", uid, "", "member")
    return ("[系统事件]", "系统事件", "", "", "", "system")


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
        elif isinstance(comp, Unknown):
            text = str(getattr(comp, "text", "") or "").strip()
            if text:
                push(K_TEXT)
                parts.append(text)
        elif isinstance(comp, Image):
            push(K_IMAGE)
        elif isinstance(comp, Video):
            push(K_VIDEO)
        elif isinstance(comp, Record):
            push(K_VOICE)
        elif isinstance(comp, File):
            push(K_FILE)
        elif isinstance(comp, Face):
            push(K_EMOJI)
        elif isinstance(comp, Poke):
            push(K_POKE)
        elif isinstance(comp, Forward):
            push(K_FORWARD)
        elif isinstance(comp, Reply):
            # 纯引用回复（无 Plain/媒体）也要落库：用引用携带的原文兜底为 text
            snapshot = str(
                getattr(comp, "message_str", None)
                or getattr(comp, "text", None)
                or ""
            ).strip()
            if snapshot:
                push(K_TEXT)
                parts.append(snapshot)
    return kind, "".join(parts).strip()


def content_placeholder(kind: list[str]) -> str:
    if not kind:
        return ""
    return f"[{kind[0]}]"
