"""ChatMemory — 独立对话记录存档。

每条记录带两个独立维度的状态字段：

- ``llm_status``：LLM 配对状态（单值，``''`` = 默认/未走 LLM）
  - ``''``            默认（命令、插件 ``set_result``、纯媒体等）
  - ``'llm_pending'`` LLM 触发但 assistant 未成功（孤儿 user）
  - ``'llm_success'`` LLM 路径且 assistant 成功回复（user 与 assistant 双侧同步）
  - ``'proactive'``   主动消息（assistant 单边，含 cron）
  - ``'orphan'``      user 漏存（DB 写入失败）但 assistant 来了

- ``content_kind``：消息内容形态（JSON 数组，可多值）
  - ``'text'`` / ``'image'`` / ``'video'`` / ``'voice'`` / ``'file'``
    / ``'emoji'`` / ``'forward'`` / ``'system_event'`` / ``'poke'``
  - ``[]`` 空数组 = empty（如纯 @ 无文字、纯 Reply 无文字）
  - ``'at'`` / ``'reply'`` 不入 content_kind，用独立字段 ``at_id`` / ``reply_id`` 表达

assistant 配对：新记录优先用内部 ``turn_id``；旧记录用 ``pair_id`` = 对应 user 的
``message_id`` 回退。平台无 mid 时也可通过 ``turn_id`` 配对。
"""

import asyncio
import base64
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.star import Star, Context, StarTools
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import TextPart

from .storage import (
    DBManager,
    effective_cross_session,
    invalid_blacklist_entries,
    normalize_session_blacklist,
    session_blacklisted,
)
from .media_archive import MediaArchiver
from .group_names import GroupNameCache, fetch_group_names
from . import shared_store
from .ui.repository import ChatMemoryRepository
from .ui.web_api import ChatMemoryUiWebApi
from .message_classifier import (
    classify_assistant_chain as _classify_assistant_chain_impl,
    classify_content as _classify_content_impl,
    content_placeholder as _content_placeholder_impl,
    build_relation_seed as _build_relation_seed_impl,
    build_relation_seed_full as _build_relation_seed_full_impl,
    extract_text as _extract_text_impl,
    system_event_summary as _system_event_summary_fn,
    _system_event_parts as _system_event_parts_fn,
)
from .context_builder import (
    CM_GENERAL_RULES,
    CROSS_SESSION_CONTEXT_INSTRUCTION,
    CROSS_SESSION_SOURCE_MARKER_PREFIX,
    FULL_GROUP_CONTEXT_INSTRUCTION,
    TakeoverContextBuilder,
    strip_cm_xml_tags as _strip_cm_xml_tags_impl,
    strip_bracket_media_placeholders as _strip_bracket_media_placeholders_impl,
    strip_reasoning_prefix as _strip_reasoning_prefix_impl,
)
from .models import (
    LLM_DEFAULT as _LLM_DEFAULT,
    LLM_ORPHAN as _LLM_ORPHAN,
    LLM_PENDING as _LLM_PENDING,
    LLM_PROACTIVE as _LLM_PROACTIVE,
    LLM_SUCCESS as _LLM_SUCCESS,
    K_SYSTEM as _K_SYSTEM,
    MEDIA_KINDS as _MEDIA_KINDS,
    SEND_ATTEMPTED as _SEND_ATTEMPTED,
    SEND_PREPARED as _SEND_PREPARED,
)
from .relation_codec import (
    AT_TOKEN_RE as _AT_TOKEN_RE,
    MEDIA_TOKEN_RE as _MEDIA_TOKEN_RE,
    build_current_turn_xml,
    dump_relation_data,
    escape_plain_text as _escape_plain_text,
    truncate_content_template,
)


_TAKEOVER_APPLIED_EXTRA = "chat_memory_takeover_applied"
_CURRENT_FOCUS_INJECTED_EXTRA = "chat_memory_current_focus_injected"
# 配置白名单可直接选的 content_kind（与存储值一一对应；旧大类 media 等值忽略）
_CONTENT_KIND_OPTIONS = frozenset(
    {"text", "image", "video", "voice", "emoji", "poke", "file", "forward", "event"}
)
# 事件子类全集（relation_data.event.type）
_ALL_EVENT_TYPES = frozenset(
    {"ban", "member", "title", "recall", "resource", "poke", "reaction", "system"}
)

# 自己发言的平台化（方案 B）：发送流程结束后，从平台群历史里把自己刚发的消息拉回来。
#
# 为什么不用"包装 event.send"：那样只能拿到本地视角（没有平台 id），而消息表的主键是
# 平台共享 id（`real_seq`）。群历史里的自己发言由 NapCat 走同一条 parseMessage 产出，
# `post_type` 直接就是 `message_sent`、带 `real_seq`（＝观察侧同一个服务器序号），
# 所以拉回来就能和别的实例合并到同一条物理消息上——本质是"把推不到的回显拉回来"。
# 回显的 push 通道：直接订阅适配器的 aiocqhttp 事件总线（`bot.on(...)`，公开装饰器）。
# 事件名走 `message_sent.self.normal`，总线按名字逐级回退（…→ message_sent → 根），
# 订阅根名即可收到全部自身消息回显。这条通道**不经过 AstrBot pipeline**，所以既不碰
# `ignore_bot_self_message`，也没有"自己的消息被当成用户消息回一句"的风险。
_ECHO_BUS_EVENT = "message_sent"
_ECHO_SUBSCRIBED_MARK = "_chat_memory_echo_subscribed"
_ECHO_HANDLER_MARK = "_chat_memory_echo_handler"
_MESSAGE_TYPE_BY_OB11 = {"group": "GroupMessage", "private": "FriendMessage"}
# 连续这么多轮巡检都没订上 push 就声明一次"满效率需要 NapCat 回显"（避免只靠猜）。
_PUSH_DECLARE_ROUNDS = 3

# 回显订阅巡检：启动时立刻订一次，之后按这个间隔补齐（平台重载会重建 bot 与总线）。
# **不依赖任何消息**——测试端没有真实消息输入时，重载插件就应当看到结果。
_ECHO_WATCH_INTERVAL_SEC = 60
# 切读门槛：双写必须跑满这么多天，配置的切读才生效（"配了但没到日子"＝先只写不读）。
# 显式 source="v2" 的调用不受它限制——那是给对拍/灰度用的程序化开关，不是用户开关。
# 同一个数也被查询台用（ui/repository.py），所以常量定义在 shared_store 里。
_SHADOW_READ_MIN_DAYS = shared_store.SHADOW_READ_MIN_DAYS
# 本地开关：置 True 时上面的天数不再作为条件（仅供本机验证，不进配置、不写日志）
_DEBUG_IGNORE_CUTOVER_WINDOW = False
# 主动查"自己在这个群里的显示名"的缓存时长：群名片改了最多 6 小时后自动跟上
_MEMBER_NAME_TTL_SEC = 6 * 3600
# 成员名缓存条数上限（大约 = 2000 个 (群, 人) 组合）
_MEMBER_NAME_CACHE_MAX = 2000
_ECHO_WATCH_FAST_ROUNDS = 10      # 前几轮用短间隔，等平台实例加载完
_ECHO_WATCH_FAST_INTERVAL_SEC = 2

# 自己发言的平台化（方案 B）：发送流程结束后，从平台群历史里把自己刚发的消息拉回来。
_SENT_HISTORY_ACTION = "get_group_msg_history"
# 撤回通知常**早于**目标消息落库（平台侧两个事件、或消息还在写）：当场标不上就按这个
# 退避重试，耗尽才放弃（放弃后还有启动补标兜底）。总等待约 13 秒，覆盖全部实测场景。
_RECALL_RETRY_DELAYS = (0.5, 1.5, 3.0, 8.0)
# 待重试的撤回上限：目标迟迟不落库（消息被平台拒收/写失败）时不让内存无限增长。
_RECALL_PENDING_MAX = 200
# 发送侧记账的认领宽限（秒）：push 回显（含多段）在发送结束后这么久内仍算本轮的。
# 段与段之间由分段间隔拉开（1.5~3.5s），一整轮的段都在这个窗口内到齐。
_ECHO_CLAIM_GRACE_SEC = 60
_SENT_HISTORY_COUNT = 20                 # 一次拉最近 N 条；主键去重，宁多勿漏
_SENT_HISTORY_WINDOW_SEC = 300           # 首次拉取的回看窗口（秒）
_SENT_HISTORY_DELAYS = (0.6, 1.5, 3.0)   # 服务端写入有传播延迟，短退避重试
_SENT_HISTORY_TIMEOUT_SEC = 15            # 单次 call_action 的硬上限（客户端不响应时别挂死）
_KIND_BY_SEGMENT = {
    "image": "image", "record": "voice", "video": "video", "file": "file",
    "face": "emoji", "poke": "poke",
}


def _normalize_echo_kind(kind: list[str], content: str) -> tuple[list[str], str]:
    """回显行的 content_kind 按**入站口径**归一：戳一戳在入站侧是 ``["event"]``。

    同一条物理消息只写一行，谁先写谁的 kind 就留下（合并只补空）；两侧口径不一致时，
    按 kind 白名单过滤会出现"同一条消息有时匹配、有时不匹配"。
    """
    if "poke" in (kind or []):
        return ["event"], content or "[戳一戳]"
    return kind, content


def _render_ob11_segments(segments) -> dict:
    """OB11 消息段数组 → 落库所需字段（词表与入站/出站分类器保持一致）。

    返回 ``{"kind": [...], "text": ..., "at_id": ..., "reply_id": ..., "forward_id": ...}``。
    同一条物理消息在观察侧与发送侧必须渲染成同一套 kind，否则合并视图会两副面孔。
    """
    kind: list[str] = []
    parts: list[str] = []
    out: dict = {
        "kind": kind, "text": "", "at_id": None, "reply_id": None, "forward_id": None,
    }

    def push(value: str) -> None:
        if value not in kind:
            kind.append(value)

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            text = str(data.get("text") or "")
            if text.strip():
                push("text")
                parts.append(text)
        elif seg_type in _KIND_BY_SEGMENT:
            push(_KIND_BY_SEGMENT[seg_type])
        elif seg_type in ("forward", "node", "nodes"):
            push("forward")
            if out["forward_id"] is None and data.get("id"):
                out["forward_id"] = str(data["id"])
        elif seg_type == "at":
            if out["at_id"] is None and data.get("qq"):
                out["at_id"] = str(data["qq"])
        elif seg_type == "reply":
            if out["reply_id"] is None and data.get("id"):
                out["reply_id"] = str(data["id"])
        elif seg_type in ("json", "xml", "markdown"):
            push("text")
            parts.append(f"[{seg_type}]")
    out["text"] = "".join(parts).strip()
    return out


def _select_own_history_items(items, *, self_id: str, since_ts: int) -> list[dict]:
    """群历史 → "自己发的、窗口内的、带 real_seq 的"消息（升序，按 real_seq 去重）。

    没有 ``real_seq`` 的条目直接跳过：没有平台共享 id 就无法与观察侧合并，
    硬塞一条本地键行会把"消息表只放看得见的消息"这条线拉歪。
    """
    picked: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        if not self_id or str(sender.get("user_id") or "") != str(self_id):
            continue
        real_seq = str(item.get("real_seq") or "")
        if not real_seq or real_seq in seen:
            continue
        try:
            ts = int(item.get("time") or 0)
        except (TypeError, ValueError):
            continue
        if since_ts and ts and ts < since_ts:
            continue
        seen.add(real_seq)
        picked.append(item)
    picked.sort(key=lambda it: int(it.get("time") or 0))
    return picked


async def _first_group_names(calls: list[tuple[str, object]]) -> tuple[str, list]:
    """并发问所有实例的 ``get_group_list``，返回第一个有结果的 ``(平台名, 群名对)``。

    平台名跟着"哪一次调用给出的结果"一起返回：原来落库时用"第一个平台实例"的名字，
    多平台类型时会把这些群名挂到错误的 ``platform_name`` 下（查询台按
    ``(platform_name, session_id)`` join 不到）。
    """
    tasks = {
        asyncio.create_task(fetch_group_names(call)): name for name, call in calls
    }
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                pairs = task.result() if not task.cancelled() else []
                if pairs:
                    return tasks[task], pairs
        return "", []
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.max_len = config.get("max_content_length", 0)
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)
        # 日志前缀附加机器人 ID（顶层配置）：多 Bot 实例环境便于定位。
        # 日志等级不再由插件配置：AstrBot WebUI 可运行期修改插件日志等级。
        self._config = config
        self.log_with_bot_id = self._resolve_log_with_bot_id(config)
        # 与 AstrBot 内置「群聊上下文感知」共存警告（1.4.1）：始终启用，进程内只提示一次。
        self._builtin_ltm_warned = False
        # 未知 notice 类型的 WARNING 去重（按 post_type/notice_type/sub_type）
        self._warned_unknown_notices: set[tuple[str, str, str]] = set()

        ct_conf = config.get("context_takeover", {}) or {}
        self.ct_enable = bool(ct_conf.get("enable", False))
        self.ct_cross_session = bool(ct_conf.get("cross_session", False))
        self.ct_full_group = bool(ct_conf.get("full_group", False))
        # 跨会话黑名单（双向生效）：名单内的会话不参与跨会话记忆——既不作为来源出现在
        # 别人的记忆里，自己也读不到任何其他会话；它们自己的同会话记忆不受影响。
        self.ct_cross_session_blacklist = normalize_session_blacklist(
            ct_conf.get("cross_session_blacklist", [])
        )
        self._warn_invalid_blacklist(ct_conf.get("cross_session_blacklist", []))
        self._blacklist_noted: set[str] = set()
        # 只钳下限：避免负数触发 SQLite LIMIT -1（等价于不限制）；上限交给用户决定。
        self.ct_limit_rounds = max(1, int(ct_conf.get("limit_rounds", 30)))
        self.ct_max_context_chars = max(0, int(ct_conf.get("max_context_chars", 0)))
        self.ct_clear_native_history = bool(ct_conf.get("clear_native_history", True))
        # 严格接管默认开启：CM 无可用记录时也显式置空 req.contexts，避免静默回退 native。
        # 通用部署可按需开启 fallback；用户个人 LM×CM 部署保持严格接管。
        self.ct_fallback_to_native_on_empty = bool(
            ct_conf.get("fallback_to_native_on_empty", False)
        )
        ct_status = ct_conf.get("llm_status_filter", ["llm_success"])
        # "no_llm" 是 UI 占位符，DB 实际值是空串 ""
        ct_status_list = list(ct_status) if ct_status else ["llm_success"]
        self.ct_llm_status_filter = ["" if s == "no_llm" else s for s in ct_status_list]
        # Kind 白名单：选中=需要；默认 ["text"]；空集合 = 不过滤（全部进入）。
        # 配置值直接对应存储的 content_kind 细粒度取值；未知/旧值（如 1.5.x 的 media）
        # 一律忽略——不做兼容映射，用户需重新勾选具体类型。
        # 类型守卫：配置被手改成非列表时回落到默认 ["text"]（否则会退化成"不过滤"）。
        include_raw = ct_conf.get("include_content_kinds", ["text"])
        if not isinstance(include_raw, list):
            include_raw = ["text"]
        self.ct_include_kinds: set[str] = {
            value
            for value in include_raw
            if isinstance(value, str) and value in _CONTENT_KIND_OPTIONS
        }
        # 1.5.0 前落库的事件行 kind 是旧值 `system_event`：勾了 `event` 就把旧值一起放行，
        # 否则历史事件在上下文里被静默排除（查询台早就兼容了这个旧值）。
        if "event" in self.ct_include_kinds:
            self.ct_include_kinds.add("system_event")
        # 事件子类（relation_data.event.type）：空 = 不按子类过滤（全部放行）
        self.ct_event_kinds: set[str] = {
            value
            for value in (ct_conf.get("event_kinds") or [])
            if isinstance(value, str) and value in _ALL_EVENT_TYPES
        } or set(_ALL_EVENT_TYPES)
        # ALL 模式：content_kind 必须 ⊆ 白名单（且非空）；False = ANY（任一交集即进）
        self.ct_include_all_match = bool(ct_conf.get("include_all_match", False))
        # 撤回消息跳过上下文：开启后被撤回的原消息与撤回事件都不进 LLM 上下文
        self.ct_recall_skip_context = bool(ct_conf.get("recall_skip_context", False))
        # persona 过滤：开启后查询严格按当前 persona_id 过滤；persona_id 为空时跳过（兜底）
        # 与 cross_session=T 协同可获完整 persona 隔离体验（切 persona + /new + 切回仍可见旧数据）
        self.ct_filter_by_persona = bool(ct_conf.get("filter_by_persona", False))
        # 工具调用上下文：接管时回放 CM 库中最近 N 个轮次的工具调用记录
        # （assistant tool_calls + role=tool），保证 LLM 跨轮看到工具返回，
        # 不重复调用工具（重复建任务、重复扣费）。0 = 不回放；负值按 0。
        self.ct_keep_tool_turns = max(0, int(ct_conf.get("keep_tool_turns", 2)))

        # 读取 AstrBot 全局时区配置（IANA 名称如 "Asia/Shanghai"），传给 DBManager
        # 做查询输出转换：存储统一 UTC naive，返回时转此 tz naive
        try:
            tz_name = context.get_config().get("timezone", "Asia/Shanghai")
            self._tz = ZoneInfo(tz_name)
        except Exception:
            self._tz = ZoneInfo("Asia/Shanghai")

        data_dir = StarTools.get_data_dir("chat_memory")
        # 多 BOT 合并存储：**本版强制双写**（旧表照旧写 + 镜像到合并存储），没有配置开关
        # ——切读门槛（双写满 7 天）与下一版的"单写"都依赖这段窗口连续。合并存储随本版
        # 首次发布，不存在"关掉双写"的线上配置，所以这里没有兼容分支。
        self.shared_store_shadow = True
        # 切读（单开关）：勾上后接管上下文与公开查询 API 都读合并存储。双写没满 7 天不生效
        # （见 _refresh_shadow_read_gate），读失败自动回退旧表（见 _read_failed）。
        self.shadow_read = bool(config.get("shadow_read", False))
        self._shadow_read_failures = 0
        # 门槛状态（initialize 里按双写窗口算）：默认未就绪 = 只写不读
        self._shadow_read_ready = False
        self._shadow_read_window_days = -1.0
        # 自己发言的平台化：发送后拉群历史（需要 OneBot 客户端支持 get_group_msg_history）
        self.sent_capture_history = bool(config.get("sent_capture_history", False))
        # (platform_id, message_type, session_id) → conversation_id：push 回显没有
        # AstrMessageEvent，拿不到 cid，只能靠入站事件维护的这张表兜
        self._conversation_ids: dict[tuple[str, str, str], str] = {}
        self._shadow_tasks: set[asyncio.Task] = set()
        # 群名现查缓存（不落库）：查询台要显示 群聊·群号（群名）
        self._group_names = GroupNameCache()
        self._sent_history_unsupported: set[str] = set()
        self._echo_subscribe_failed: set[str] = set()
        self._echo_watch_task: Optional[asyncio.Task] = None
        self._push_missing_declared = False
        self._push_subscribed_platforms: set[str] = set()
        self._echo_poll_rounds = 0
        self._echo_poll_last = ""
        self._platform_bots: dict[str, Any] = {}
        # self_id → BOT 自己的 QQ 昵称：自己贴表情/戳一戳这类事件里，事件本身的 sender
        # 只有号码，靠它才能把"谁做的"写成 DeepSeek〔Bot〕而不是一串数字。
        self._nickname_task: Optional[asyncio.Task] = None
        self._bot_nicknames: dict[str, str] = {}
        # 好友备注（user_id → 备注名）：只有 get_friend_list 能拿到，QQ 昵称里没有
        self._friend_remarks: dict[str, str] = {}
        self._friend_remarks_at: float = 0.0
        # (platform_id, session_id, user_id) → (名字, 取到时刻)：自己发言的显示名缓存
        self._member_names: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._bot_nicknames_at: float = 0.0
        # 待重试的撤回标记 (umo, mid)：通知早于消息落库时当场标不上，退避重试几次。
        self._pending_recalls: set[tuple[str, str]] = set()
        # 发送侧记账：会话 → 本轮（turn_id / cid / 关闭时刻）。push 回显靠它归属轮次，
        # 不再用"正文 + 时间窗"猜。键 = (platform_id, message_type, session_id)。
        self._active_turns: dict[tuple[str, str, str], dict[str, Any]] = {}
        # 回显订阅的归属 token（每个插件实例一份）：重载后旧实例的回调仍挂在平台上，
        # 靠它区分"我们的"与"上一个实例留下的"（见 _drop_foreign_echo_handlers）
        self._echo_owner: str = uuid.uuid4().hex
        self._sent_history_cursor: dict[tuple[str, str], int] = {}
        self.db = DBManager(data_dir, tz=self._tz, shadow=self.shared_store_shadow)

        # 媒体归档（1.3.0）：user 侧媒体落盘供 CM_UI 回看；尽力而为，不阻塞管线。
        media_conf = config.get("media_archive", {}) or {}
        self.media_archiver = MediaArchiver(
            self.db,
            data_dir,
            enabled=self._to_bool(media_conf.get("enabled"), True),
            include_video=self._to_bool(media_conf.get("include_video"), False),
            retention_days=self._to_int(media_conf.get("retention_days"), 30),
            max_total_mb=self._to_int(media_conf.get("max_total_mb"), 2048),
        )
        self._media_cleanup_task: Optional[asyncio.Task] = None

        # 只读查询台（1.4.0 并入）：initialize 阶段构建，此处占位防误用。
        self.ui_repository: Optional[ChatMemoryRepository] = None
        self.ui_web_api: Optional[ChatMemoryUiWebApi] = None

        # "reset/new 识别不出形态"的 WARNING 是否已打过（花式写法会反复命中）
        self._warned_reset_unknown = False
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_started = False

        cleanup_desc = (
            f"自动清理 {self.auto_cleanup_days} 天前的记录"
            if self.auto_cleanup_days > 0
            else "自动清理关闭"
        )
        logger.info(f"[ChatMemory] 对话记录存档已启用（{cleanup_desc}）")
        archive_desc = (
            f"媒体归档启用（保留 {self.media_archiver.retention_days} 天，"
            f"上限 {self.media_archiver.max_total_bytes // (1024 * 1024)}MB，"
            f"视频={'含' if self.media_archiver.include_video else '不含'}）"
            if self.media_archiver.enabled
            else "媒体归档关闭（仅存元信息）"
        )
        logger.info(f"[ChatMemory] {archive_desc}")

        if self.ct_enable:
            modes = []
            if self.ct_cross_session:
                modes.append("cross_session")
            if self.ct_full_group:
                modes.append("full_group")
            mode_repr = "+".join(modes) if modes else "standard"
            logger.info(
                f"[ChatMemory] 上下文接管已启用 "
                f"(mode={mode_repr}, limit={self.ct_limit_rounds}, "
                f"clear_native={self.ct_clear_native_history})"
            )

    async def initialize(self) -> None:
        """在插件加载阶段完成数据库迁移并启动后台服务。

        初始化失败必须继续向外抛出，让 AstrBot 将插件标记为加载失败；否则插件可能
        表面可用、实际从第一条消息开始持续漏存。失败前释放已创建的数据库连接。
        """
        try:
            await self.db.init_db()
            if self.shared_store_shadow:
                # 载入即订阅 + 后台巡检：不依赖消息，重载插件就能看到结果
                self._echo_watch_task = asyncio.create_task(self._echo_subscribe_loop())
                # 登录昵称：平台没就绪时这次会拿不到，订阅巡检第 1 轮会再试一次
                self._nickname_task = asyncio.create_task(
                    self._refresh_bot_nicknames(force=True)
                )
                logger.info(
                    f"{self._log_prefix()} 合并存储影子双写已启用："
                    f"旧表照旧写入，同时镜像到 chat_memory_messages / "
                    f"chat_memory_message_bots（本版强制双写；切读需双写满 "
                    f"{_SHADOW_READ_MIN_DAYS} 天）"
                )
                # 切读门槛：按双写窗口判定，并把结论写进日志（勾了但没到日子要看得出）
                await self._refresh_shadow_read_gate()
                # 启动期维护（撤回补标 / persona 回填 / 回显 target 纠正 / 孤儿回收）**放后台**：
                # 它们在大库上要几十秒到几分钟（线上 415 个撤回事件实测 4 分钟），同步 await
                # 会让插件重载一直不返回（Dashboard 升级卡在"更新中"），期间读路径还会被长事务
                # 挡住（`database is locked` → OperationalError 回退旧表）。
                self._spawn_shadow_task(self._run_startup_maintenance())
                stats = getattr(self.db, "shadow_stats", None) or {}
                if stats.get("failures") or stats.get("skipped"):
                    # 影子写失败/跳过没有 DB 统计表了，启动这一行是唯一可见口径
                    logger.warning(
                        f"{self._log_prefix()} 合并存储写入统计：写入 {stats.get('writes', 0)} / "
                        f"跳过 {stats.get('skipped', 0)} / 失败 {stats.get('failures', 0)}"
                        f"（最近一次 {stats.get('last_error') or '-'}）"
                    )
            await self._ensure_cleanup_started()
            # 媒体归档：worker 与周期清理独立于记录自动清理（auto_cleanup_days 关闭也运行）
            self.media_archiver.start()
            if self.media_archiver.enabled:
                self._media_cleanup_task = asyncio.create_task(
                    self._media_cleanup_loop()
                )
            # 只读查询台（原 chat_memory_ui，1.4.0 起并入）：失败不影响存档。
            await self._init_ui()
        except BaseException:
            # 初始化失败时 AstrBot **不会**调 terminate（StarManager.load_plugin 只记 failed
            # 并清 registry），所以这里必须自己回收：后台任务、订阅、归档 worker 一个都
            # 不能留——否则死实例的巡检会一直跑，还会把新实例的回显订阅摘掉。
            await self._cleanup_after_failed_init()
            try:
                await self.db.engine.dispose()
            except Exception as dispose_error:
                logger.warning(
                    f"{self._log_prefix()} 初始化失败后的 engine.dispose 异常: "
                    f"{dispose_error}"
                )
            raise

    async def _cleanup_after_failed_init(self) -> None:
        """初始化失败的兜底回收（尽力而为，逐项兜住异常）。"""
        for name in ("_echo_watch_task", "_cleanup_task", "_media_cleanup_task",
                     "_nickname_task"):
            task = getattr(self, name, None)
            if task is not None:
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001 - 取消失败不影响其它回收
                    pass
        for task in list(getattr(self, "_shadow_tasks", ())):
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._release_echo_subscriptions()
        except Exception as exc:  # noqa: BLE001 - 摘订阅失败只记类型
            logger.debug(
                f"{self._log_prefix()} 初始化失败后摘回显订阅异常: {type(exc).__name__}"
            )
        try:
            await self.media_archiver.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"{self._log_prefix()} 初始化失败后停归档异常: {type(exc).__name__}"
            )

    async def _run_startup_maintenance(self) -> None:
        """启动期清理（后台任务）：**只做孤儿行回收**，不扫历史。

        撤回标记靠事件到达时的退避重试（``_mark_recalled``），不再在加载时扫事件行补标；
        persona 回填与回显 target 纠正属于一次性历史修补，也已移除（新数据写入时即正确）。
        孤儿回收不是"补历史"而是清理垃圾（无参与行的消息 / 指向已删消息的参与行），
        没有外键就只能靠它定期收，实测 8 万行耗时 0.07s。
        """
        db = self.db
        try:
            orphan_msgs, orphan_parts = await db.purge_orphans()
            if orphan_msgs or orphan_parts:
                logger.info(
                    f"{self._log_prefix()} 回收合并存储孤儿行："
                    f"消息 {orphan_msgs} / 参与行 {orphan_parts}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"{self._log_prefix()} 启动维护「孤儿行回收」失败: {type(exc).__name__}"
            )

    # ── 自动清理 ─────────────────────────────────────

    async def _ensure_cleanup_started(self):
        if self._cleanup_started or self.auto_cleanup_days <= 0:
            return
        self._cleanup_started = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            f"{self._log_prefix()} 启动周期清理任务（每 24h 清理一次，"
            f"阈值 {self.auto_cleanup_days} 天）"
        )

    async def _cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(86400)
                cutoff = (
                    datetime.now(dt_timezone.utc).replace(tzinfo=None)
                    - timedelta(days=self.auto_cleanup_days)
                )
                try:
                    deleted, media_rows = await self.db.delete_old(cutoff)
                    await self.db.backfill_turn_persona()
                    await self.media_archiver.delete_files(media_rows)
                    if deleted > 0:
                        logger.info(
                            f"{self._log_prefix()} 自动清理：删除 {deleted} 条 "
                            f"早于 {self.auto_cleanup_days} 天的记录"
                        )
                    else:
                        logger.debug(f"{self._log_prefix()} 自动清理：本轮无可清理记录")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"{self._log_prefix()} 自动清理失败: {e}")
        except asyncio.CancelledError:
            pass

    async def _media_cleanup_loop(self):
        """媒体归档周期清理（每小时）：保留期 → 总量上限 → 孤儿文件。"""
        try:
            while True:
                await asyncio.sleep(3600)
                try:
                    stats = await self.media_archiver.cleanup_cycle()
                    if any(stats.values()):
                        logger.debug(
                            f"{self._log_prefix()} 媒体归档清理: {stats}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"{self._log_prefix()} 媒体归档清理失败: {e}")
        except asyncio.CancelledError:
            pass

    # ── 只读查询台（原 chat_memory_ui，1.4.0 并入）────────────

    async def _init_ui(self) -> None:
        """启用只读查询台。失败只降级（存档功能不受影响），不阻断插件加载。"""
        if not hasattr(self.context, "register_web_api"):
            logger.warning(
                f"{self._log_prefix()} 当前 AstrBot 不支持 Plugin Page Web API，"
                "查询台未启用"
            )
            return
        try:
            settings = await self.get_kv_data("ui_settings", {})
            settings = settings if isinstance(settings, dict) else {}
            database_path = self._normalize_ui_db_path(settings.get("database_path", ""))
            immutable_fallback = settings.get("immutable_fallback", True)
            if not isinstance(immutable_fallback, bool):
                immutable_fallback = True
            self.ui_repository = ChatMemoryRepository(
                database_path,
                expected_shadow_version=str(shared_store.SHADOW_SCHEMA_VERSION),
                timezone=self._tz,
                default_page_size=50,
                max_page_size=200,
                allow_immutable_fallback=immutable_fallback,
                # 只有指向插件实时库时才允许恢复软删除记录（写入口）
                writable_path=self.db.db_path,
                # 查询台是长期存活的读者：复用只读连接，把"旧历史去重"缓存在 TEMP 表里
                keep_open=True,
                # 读源必须与插件一致：只看"三表齐 + 版本对 + 满 7 天"会让默认配置下
                # 上下文读旧表、查询台已读合并存储（用户看到的两套数据不同源）。
                shadow_read_enabled=self._ui_shadow_read_allowed(),
            )
            self.ui_web_api = ChatMemoryUiWebApi(
                self.context,
                self.ui_repository,
                default_database_path=self.db.db_path,
                repository_updater=self._update_ui_repository,
                media_root=self.media_archiver.media_dir,
                export_dir=self.db.data_dir / "exports",
                group_names_loader=self.group_name_map,
                plugin_version=self._read_plugin_version(),
            )
            self.ui_web_api.register()
            # 查询台的两条缓存（旧历史去重 ~0.27s + 物化副本 ~0.57s）在后台建好：
            # 用户点开页面时就走 76ms 的快路径，不必把加载成本转成他的等待。
            self._spawn_shadow_task(asyncio.to_thread(self.ui_repository.warm))
            logger.info(f"{self._log_prefix()} 只读查询台已启用：{database_path}")
        except Exception as exc:
            logger.warning(
                f"{self._log_prefix()} 查询台启用失败（存档功能不受影响）：{exc}"
            )

    def _ui_shadow_read_allowed(self) -> bool:
        """查询台是否允许读合并存储：**与插件上下文用同一个读源判定**。

        只判"三表齐 / 版本对 / 双写满 7 天"是不够的：默认配置 ``shadow_read=false`` 时
        上下文读旧表，而查询台会自己切到合并存储——用户看到的数据与模型记住的不是一套。
        """
        return self._shadow_read_source() == "v2"

    @staticmethod
    def _read_plugin_version() -> str:
        """插件版本号（单源＝``metadata.yaml``；读不到返回空串，由 UI 显示 unknown）。"""
        try:
            import yaml

            path = Path(__file__).with_name("metadata.yaml")
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return str(data.get("version") or "")
        except Exception as exc:  # noqa: BLE001 - 版本只是展示，读不到不影响查询台
            logger.debug(
                f"[ChatMemory] 读取 metadata.yaml 版本失败: {type(exc).__name__}"
            )
            return ""

    def _normalize_ui_db_path(self, value) -> Path:
        raw = str(value or "").strip()
        if not raw:
            return self.db.db_path
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("数据库路径必须是 AstrBot 服务器上的绝对路径")
        return path.resolve(strict=False)

    async def _update_ui_repository(
        self, database_path: str, immutable_fallback: bool
    ) -> tuple:
        """查询台数据源切换：校验新库、持久化设置、替换只读仓库。"""
        normalized = self._normalize_ui_db_path(database_path)
        candidate = ChatMemoryRepository(
            normalized,
            expected_shadow_version=str(shared_store.SHADOW_SCHEMA_VERSION),
            timezone=self._tz,
            default_page_size=50,
            max_page_size=200,
            allow_immutable_fallback=immutable_fallback,
            # 切到别的库后 writable=False → 查询台的恢复按钮会被拒绝
            writable_path=self.db.db_path,
            keep_open=True,
            shadow_read_enabled=self._ui_shadow_read_allowed(),
        )
        health = await candidate.health()
        stored = "" if normalized == self.db.db_path else str(normalized)
        await self.put_kv_data(
            "ui_settings",
            {"database_path": stored, "immutable_fallback": immutable_fallback},
        )
        # 旧仓库的只读连接（含 TEMP 缓存表）必须显式关掉，否则旧库文件一直被持有
        if self.ui_repository is not None:
            self.ui_repository.close()
        self.ui_repository = candidate
        logger.info(f"{self._log_prefix()} 查询台已切换只读数据库：{candidate.db_path}")
        return candidate, health

    # ── 日志/工具辅助 ───────────────────────────────────

    @staticmethod
    def _to_bool(value, default: bool) -> bool:
        """配置布尔兜底：字符串 'false'/'0' 等不能误判为 True。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(value, (int, float)):
            return bool(value)
        return bool(default)

    @staticmethod
    def _to_int(value, default: int) -> int:
        """配置整数兜底：非数字输入回退默认值而不是抛异常。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _resolve_log_with_bot_id(config) -> bool:
        """解析日志实例前缀开关：仅读顶层配置，无旧组继承。"""
        return bool(config.get("log_with_bot_id"))

    def _log_prefix(self, event=None) -> str:
        # 全工作区统一格式：[模块名][platform:{platform_id}]（禁止自创变体）。
        # 取不到 platform_id 时只留模块名。
        if self.log_with_bot_id and event is not None:
            try:
                platform_id = event.get_platform_id()
            except Exception:
                platform_id = ""
            if platform_id:
                return f"[ChatMemory][platform:{platform_id}]"
        return "[ChatMemory]"

    def _truncate(self, text: str) -> str:
        if self.max_len <= 0:
            return text
        return text[:self.max_len]

    @staticmethod
    def _strip_reasoning_prefix(text: str) -> str:
        return _strip_reasoning_prefix_impl(text)

    @staticmethod
    def _strip_cm_xml_tags(text: str) -> str:
        return _strip_cm_xml_tags_impl(text)

    def _clean_sent_chain(self, chain) -> bool:
        """清洗发送链中泄漏的 <cm_*> 结构与模仿输出的方括号媒体占位，返回是否有改动。

        就地修改 ``chain``，落库文本随后从清洗后的链提取，两侧共用一次清洗。
        LLM 回复中字面的 [图片] 等占位替换为裸词"图片"，避免用户看到模板标记。
        """
        changed = False
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = self._strip_cm_xml_tags(comp.text)
                cleaned = _strip_bracket_media_placeholders_impl(cleaned)
                if cleaned != comp.text:
                    comp.text = cleaned
                    changed = True
        return changed

    @staticmethod
    def _extract_text(event: AstrMessageEvent) -> str:
        return _extract_text_impl(event)

    @staticmethod
    def _classify_content(
        event: AstrMessageEvent,
    ) -> tuple[list[str], Optional[str], Optional[str], Optional[str]]:
        return _classify_content_impl(event)

    @staticmethod
    def _classify_assistant_chain(chain) -> tuple[list[str], str]:
        return _classify_assistant_chain_impl(chain)

    @staticmethod
    def _build_relation_seed(event: AstrMessageEvent) -> tuple[str, Optional[dict]]:
        return _build_relation_seed_impl(event)

    @staticmethod
    def _build_relation_seed_full(
        event: AstrMessageEvent,
    ) -> tuple[str, Optional[dict], list[dict], list]:
        """user 捕获专用：额外返回 media 条目与来源引用（媒体归档用）。"""
        return _build_relation_seed_full_impl(event)

    async def _archive_media(
        self,
        event: AstrMessageEvent,
        umo: str,
        cid: str,
        turn_id: str,
        media_entries: list[dict],
        media_refs: list,
    ) -> None:
        """把 user 侧媒体交给归档器：本地源同步拷贝、base64/data 同步写、URL 入队。

        全部尽力而为：任何失败都只降级为"未归档"，不影响已完成的记录存档。
        """
        if not media_entries:
            return
        for entry, ref in zip(media_entries, media_refs):
            if not isinstance(entry, dict) or not ref:
                continue
            media_id = str(entry.get("id") or "")
            kind = str(entry.get("kind") or "")
            name = str(entry.get("name") or "")
            if not media_id:
                continue
            try:
                if ref.get("local"):
                    await self.media_archiver.archive_local(
                        media_id, ref["local"], umo, cid, turn_id, kind, name=name
                    )
                elif ref.get("base64") is not None:
                    import base64

                    data = base64.b64decode(ref["base64"])
                    await self.media_archiver.archive_bytes(
                        media_id, data, umo, cid, turn_id, kind, name=name
                    )
                elif ref.get("data"):
                    data = self._decode_data_uri(ref["data"])
                    if data:
                        await self.media_archiver.archive_bytes(
                            media_id, data, umo, cid, turn_id, kind, name=name
                        )
                elif ref.get("url"):
                    self.media_archiver.enqueue(
                        {
                            "media_id": media_id,
                            "kind": kind,
                            "url": ref["url"],
                            "umo": umo,
                            "conversation_id": cid,
                            "turn_id": turn_id,
                            "name": name,
                        }
                    )
            except Exception as exc:
                logger.debug(
                    f"{self._log_prefix(event)} 媒体归档提交失败 "
                    f"kind={kind} id={media_id}: {exc}"
                )

    @staticmethod
    def _decode_data_uri(uri: str) -> Optional[bytes]:
        """解析 data:[<mime>][;base64],<payload>；失败返回 None。"""
        try:
            header, _, payload = uri.partition(",")
            if not payload or not header.startswith("data:"):
                return None
            import base64

            if header.endswith(";base64"):
                return base64.b64decode(payload)
            return payload.encode("utf-8")
        except Exception:
            return None

    @staticmethod
    def _content_placeholder(kind: list[str]) -> str:
        return _content_placeholder_impl(kind)

    @staticmethod
    def _is_notice_derived_event(event: AstrMessageEvent) -> bool:
        """chain 为空且原始 JSON 是 OneBot notice/request 的事件。

        AstrBot 把 notice/request 转成空消息（sender 取 notice.user_id，即被操作
        对象而非操作者），因此不能按 sender 做 BOT 自身跳过。
        """
        try:
            if event.get_messages():
                return False
        except Exception:
            return False
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        return isinstance(raw, dict) and str(raw.get("post_type") or "") in (
            "notice",
            "request",
        )

    @staticmethod
    def _system_event_summary_impl(event: AstrMessageEvent) -> str:
        return _system_event_summary_fn(event)

    @staticmethod
    def _system_event_parts_impl(event: AstrMessageEvent):
        """(summary, action, detail, target_id, operator_id, event_type) 或 None。"""
        return _system_event_parts_fn(event)

    async def _mark_recalled(self, umo: str, message_id: str) -> None:
        """给被撤回的消息打 ``recalled_at``；当场没命中就退避重试，耗尽才放弃。

        为什么需要重试：撤回通知常**早于**目标消息落库（平台侧两个事件、或写入还在排队），
        当场 `UPDATE … WHERE message_id = ?` 命中 0 行。以前这种情况只能等下次插件加载
        时扫事件行补标（要扫 3 万行、还要长事务），现在先在原地退避几次（0.5/1.5/3/8 秒，
        覆盖全部实测场景），真正没落库的才交给启动补标兜底。
        """
        mid = str(message_id or "").strip()
        if not (umo and mid):
            return
        try:
            if await self.db.mark_recalled(umo, mid) > 0:
                return
        except Exception as exc:  # noqa: BLE001 - 标记失败不影响存档
            logger.warning(f"{self._log_prefix()} 撤回标记失败: {type(exc).__name__}")
            return
        self._schedule_recall_retry(umo, mid)

    def _schedule_recall_retry(self, umo: str, mid: str) -> None:
        """把"还没落库的撤回目标"挂到退避重试队列（同一目标只挂一条链）。"""
        key = (umo, mid)
        if key in self._pending_recalls:
            return
        if len(self._pending_recalls) >= _RECALL_PENDING_MAX:
            logger.warning(
                f"{self._log_prefix()} 撤回待重试已达上限 {_RECALL_PENDING_MAX}，"
                f"放弃本次补标（目标 mid={mid}；下次加载的启动补标仍会兜底）"
            )
            return
        self._pending_recalls.add(key)
        self._spawn_shadow_task(self._retry_recall(umo, mid))

    async def _retry_recall(self, umo: str, mid: str) -> None:
        """按 ``_RECALL_RETRY_DELAYS`` 退避重试撤回标记，命中即停。"""
        key = (umo, mid)
        try:
            for attempt, delay in enumerate(_RECALL_RETRY_DELAYS, start=1):
                await asyncio.sleep(delay)
                try:
                    if await self.db.mark_recalled(umo, mid) > 0:
                        logger.debug(
                            f"{self._log_prefix()} 撤回补标命中（第 {attempt} 次退避后，"
                            f"mid={mid}）"
                        )
                        return
                except Exception as exc:  # noqa: BLE001 - 单次失败继续下一次
                    logger.debug(
                        f"{self._log_prefix()} 撤回补标第 {attempt} 次失败: "
                        f"{type(exc).__name__}"
                    )
            logger.warning(
                f"{self._log_prefix()} 撤回目标 mid={mid} 在 "
                f"{sum(_RECALL_RETRY_DELAYS):.1f}s 内仍未落库，放弃补标"
                f"（下次加载的启动补标仍会兜底）"
            )
        finally:
            self._pending_recalls.discard(key)

    async def _recall_snippet(self, umo: str, event: AstrMessageEvent) -> str:
        """撤回 notice：用 message_id 本地关联被撤回的 user 记录，返回可读摘要。

        失败/未命中/原文为空时返回空串（调用方回退为纯 "[撤回消息]"）。
        """
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return ""
        message_id = str(raw.get("message_id") or "").strip()
        if not message_id:
            return ""
        try:
            row = await self.db.find_by_message_id(umo, message_id)
        except Exception:
            return ""
        if not row:
            return ""
        text = _AT_TOKEN_RE.sub("", str(row.get("content") or ""))
        text = _MEDIA_TOKEN_RE.sub("", text)
        text = " ".join(text.split())
        if not text:
            return ""
        nickname = str(row.get("sender_nickname") or "").strip()
        head = f"{nickname}：" if nickname else ""
        return f"[撤回消息] {_escape_plain_text(head + text)}"

    async def _resolve_event_target(self, umo: str, user_id: str) -> str:
        """system_event 的目标昵称：按 umo+user_id 查最近一条真实昵称。

        排除适配器兜底写入的 QQ 号（纯数字）与空值——账号绝不进上下文；
        失败/未命中返回空串（<cm_event> 不带 target 属性）。
        """
        if not umo or not user_id:
            return ""
        bot_name = self._bot_nickname(user_id)
        if bot_name:
            return bot_name
        try:
            nickname = await self.db.latest_sender_nickname(umo, user_id)
        except Exception:
            return ""
        nickname = str(nickname or "").strip()
        if not nickname or nickname.isdigit():
            return ""
        return nickname

    async def _reaction_target_nickname(self, umo: str, message_id: str) -> str:
        """表情回应 notice：按 message_id 查被回应消息的作者昵称；无则空串。

        与 _resolve_event_target 同规则：纯数字（QQ 号兜底）不进上下文。
        """
        if not umo or not message_id:
            return ""
        try:
            row = await self.db.find_by_message_id(umo, message_id)
        except Exception:
            return ""
        if not row:
            return ""
        nickname = str(row.get("sender_nickname") or "").strip()
        if not nickname or nickname.isdigit():
            return ""
        return nickname

    async def _annotate_poke_targets(self, relation_data: dict, umo: str) -> None:
        """poke 条目补 target 昵称（name 字段）：上下文渲染 <cm_event type="poke" target="…"/>。

        失败/未命中静默：无 name 时渲染无属性形态，账号绝不进上下文。
        """
        media = relation_data.get("media")
        if not isinstance(media, list):
            return
        for item in media:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "") != "poke" or item.get("name"):
                continue
            nickname = await self._resolve_event_target(umo, str(item.get("id") or ""))
            if nickname:
                item["name"] = nickname

    async def _get_curr_cid(self, umo: str) -> str:
        try:
            conv_mgr = self.context.conversation_manager
            return await conv_mgr.get_curr_conversation_id(umo) or ""
        except Exception:
            return ""

    async def _get_curr_persona(self, umo: str, cid: Optional[str] = None) -> str:
        """取当前 conversation 的 persona_id。cid 未提供则先查 curr cid。
        失败或无 persona 返回空串（filter_by_persona 兜底：空串不过滤）。"""
        try:
            conv_mgr = self.context.conversation_manager
            if not cid:
                cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await conv_mgr.get_conversation(umo, cid)
            return getattr(conv, "persona_id", "") or ""
        except Exception:
            return ""

    async def _get_effective_persona(self, umo: str, event: AstrMessageEvent,
                                     cid: Optional[str] = None) -> str:
        """通过 resolve_selected_persona 获取当前实际生效的 persona_id。

        与 _ensure_persona_and_skills 同源，保证 CM 记录/过滤的 persona 与 LLM
        实际使用的 persona 一致。优先级：session 规则 > conversation.persona_id > config 默认。
        None / '[%None]' / 异常一律返回空串（兜底跳过过滤）。
        """
        try:
            conv_persona_id = await self._get_curr_persona(umo, cid) or None
            # provider_settings 是 resolve_selected_persona 的废参数（函数体不读），不再传。
            resolved, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name=event.get_platform_name(),
            )
            if resolved and resolved != "[%None]":
                return resolved
            return ""
        except Exception:
            return ""

    @staticmethod
    def _get_message_id(event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""
        return getattr(msg_obj, "message_id", "") or ""

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str]:
        """拆 ``platform_id:MessageType:session_id`` 三段。"""
        if not umo:
            return "", "", ""
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return "", "", ""
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _get_group_id(event: AstrMessageEvent) -> str:
        try:
            return event.get_group_id() or ""
        except Exception:
            return ""

    async def _refresh_bot_nicknames(self, *, force: bool = False) -> dict[str, str]:
        """取各实例登录账号的 QQ 昵称（``get_login_info``），进程内缓存 6 小时。

        自己的动作（主动贴表情、主动戳一戳）在平台事件里只有 self_id，没有昵称；
        不补这一步，查询台里这些记录只能显示成一串号码，按发言者根本筛不到。
        """
        now = time.time()
        cached = getattr(self, "_bot_nicknames", None) or {}
        cached_at = getattr(self, "_bot_nicknames_at", 0.0)
        if not force and cached and now - cached_at < 21600:
            return dict(cached)
        # 按 uid 合并：某个实例这次取不到时，不要把它上次拿到的昵称一起丢掉
        names: dict[str, str] = dict(cached)
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception:  # noqa: BLE001 - 平台没就绪就等下轮
            return dict(self._bot_nicknames)
        for inst in insts:
            bot, _ = self._find_onebot_client(inst)
            call = getattr(bot, "call_action", None)
            if not callable(call):
                continue
            try:
                # 硬超时：适配器的 api_timeout 是 180s，而这里是巡检主循环，
                # 平台连上但未就绪时不能把整个回显订阅巡检卡住 3 分钟。
                info = await asyncio.wait_for(call("get_login_info"), timeout=5)
            except Exception:  # noqa: BLE001 - 超时/失败都不影响别的实例
                continue
            data = info.get("data") if isinstance(info, dict) else None
            payload = data if isinstance(data, dict) else info
            if not isinstance(payload, dict):
                continue
            uid = str(payload.get("user_id") or "")
            nick = str(payload.get("nickname") or "").strip()
            if uid and nick:
                names[uid] = nick
        if names:
            self._bot_nicknames = names
            self._bot_nicknames_at = now
        try:
            await self._refresh_friend_remarks()
        except Exception:  # noqa: BLE001 - 备注拿不到不影响昵称
            pass
        return dict(self._bot_nicknames)

    async def _resolve_member_nickname(
        self, platform_id: str, message_type: str, session_id: str, user_id: str
    ) -> str:
        """查一次"这个账号在这个会话里叫什么"：群聊取群名片，退回 QQ 昵称。

        为什么不靠 `lookup_sender_nickname`：它只能抄"别的实例观察过这个人"的行。
        BOT 单独在一个群里没人观测它时永远抄不到，昵称就会一直是 QQ 号；而
        `get_group_member_info` 不依赖任何人发言，随时能拿到名片。
        """
        key = (str(platform_id), str(session_id), str(user_id))
        names = getattr(self, "_member_names", None)
        if names is None:
            names = self._member_names = {}
        cached = names.get(key)
        now = time.time()
        if cached and now - cached[1] < _MEMBER_NAME_TTL_SEC:
            return cached[0]
        if not (str(session_id).isdigit() and str(user_id).isdigit()):
            return ""
        try:
            inst = self.context.get_platform_inst(platform_id)
        except Exception:  # noqa: BLE001 - 平台没就绪就等下次
            return ""
        if inst is None:
            return ""
        bot, _ = self._find_onebot_client(inst)
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return ""
        payload: dict = {}
        try:
            if message_type == "GroupMessage":
                reply = await asyncio.wait_for(
                    call("get_group_member_info", group_id=int(session_id),
                         user_id=int(user_id)),
                    timeout=5,
                )
            else:
                reply = await asyncio.wait_for(
                    call("get_stranger_info", user_id=int(user_id)), timeout=5
                )
            data = reply.get("data") if isinstance(reply, dict) else None
            if isinstance(data, dict):
                payload = data
        except Exception:  # noqa: BLE001 - 查不到就留空，绝不影响写路径
            return ""
        # 取回来的两个名字按会话类型排序：群聊看群名片，私聊看备注（备注在好友列表里）。
        # 私聊绝不采用 card——群名片只属于群维度。
        card = str(payload.get("card") or "").strip()
        nick = str(payload.get("nickname") or "").strip()
        if message_type == "GroupMessage":
            name = card or nick
        else:
            name = self._friend_remark(user_id) or nick
        logger.debug(
            f"{self._log_prefix()} 成员名解析 platform={platform_id} "
            f"session={session_id} uid={user_id} card={card!r} nickname={nick!r}"
        )
        if not name or name.isdigit():
            return ""
        # 容量上限：长期运行下 (platform, session, user) 会一直累积；超了就清过期项，
        # 全都没过期就整体清空（缓存而已，重建只需一次接口调用）
        if len(names) >= _MEMBER_NAME_CACHE_MAX:
            expired = [k for k, (_, ts) in names.items()
                       if now - ts >= _MEMBER_NAME_TTL_SEC]
            for stale in expired:
                names.pop(stale, None)
            if len(names) >= _MEMBER_NAME_CACHE_MAX:
                names.clear()
        names[key] = (name, now)
        return name

    async def _refresh_friend_remarks(self, *, force: bool = False) -> dict[str, str]:
        """拉一次好友列表取备注（6 小时缓存）。

        OB11 的消息事件里没有备注字段，只有 `get_friend_list` 返回 `remark`
        （没设备注时它等于 nickname，所以相等就当没有备注）。
        """
        now = time.time()
        cached = getattr(self, "_friend_remarks", None) or {}
        if not force and cached and now - getattr(self, "_friend_remarks_at", 0.0) < 21600:
            return dict(cached)
        remarks: dict[str, str] = dict(cached)
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception:  # noqa: BLE001 - 平台没就绪就等下次
            return dict(remarks)
        for inst in insts:
            bot, _ = self._find_onebot_client(inst)
            call = getattr(bot, "call_action", None)
            if not callable(call):
                continue
            try:
                reply = await asyncio.wait_for(call("get_friend_list"), timeout=8)
            except Exception:  # noqa: BLE001 - 单个实例失败不影响别的
                continue
            data = reply.get("data") if isinstance(reply, dict) else None
            items = data if isinstance(data, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("user_id") or "")
                remark = str(item.get("remark") or "").strip()
                nick = str(item.get("nickname") or "").strip()
                if uid and remark and remark != nick and not remark.isdigit():
                    remarks[uid] = remark
        if remarks:
            self._friend_remarks = remarks
            self._friend_remarks_at = now
        return dict(remarks)

    def _friend_remark(self, user_id: str) -> str:
        """好友备注（没设过返回空串）。"""
        return (getattr(self, "_friend_remarks", None) or {}).get(str(user_id or ""), "")

    def _bot_nickname(self, self_id: str) -> str:
        """缓存的登录昵称（没有就返回空串，绝不返回号码）。"""
        names = getattr(self, "_bot_nicknames", None) or {}
        return names.get(str(self_id or ""), "")

    @staticmethod
    def _get_sender_nickname(event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except Exception:
            return ""

    @staticmethod
    def _get_self_id(event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""
        return getattr(msg_obj, "self_id", "") or ""

    @staticmethod
    def _get_platform_name(event: AstrMessageEvent) -> str:
        try:
            return event.get_platform_name() or ""
        except Exception:
            return ""

    @staticmethod
    def _get_raw_timestamp(event: AstrMessageEvent) -> Optional[int]:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return None
        ts = getattr(msg_obj, "timestamp", None)
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts)
        return None

    @staticmethod
    def _get_platform_timestamp(event: AstrMessageEvent) -> Optional[int]:
        """平台消息自带时间（OneBot v11 事件的 ``time``）。

        合并存储的归一键必须用它：AstrBot 的 ``AstrMessageObj.timestamp`` 是
        ``int(time.time())``（本地接收时刻），同一进程里多个实例可能落在不同秒
        （实测 1.4% 的多实例消息如此），会让同一条消息拆成两条物理消息。
        取不到（非 OneBot 或字段缺失）返回 None，由影子层回落接收时刻。
        """
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
        if raw is None:
            return None
        value = None
        if isinstance(raw, dict):
            value = raw.get("time")
        if value is None:
            try:
                value = raw["time"]
            except Exception:
                value = getattr(raw, "time", None)
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    @staticmethod
    def _get_platform_uid(event: AstrMessageEvent) -> Optional[str]:
        """平台侧共享的消息 id（各实例相同）。

        NapCat 的 OB11 消息事件里 ``message_id`` / ``message_seq`` / ``real_id``
        三个字段都等于本地短 id（= md5(msgId|chatType|peerUid) 截断，每实例一套），
        只有 ``real_seq``（= 服务器 ``msgSeq``）跨实例一致，故取它。
        其它客户端/旧版本没有该字段时返回 None，归一退化到内容指纹。
        """
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
        if raw is None:
            return None
        value = None
        if isinstance(raw, dict):
            value = raw.get("real_seq")
        if value is None:
            try:
                value = raw["real_seq"]
            except Exception:
                value = getattr(raw, "real_seq", None)
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _get_post_type(event: AstrMessageEvent) -> str:
        """原始事件的 post_type（message / message_sent / notice…）。

        ``message_sent`` 表示这是平台回显的自己消息（NapCat 需开启 reportSelfMessage）。
        """
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
        value = None
        if isinstance(raw, dict):
            value = raw.get("post_type")
        if value is None:
            try:
                value = raw["post_type"]
            except Exception:
                value = getattr(raw, "post_type", None)
        return str(value or "").strip()

    @staticmethod
    def _get_message_sent_type(event: AstrMessageEvent) -> str:
        """部分客户端另给 ``message_sent_type``（NapCat 自发消息为 ``self``）。"""
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
        value = None
        if isinstance(raw, dict):
            value = raw.get("message_sent_type")
        if value is None:
            try:
                value = raw["message_sent_type"]
            except Exception:
                value = getattr(raw, "message_sent_type", None)
        return str(value or "").strip()

    @classmethod
    def _is_self_echo_event(cls, event: AstrMessageEvent) -> bool:
        """是否平台回显的自己消息。

        NapCat 的日志实测：自发消息带 ``post_type=message_sent`` **且**
        ``message_sent_type=self``；不同客户端可能只给其中一个，故两者都认。
        notice 派生的事件仍走 system_event 归档，不受此判定影响。
        """
        return (
            cls._get_post_type(event) == "message_sent"
            or cls._get_message_sent_type(event) == "self"
        )

    async def _capture_self_echo(self, event: AstrMessageEvent) -> bool:
        """回显入库（影子专用）：role=assistant + 平台 uid，与观察侧同一 msg_uid。

        与 push 总线那条通道**同一套归属规则**：轮次/说话对象都取发送侧记账，不能拿
        "事件的发送者"（＝BOT 自己）当 target，否则这一段在"当前用户"范围里查不到。
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        self_id = self._get_self_id(event)
        if not umo or not self_id:
            return False
        audit = self._collect_audit_fields(event)
        resolved = await self._resolve_member_nickname(
            str(audit.get("platform_id") or ""),
            str(audit.get("message_type") or ""),
            str(audit.get("session_id") or ""),
            str(self_id),
        )
        if resolved:
            audit["sender_nickname"] = resolved
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        claimed = self._claim_turn_for_echo(audit)
        if claimed.get("cid"):
            cid = str(claimed["cid"])
        if not cid:
            return False
        content = (getattr(event, "message_str", "") or "").strip()
        kind, at_id, reply_id, forward_id = self._classify_content(event)
        kind, content = _normalize_echo_kind(kind, content)
        ok = await self._safe_insert_shadow_only(
            umo, cid, claimed.get("target_id") or self_id, "assistant", content,
            message_id=self._get_message_id(event) or None,
            llm_status=claimed.get("llm_status") or "", content_kind=kind,
            at_id=at_id, reply_id=reply_id, forward_id=forward_id,
                turn_id=claimed.get("turn_id") or None,
            claim_turn=bool(claimed.get("turn_id")),
            persona_id=claimed.get("persona_id") or None,
            send_status=_SEND_ATTEMPTED,
            **audit,
        )
        if ok:
            logger.debug(
                f"{self._log_prefix(event)} 回显入库：own message "
                f"{len(content)} 字（仅影子表）"
            )
        return ok

    def _collect_audit_fields(self, event: AstrMessageEvent) -> dict:
        """从 event 提取审计/上下文字段，供 INSERT 使用。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        platform_id, message_type, session_id = self._parse_umo(umo)
        self_id = self._get_self_id(event)
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001 - 拿不到发送者就按原样
            sender_id = ""
        # 从原始事件里分别取「群名片」和「QQ 昵称」：适配器给的 get_sender_name()
        # 是 card or nickname 的结果，分不出两者，而两种会话要的优先级不同。
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        sender = raw.get("sender") if isinstance(raw, dict) else None
        card = str((sender or {}).get("card") or "").strip()
        qq_nick = str((sender or {}).get("nickname") or "").strip()
        fallback = self._get_sender_nickname(event)
        if message_type == "GroupMessage":
            # 群聊：群里看到的名字（群名片）优先，其次 QQ 昵称
            nickname = card or qq_nick or fallback
        else:
            # 私聊（含临时会话）：备注 → QQ 昵称 → 号码。
            # **不取群名片**：那是群维度概念，出现在私聊里会让两类会话混淆。
            nickname = self._friend_remark(sender_id) or qq_nick
            if not nickname and fallback and fallback.isdigit():
                nickname = fallback
        # 自己的动作/通知事件可能完全没有名字：补登录昵称，最后才是号码。
        if self_id and sender_id == str(self_id) and (
            not nickname or nickname.isdigit()
        ):
            nickname = self._bot_nickname(self_id) or nickname
        return {
            "platform_id": platform_id,
            "platform_name": self._get_platform_name(event),
            "message_type": message_type,
            "session_id": session_id,
            "self_id": self_id,
            "group_id": self._get_group_id(event),
            "sender_nickname": nickname,
            "raw_timestamp": self._get_raw_timestamp(event),
            "platform_timestamp": self._get_platform_timestamp(event),
            "platform_uid": self._get_platform_uid(event),
            "post_type": self._get_post_type(event),
        }

    # NapCat 查不到临时会话来源群时写的哨兵值（不是真实群号）
    _TEMP_GROUP_SENTINEL = "284840486"

    def _attach_temp_session(self, relation_data: dict, event) -> None:
        """临时会话（非好友私聊）把来源群记下来；好友私聊不写。

        NapCat 在私聊事件里带 ``group_id``/``sub_type``/``temp_source``，
        AstrBot 适配器不会搬到 ``AstrBotMessage.group_id``，只能从原始事件读。
        用途暂不消费：留作"这条私聊是从哪个群点进来的"的溯源依据。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return
        if str(raw.get("sub_type") or "") != "group":
            return
        group_id = str(raw.get("group_id") or "").strip()
        if not group_id or group_id == self._TEMP_GROUP_SENTINEL:
            return
        relation_data["sub_type"] = "group"
        relation_data["temp_group_id"] = group_id
        try:
            relation_data["temp_source"] = int(raw.get("temp_source") or 0)
        except (TypeError, ValueError):
            relation_data["temp_source"] = 0

    # ── 用户消息捕获（核心逻辑，可被多个钩子复用）──────

    async def _capture_user_internal(self, event: AstrMessageEvent) -> bool:
        """捕获 user 消息立即落库。返回 True 表示成功（或已捕获过）。

        幂等：通过 ``chat_memory_captured`` extra 防重复。
        ``chat_memory_capture_attempted`` 仅在"真正尝试过写库"的路径才设：
        - cron / bot 自身 / umo 空：跳过且**不**标 attempted → capture_bot 走 proactive
        - cid 暂未就绪 / 内容空 / 写库失败：标 attempted + 未标 captured → orphan
        """
        if event.get_extra("chat_memory_captured"):
            return True

        umo = getattr(event, "unified_msg_origin", "")
        user_id = event.get_sender_id() or ""
        if not umo or not user_id:
            logger.debug(f"{self._log_prefix(event)} 跳过 user 捕获：umo 或 user_id 为空")
            return False

        try:
            if user_id == event.get_self_id():
                # notice/request 转成的空消息里，sender 取的是 notice.user_id
                # （被操作对象）——自己被禁言/被踢等事件 sender 就是 BOT 自己。
                # 这类事件应继续走 system_event 归档，BOT 自身守卫只拦真正的 bot 发言。
                if self._is_self_echo_event(event):
                    # 平台回显（post_type=message_sent）：这是"我发出的那条平台消息"的
                    # 权威事实，与其它 BOT 的观察用同一个 real_seq → 影子层自动并成同一行。
                    # 只镜像影子表（旧表已有发送路径的 assistant 行），随后停止事件，
                    # 避免自己的消息继续走进 LLM 阶段形成自回复循环。
                    await self._capture_self_echo(event)
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                    return True
                if not self._is_notice_derived_event(event):
                    logger.debug(
                        f"{self._log_prefix(event)} 跳过 user 捕获：BOT 自身消息"
                    )
                    return False
        except Exception:
            pass

        # cron 平台：跳过 user capture 且不标 attempted → capture_bot 走 proactive 分支
        if self._get_platform_name(event) == "cron":
            logger.debug(f"{self._log_prefix(event)} cron 平台，跳过 user 捕获（assistant 将标 proactive）")
            return False

        # 以下路径都是"真正尝试过 capture"：cid 未就绪、内容空、写库失败都属 orphan
        if not event.get_extra("chat_memory_capture_attempted"):
            event.set_extra("chat_memory_capture_attempted", True)

        cid = await self._get_curr_cid(umo)
        if not cid:
            logger.debug(f"{self._log_prefix(event)} 跳过 user 捕获：cid 暂未创建（首条消息可能漏存）")
            return False

        kind, at_id, reply_id, forward_id = self._classify_content(event)
        user_template, relation_data, media_entries, media_refs = (
            self._build_relation_seed_full(event)
        )
        if "poke" in kind:
            # 戳一戳并入事件协议（1.5.0）：kind 归一为 event，event 结构随
            # relation 落库；target 取 poke 条目已解析的昵称，operator 取发言者。
            if relation_data:
                await self._annotate_poke_targets(relation_data, umo)
                poke_name = ""
                for item in relation_data.get("media") or []:
                    if (isinstance(item, dict)
                            and str(item.get("kind") or "") == "poke"):
                        poke_name = str(item.get("name") or "").strip()
                        break
            else:
                poke_name = ""
            sender_nick = str(self._get_sender_nickname(event) or "").strip()
            if sender_nick.isdigit():
                sender_nick = ""
            if not isinstance(relation_data, dict):
                relation_data = {"v": 1, "mentions": [], "media": [], "reply": None}
            relation_data["event"] = {
                "type": "poke",
                "action": "戳一戳",
                "detail": "",
                "target": poke_name,
                "operator": sender_nick,
            }
            kind = [_K_SYSTEM]
            poke_event = True
        else:
            poke_event = False
        user_text = user_template or self._extract_text(event)

        # content 决定：有文本用文本；否则用占位；empty（[] + 无引用字段）用空串。
        # 事件优先使用 notice/request 事件的可读摘要（如 [撤回消息]），
        # kind 保持 event，不写成 text 类型。
        if user_text:
            content = user_text
        elif kind == [_K_SYSTEM] and poke_event:
            # 戳一戳事件：无 notice 摘要，UI 显示 [戳一戳]
            content = "[戳一戳]"
        elif kind == [_K_SYSTEM]:
            summary = self._system_event_summary_impl(event) or self._content_placeholder(kind)
            parts = self._system_event_parts_impl(event)
            action = parts[1] if parts else ""
            detail = parts[2] if parts else ""
            target_uid = parts[3] if parts else ""
            operator_uid = parts[4] if parts else ""
            event_type = parts[5] if parts else "system"
            content = summary
            if content == "[撤回消息]":
                snippet = await self._recall_snippet(umo, event)
                if snippet:
                    content = snippet
                    action = "撤回消息"
                    detail = snippet[len("[撤回消息] ") :]
                    if "：" in detail:
                        detail = detail.split("：", 1)[1]
            if content in ("[系统事件]", "[系统通知]"):
                # 未知 notice 类型（1.5.0）：兜底已落库（type=system）；每个
                # (post_type, notice_type, sub_type) 组合首次出现打 WARNING，
                # 带**完整**原始事件 JSON（可能含账号/群号；按用户要求日志不截断，
                # 仅本地诊断、不入库不进上下文），重复出现降为 DEBUG 防刷屏。
                raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
                if isinstance(raw, dict):
                    type_key = (
                        str(raw.get("post_type") or ""),
                        str(raw.get("notice_type") or ""),
                        str(raw.get("sub_type") or ""),
                    )
                    first_seen = type_key not in self._warned_unknown_notices
                    if first_seen:
                        self._warned_unknown_notices.add(type_key)
                    # 首次出现：WARNING 只报类型（原始负载会带账号/群号，不能进 WARNING）；
                    # 完整 raw 一律走 DEBUG，默认不输出。
                    log_fn = logger.warning if first_seen else logger.debug
                    log_fn(
                        f"{self._log_prefix(event)} 未知 notice 类型: "
                        f"post_type={raw.get('post_type')!r} "
                        f"notice_type={raw.get('notice_type')!r} "
                        f"sub_type={raw.get('sub_type')!r}"
                    )
                    logger.debug(
                        f"{self._log_prefix(event)} 未知 notice 原始负载: {str(raw)!r}"
                    )
            if action:
                # <cm_event> 结构：target=被操作对象、operator=操作者（仅部分
                # 事件平台提供），昵称按 umo+user_id 本地解析，QQ 号不进上下文。
                if not isinstance(relation_data, dict):
                    relation_data = {"v": 1, "mentions": [], "media": [], "reply": None}
                # target 与 operator 按事件类型分别解析：只有操作者的事件
                # （上传/好友添加/请求）target_uid 为空，只解析 operator。
                target = await self._resolve_event_target(umo, target_uid)
                operator = ""
                if operator_uid and operator_uid != target_uid:
                    operator = await self._resolve_event_target(umo, operator_uid)
                event_data = {
                    "action": action,
                    "detail": detail or "",
                    "target": target or "",
                    "operator": operator or "",
                    "type": event_type,
                }
                if action == "撤回消息":
                    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
                    if isinstance(raw, dict):
                        recalled_mid = str(raw.get("message_id") or "")
                        event_data["message_id"] = recalled_mid
                        # 标记原消息（与配置无关，始终落库；是否进上下文由配置决定）
                        await self._mark_recalled(umo, recalled_mid)
                if action == "群名片变更":
                    # 名片变了：缓存里的旧名字立刻作废（否则要等 TTL 到点才跟上）
                    platform_id, _, session_id = self._parse_umo(umo)
                    subject = str(target_uid or operator_uid or "")
                    getattr(self, "_member_names", {}).pop(
                        (platform_id, session_id, subject), None
                    )
                if event_type == "reaction":
                    # 表情回应：按 message_id 关联被回应消息的作者昵称（同撤回）；
                    # 附带 emoji_id 供查询台查看。
                    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
                    if isinstance(raw, dict):
                        target_msg_id = str(raw.get("message_id") or "")
                        event_data["message_id"] = target_msg_id
                        likes = raw.get("likes")
                        if (isinstance(likes, list) and likes
                                and isinstance(likes[0], dict)):
                            event_data["emoji_id"] = str(
                                likes[0].get("emoji_id") or ""
                            )
                        author = await self._reaction_target_nickname(
                            umo, target_msg_id
                        )
                        if author:
                            event_data["target"] = author
                relation_data["event"] = event_data
        elif kind:
            content = self._content_placeholder(kind)
        else:
            content = ""

        # empty 且无任何引用字段：什么都没存，跳过
        if not content and at_id is None and reply_id is None and forward_id is None:
            logger.debug(f"{self._log_prefix(event)} 跳过 user 捕获：消息完全为空")
            return False

        msg_id = self._get_message_id(event)
        no_mid = not msg_id

        # 取当前生效 persona_id 缓存到 extras，capture_bot 复用避免二次查询
        persona_id = await self._get_effective_persona(umo, event, cid)
        event.set_extra("chat_memory_persona_id", persona_id)
        # 内部 turn_id 不依赖平台 message_id；无 mid 平台也能建立 user/assistant 配对。
        turn_id = event.get_extra("chat_memory_turn_id") or uuid.uuid4().hex
        event.set_extra("chat_memory_turn_id", turn_id)

        if isinstance(relation_data, dict):
            self._attach_temp_session(relation_data, event)
        audit = self._collect_audit_fields(event)
        # 事件（贴表情/戳一戳/名片变更…）平台只给账号：优先查该会话里的群名片，
        # 查不到才用登录昵称/空值。这样事件行显示的也是"群里看到的名字"。
        own = bool(audit.get("self_id")) and str(user_id) == str(audit.get("self_id"))
        current_nick = str(audit.get("sender_nickname") or "")
        # 空、或"昵称就是号码"都要再查一次：号码只是最后一档，不是终点
        if not current_nick or current_nick.isdigit() or own:
            resolved = await self._resolve_member_nickname(
                str(audit.get("platform_id") or ""),
                str(audit.get("message_type") or ""),
                str(audit.get("session_id") or ""),
                str(user_id or ""),
            )
            if resolved:
                audit["sender_nickname"] = resolved
        if relation_data and relation_data.get("reply"):
            relation_data["reply"] = await self._resolve_reply_relation(
                umo=umo,
                platform_id=str(audit.get("platform_id") or ""),
                self_id=str(audit.get("self_id") or ""),
                reply_seed=relation_data["reply"],
            )
        ok = await self._safe_insert(
            umo, cid, user_id, "user", truncate_content_template(content, self.max_len),
            message_id=msg_id or None, pair_id=None,
            llm_status=_LLM_DEFAULT, content_kind=kind,
            at_id=at_id, reply_id=reply_id, forward_id=forward_id,
            persona_id=persona_id or None,
            turn_id=turn_id,
            relation_data=dump_relation_data(relation_data),
            **audit,
        )
        if not ok:
            logger.warning(f"{self._log_prefix(event)} user 写入失败，extras 未标记，assistant 将标 orphan")
            return False

        # 媒体归档：本地源同步接管（毫秒级）+ 远程源后台下载，失败不影响存档。
        await self._archive_media(event, umo, cid, turn_id, media_entries, media_refs)

        kind_repr = "/".join(kind) if kind else "empty"
        ref_repr = []
        if at_id: ref_repr.append(f"at={at_id}")
        if reply_id: ref_repr.append(f"reply={reply_id}")
        if forward_id: ref_repr.append(f"fwd={forward_id}")
        ref_str = f"[{','.join(ref_repr)}]" if ref_repr else ""
        logger.debug(
            f"{self._log_prefix(event)} user[{msg_id or '-'}][{kind_repr}]{ref_str} -> "
            f"{user_id}@{cid}: {content}"
        )

        event.set_extra("chat_memory_captured", True)
        event.set_extra("chat_memory_cid", cid)
        event.set_extra("chat_memory_user_msg_id", msg_id)
        event.set_extra("chat_memory_llm_triggered", False)
        event.set_extra("chat_memory_no_mid", no_mid)
        # push 回显没有 AstrMessageEvent，cid 只能靠这张内存表带过去；入站事件是唯一
        # 能确定"这个会话当前 cid"的地方（track_platform_context 早于捕获，读不到）。
        self._remember_cid(umo, str(cid), self._get_group_id(event))
        return True

    async def _resolve_reply_relation(
        self,
        umo: str,
        platform_id: str,
        self_id: str,
        reply_seed: dict,
    ) -> dict:
        """精确 user turn 优先；Bot、缺失或歧义目标统一保存最小快照。"""
        target_user_id = str(reply_seed.get("target_user_id") or "").strip()
        source_id = str(reply_seed.get("source_id") or "").strip()
        is_bot_reply = bool(target_user_id and self_id and target_user_id == self_id)
        if not is_bot_reply and source_id:
            target = await self.db.resolve_user_reply_target(umo, platform_id, source_id)
            if target:
                return {
                    "resolution": "turn",
                    "target_turn_id": target.get("turn_id"),
                    "target_role": "user",
                }
        return {
            "resolution": "snapshot",
            "target_user_id": target_user_id,
            "target_nickname": str(reply_seed.get("target_nickname") or "").strip(),
            "fallback_text": str(reply_seed.get("fallback_text") or "").strip(),
        }

    @filter.event_message_type(filter.EventMessageType.ALL, priority=2000)
    async def track_platform_context(self, event: AstrMessageEvent):
        """记下事件里的平台上下文：bot 实例，并按需补订回显。

        cid **不在这里记**：本 Hook 在 `capture_user`（priority 1000）之前运行，
        此时 `chat_memory_cid` 一定还没写上（那是捕获成功后才设的），读了永远是空串。
        cid 改由 `_capture_user_internal` 落库成功后直接调 `_remember_cid`。
        bot 对象必须每来一个事件确认一次：平台重载会换 bot，订阅跟着换总线。
        """
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            platform_id, message_type, session_id = self._parse_umo(umo)
            if not (platform_id and message_type and session_id):
                return
            event_bot = getattr(event, "bot", None)
            if event_bot is not None:
                self._platform_bots[platform_id] = event_bot   # 学下来给巡检/卸载用
            self._ensure_echo_subscription(platform_id, event_bot, source="event")
        except Exception as exc:  # noqa: BLE001 - 记录上下文不得影响任何流程
            logger.debug(
                f"{self._log_prefix(event)} 平台上下文记录失败: {type(exc).__name__}"
            )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def capture_user(self, event: AstrMessageEvent):
        """所有进入 ProcessStage 的 user 消息立即落库（默认 llm_status=''）。"""
        await self._ensure_cleanup_started()
        await self._capture_user_internal(event)

    # ── LLM 触发标记 + 兜底捕获 ──────────────────────

    @filter.on_llm_request()
    async def mark_llm_triggered(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 调用时：兜底重试 user 捕获并按 ``turn_id`` 升级为 ``llm_pending``。"""
        # 同一事件可能触发多次 LLM 调用（tool loop）：状态已升级过就早退，避免重复 DB 写。
        # 捕获失败时不会设置该标记，后续 LLM 调用仍会重试兜底捕获。
        if event.get_extra("chat_memory_llm_triggered"):
            return

        if not event.get_extra("chat_memory_captured"):
            logger.debug(f"{self._log_prefix(event)} LLM 触发，补捕获 user（首条消息兜底）")
            ok = await self._capture_user_internal(event)
            if not ok:
                logger.warning(f"{self._log_prefix(event)} LLM 触发但 user 捕获失败，放弃 llm_status 更新")
                return

        umo = getattr(event, "unified_msg_origin", "")
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not umo or not cid:
            return

        event.set_extra("chat_memory_llm_triggered", True)

        turn_id = event.get_extra("chat_memory_turn_id")
        if not turn_id:
            logger.warning(f"{self._log_prefix(event)} user 已捕获但缺少 turn_id，跳过 llm_status 更新")
            return
        await self._safe_update_llm_status_by_turn(umo, cid, turn_id, _LLM_PENDING)
        logger.debug(f"{self._log_prefix(event)} turn[{turn_id}] llm_status -> llm_pending")

    # ── 工具调用捕获（写入 CM 数据库）──────────────────

    @staticmethod
    def _tool_result_to_text(tool_result) -> str:
        """把 CallToolResult 转成可存档的纯文本。

        只取文本内容；图片/二进制资源用占位符（runner 消息流里 LLM 看到的
        也是缓存路径文本而非 base64），不把敏感二进制原样入库。
        """
        if tool_result is None:
            return ""
        parts: list[str] = []
        for item in getattr(tool_result, "content", None) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            resource = getattr(item, "resource", None)
            if resource is not None:
                resource_text = getattr(resource, "text", None)
                if isinstance(resource_text, str):
                    parts.append(resource_text)
                else:
                    parts.append("[resource content]")
                continue
            if hasattr(item, "data"):  # ImageContent 等二进制块
                parts.append("[image content]")
                continue
            try:
                parts.append(str(item))
            except Exception:
                parts.append("[unparsed content]")
        structured = getattr(tool_result, "structuredContent", None)
        if isinstance(structured, dict) and structured:
            try:
                parts.append(
                    json.dumps(structured, ensure_ascii=False, default=str)
                )
            except Exception:
                parts.append("[structured content]")
        text = "\n\n".join(part for part in parts if part).strip()
        if text and getattr(tool_result, "isError", False):
            text = f"[error] {text}"
        return text

    @staticmethod
    def _truncate_tool_args(args_json: str, max_chars: int) -> str:
        """截断 tool 参数时必须保持合法 JSON（provider 会校验 arguments）。

        超限直接替换为占位对象，避免把截断后的非法 JSON 回放给 LLM。
        """
        if max_chars <= 0 or len(args_json) <= max_chars:
            return args_json
        return '{"_cm_truncated": true}'

    @staticmethod
    def _build_tool_contexts(records: list[dict]) -> list[dict]:
        """把工具记录渲染成 OpenAI 格式：每轮一条 assistant(tool_calls) + N 条 role=tool。

        tool_call_id 为 CM 自造（``cm_tool_<turn_id>_<n>``，turn_id 完整不带截断），只要求同一轮内
        assistant 与 tool 消息成对一致，provider 不校验其来源。每条消息携带
        私有 ``_cm_tool_turn`` 供插入定位，注入前由调用方剥掉。
        """
        grouped: dict[str, list[dict]] = {}
        order: list[str] = []
        for record in records:
            turn_id = str(record.get("turn_id") or "")
            if turn_id not in grouped:
                grouped[turn_id] = []
                order.append(turn_id)
            grouped[turn_id].append(record)

        contexts: list[dict] = []
        for turn_id in order:
            recs = grouped[turn_id]
            call_ids = [f"cm_tool_{turn_id}_{i}" for i in range(len(recs))]
            contexts.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_ids[i],
                            "type": "function",
                            "function": {
                                "name": recs[i].get("tool_name") or "",
                                "arguments": recs[i].get("tool_args") or "{}",
                            },
                        }
                        for i in range(len(recs))
                    ],
                    "_cm_tool_turn": turn_id,
                }
            )
            for i, record in enumerate(recs):
                contexts.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_ids[i],
                        "content": record.get("tool_result") or "",
                        "_cm_tool_turn": turn_id,
                    }
                )
        return contexts

    @staticmethod
    def _insert_tool_contexts(
        result: list[dict],
        tool_contexts: list[dict],
    ) -> list[dict]:
        """把工具调用段插入到对应轮次内部，与 AstrBot 原生历史顺序对齐。

        原生形态为 ``[user, assistant(tool_calls), tool…, assistant 最终回复]``；
        本方法把每轮的工具段放到该轮 user 之后（只有 assistant 的单边轮次则放
        其之前）。工具段必须跟随实际调用轮：turn 不在当前历史 contexts 中的段
        **直接丢弃**（如配置 300 轮、工具调用发生在 400 轮之前），不贴尾部、
        不按时间猜测——不存在的轮次不配拥有工具上下文。插入完成后剥掉所有
        私有定位键。
        """
        if not tool_contexts:
            for context in result:
                context.pop("_turn_id", None)
            return result
        # 按 turn 拆分工具段（保持组内顺序）
        turns: dict[str, list[dict]] = {}
        turn_order: list[str] = []
        for msg in tool_contexts:
            turn_id = str(msg.get("_cm_tool_turn") or "")
            if turn_id not in turns:
                turns[turn_id] = []
                turn_order.append(turn_id)
            turns[turn_id].append(msg)
        # 历史中每个 turn 的 user / assistant 位置
        user_pos: dict[str, int] = {}
        asst_pos: dict[str, int] = {}
        for index, context in enumerate(result):
            turn_id = str(context.get("_turn_id") or "")
            if not turn_id:
                continue
            if context.get("role") == "user" and turn_id not in user_pos:
                user_pos[turn_id] = index
            elif context.get("role") == "assistant" and turn_id not in asst_pos:
                asst_pos[turn_id] = index
        insert_at: dict[int, list[dict]] = {}
        for turn_id in turn_order:
            if turn_id in user_pos:
                key = user_pos[turn_id] + 1  # 该轮 user 之后、最终回复之前
            elif turn_id in asst_pos:
                key = asst_pos[turn_id]  # 单边 assistant 之前
            else:
                # 轮次不在当前上下文中：丢弃该段，绝不贴到别处。
                continue
            insert_at.setdefault(key, []).extend(turns[turn_id])
        merged: list[dict] = []
        for index, context in enumerate(result):
            merged.extend(insert_at.pop(index, []))
            context.pop("_turn_id", None)
            merged.append(context)
        for key in sorted(insert_at):
            merged.extend(insert_at[key])
        for msg in tool_contexts:
            msg.pop("_cm_tool_turn", None)
        return merged

    async def _safe_insert_tool(
        self,
        umo: str,
        cid: str,
        turn_id: str,
        call_index: int,
        tool_name: str,
        tool_args: str,
        tool_result: str,
    ) -> bool:
        try:
            # insert_tool_record 现在如实返回"行是否真的落库"（幂等键冲突时为 False）：
            # 原来恒定返回 True，这一轮的工具段其实没写进去也看不出来。
            inserted = await self.db.insert_tool_record(
                umo, cid, turn_id, call_index, tool_name, tool_args, tool_result
            )
            return bool(inserted)
        except Exception as e:
            # 只记异常类型：DB 异常串会带出 SQL 参数（= 工具参数/结果正文）
            logger.warning(f"{self._log_prefix()} 工具调用写入失败: {type(e).__name__}")
            return False

    async def _query_tool_contexts(self, umo: str, cid: str, source: str = "") -> list[dict]:
        """按当前接管配置回放 CM 库中最近 N 轮工具调用为 OpenAI 格式 contexts。

        ``keep_tool_turns=0`` 时直接关闭回放（用户可显式填 0）。``source="v2"`` 时读
        合并存储的工具表；读失败回退旧表（工具段与历史段必须同源，读源要一起切）。
        """
        if self.ct_keep_tool_turns <= 0:
            return []
        try:
            records = await self._read_with_switch(
                lambda: self.db.query_tool_records_v2(umo, cid, self.ct_keep_tool_turns),
                lambda: self.db.query_tool_records(umo, cid, self.ct_keep_tool_turns),
                source=source,
            )
        except Exception as e:  # noqa: BLE001 - 工具段取不到不影响历史段
            logger.warning(
                f"{self._log_prefix()} 工具上下文查询失败: {type(e).__name__}"
            )
            return []
        return self._build_tool_contexts(records)

    @filter.on_llm_tool_respond()
    async def capture_tool(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """LLM 工具调用完成后立即落库（独立工具表，不进 user/assistant 配对）。

        turn_id 复用当前轮（主动消息无轮次时自造）；``(turn_id, call_index)``
        幂等。参数与返回文本跟随 ``max_content_length`` 截断（0=不截断）。
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not cid:
            return
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if not tool_name:
            return
        turn_id = event.get_extra("chat_memory_turn_id") or uuid.uuid4().hex
        event.set_extra("chat_memory_turn_id", turn_id)
        try:
            seq = int(event.get_extra("chat_memory_tool_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        call_index = seq + 1
        event.set_extra("chat_memory_tool_seq", call_index)
        args_json = (
            json.dumps(tool_args, ensure_ascii=False, default=str)
            if tool_args is not None
            else "{}"
        )
        result_text = self._tool_result_to_text(tool_result)
        ok = await self._safe_insert_tool(
            umo,
            cid,
            turn_id,
            call_index,
            tool_name,
            self._truncate_tool_args(args_json, self.max_len),
            self._truncate(result_text),
        )
        if ok:
            logger.debug(
                f"{self._log_prefix(event)} tool[{tool_name}][#{call_index}] -> "
                f"{cid}: {result_text}"
            )

    # ── 上下文接管 ───────────────────────────────────

    @filter.on_llm_request(priority=-100)
    async def take_over_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """接管 req.contexts，注入 CM 数据 + 清空 native history。

        priority=-100 晚于常规钩子执行；最终当前轮焦点由 priority=-1000 的独立钩子补齐。
        与 mark_llm_triggered(默认 0) 顺序：先标记 llm_pending，后接管（CM 已落库再读取）。
        """
        if not self.ct_enable:
            return

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        cid = await self._get_curr_cid(umo)
        if not cid:
            logger.debug(f"{self._log_prefix(event)} 接管跳过：cid 未就绪（首条消息）")
            return

        user_id = event.get_sender_id() or ""
        persona_id = ""
        if self.ct_filter_by_persona:
            # capture_user 已把生效 persona 缓存进 extras，复用即可；
            # 只有未缓存（如 user 捕获被跳过）时才重新解析。
            persona_id = event.get_extra("chat_memory_persona_id")
            if persona_id is None:
                persona_id = await self._get_effective_persona(umo, event, cid)
            if not persona_id:
                logger.debug(
                    f"{self._log_prefix(event)} filter_by_persona=True 但当前生效 persona_id 为空，"
                    f"将仅匹配 persona_id IS NULL OR '' 的记录（老数据/未分配 persona 的消息）"
                )
        current_turn_id = event.get_extra("chat_memory_turn_id") or ""
        try:
            contexts = await self.build_takeover_contexts(
                umo=umo,
                user_id=user_id,
                conversation_id=cid,
                persona_id=persona_id,
                exclude_turn_id=current_turn_id,
            )
        except Exception as exc:  # noqa: BLE001 - 读失败 ≠ 没有历史
            await self._handle_empty_takeover(
                event, req, umo, cid, f"读取失败({type(exc).__name__})", failed=True
            )
            return
        if not contexts:
            await self._handle_empty_takeover(event, req, umo, cid, "CM 无数据")
            return

        # 真正注入时才检查内置群聊上下文感知共存（关闭或未命中不发提示）
        self._warn_builtin_ltm_coexist(event)

        # 通用规则无条件注入（cm_ 标签禁令 + cm_time 说明），位于模式指令之前。
        self._append_general_rules(req)
        if self.ct_full_group and self._is_group_umo(umo):
            self._append_full_group_instruction(req)
        if (
            self.ct_cross_session
            and user_id
            and self._has_cross_session_labels(contexts)
        ):
            self._append_cross_session_instruction(req)
        req.contexts = contexts
        event.set_extra(_TAKEOVER_APPLIED_EXTRA, True)

        if self.ct_clear_native_history:
            await self._safe_reset_history(umo, cid)

        logger.debug(
            f"{self._log_prefix(event)} 接管 contexts={len(contexts)} "
            f"(cross_session={self.ct_cross_session}, full_group={self.ct_full_group}, "
            f"cid={cid})"
        )

    @filter.on_llm_request(priority=-1000)
    async def inject_current_turn_focus(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """在其他上下文插件之后追加本轮结构锚，并与历史尾部 user 切开。"""
        if not event.get_extra(_TAKEOVER_APPLIED_EXTRA, False):
            return
        if event.get_extra(_CURRENT_FOCUS_INJECTED_EXTRA, False):
            return

        try:
            template, relation_data = self._build_relation_seed(event)
            current_part = TextPart(
                text=build_current_turn_xml(
                    template,
                    relation_data,
                    self._get_self_id(event),
                    speaker_nickname=self._get_sender_nickname(event),
                )
            ).mark_as_temp()
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = []
                req.extra_user_content_parts = parts

            parts.append(current_part)
            event.set_extra(_CURRENT_FOCUS_INJECTED_EXTRA, True)
        except Exception as exc:
            logger.warning(
                f"{self._log_prefix(event)} 当前轮焦点注入失败: {type(exc).__name__}"
            )

    async def _handle_empty_takeover(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        umo: str,
        cid: str,
        reason: str,
        *,
        failed: bool = False,
    ) -> None:
        """处理 takeover 空结果。

        严格模式显式清空 ``req.contexts``，保证 CM 仍是唯一上下文源；兼容模式保留
        AstrBot 已装载的 native contexts。两种模式都遵循 ``clear_native_history`` 配置。

        ``failed=True`` 只影响**日志**：读失败与"确实没有历史"在结果上都按用户配置处理
        （严格模式就是空上下文，那是用户要的语义），但读失败要打 WARNING——否则"合并
        存储读挂了"会被当成"CM 没记录"，无从排查。
        """
        if failed:
            logger.warning(
                f"{self._log_prefix(event)} 接管读取失败（按当前配置处理）：{reason}"
            )
        if self.ct_fallback_to_native_on_empty:
            logger.debug(f"{self._log_prefix(event)} 接管回退 native：{reason}")
            return

        req.contexts = []
        # 即使历史为空也注入通用规则，避免模型收到未解释的 cm_ 标签。
        self._append_general_rules(req)
        event.set_extra(_TAKEOVER_APPLIED_EXTRA, True)
        if self.ct_clear_native_history:
            await self._safe_reset_history(umo, cid)
        logger.debug(f"{self._log_prefix(event)} 严格接管 contexts=0：{reason}")

    def _warn_invalid_blacklist(self, entries) -> int:
        """非法黑名单条目逐条 WARNING（返回告警条数）。

        写错的名单等于没有名单：用户以为拉黑了某群、实际条目被判非法丢弃，必须可见。
        逐条输出（不截断条目本身），格式提示只打一次。
        """
        bad = invalid_blacklist_entries(entries)
        if not bad:
            return 0
        logger.warning(
            f"{self._log_prefix()} 跨会话黑名单有 {len(bad)} 条写法不对，已忽略；"
            f"格式：G:<群号> 或 F:<用户号>"
        )
        for item in bad:
            logger.warning(
                f"{self._log_prefix()} 跨会话黑名单条目无效（已忽略）：{item}"
            )
        return len(bad)

    def _blacklist_entries(self) -> list[str]:
        """跨会话黑名单（懒取：__init__ 之外构造的实例也不会炸）。"""
        return list(getattr(self, "ct_cross_session_blacklist", None) or [])

    def _note_blacklisted_session(self, umo: str) -> None:
        """黑名单会话只提示一次：让"为什么这条会话没有跨会话记忆"在日志里可见。"""
        noted = getattr(self, "_blacklist_noted", None)
        if noted is None:
            noted = set()
            self._blacklist_noted = noted
        if umo in noted:
            return
        noted.add(umo)
        # INFO 只放事件与 id（不放正文）：前缀走统一格式，platform 段取 umo 的第一段
        platform_id = umo.split(":", 1)[0] if ":" in umo else ""
        prefix = (
            f"[ChatMemory][platform:{platform_id}]"
            if self.log_with_bot_id and platform_id
            else "[ChatMemory]"
        )
        if not hasattr(self, "_blacklist_logged"):
            self._blacklist_logged: set[str] = set()
        first = umo not in self._blacklist_logged
        if first:
            self._blacklist_logged.add(umo)
        log_fn = logger.info if first else logger.debug
        log_fn(
            f"{prefix} 会话在跨会话黑名单内，本次只使用本会话记忆: {umo}"
            f"（名单 {len(self._blacklist_entries())} 项）"
        )

    def _shadow_read_source(self) -> str:
        """配置决定的读源：``"v2"``=合并存储，``"v1"``=旧表（默认）。

        切读没勾、或双写没跑满门槛时一律 v1（启动日志会说还差几天）。
        显式 ``source="v2"`` 不受此限制——那是程序化的对拍/灰度开关，见
        ``_resolve_read_source``。
        """
        if not getattr(self, "shadow_read", False):
            return "v1"
        return "v2" if getattr(self, "_shadow_read_ready", False) else "v1"

    def _resolve_read_source(self, source: str = "") -> str:
        """显式 ``source`` 优先，空串跟随配置；未知值按配置处理（不会静默当 v2）。"""
        value = str(source or "").strip().lower()
        if value in ("v1", "v2"):
            return value
        return self._shadow_read_source()

    async def _refresh_shadow_read_gate(self) -> None:
        """按双写窗口判定切读是否已解锁，并把结论写进日志。

        规则：双写跑满 ``_SHADOW_READ_MIN_DAYS`` 天才允许切读；没到日子时**配置照收、
        但不生效**（仍按只写不读跑），并且必须能看见还差几天。
        """
        days = await self.db.dual_write_window_days()
        self._shadow_read_window_days = float(days)
        self._shadow_read_ready = days >= _SHADOW_READ_MIN_DAYS
        if _DEBUG_IGNORE_CUTOVER_WINDOW:  # 仅本机验证；发布态必须为 False
            self._shadow_read_ready = True
        if not getattr(self, "shadow_read", False):
            logger.debug(
                f"{self._log_prefix()} 切读未启用（只写不读）；双写窗口 "
                f"{days:.1f} 天，门槛 {_SHADOW_READ_MIN_DAYS} 天"
            )
        elif self._shadow_read_ready:
            logger.info(
                f"{self._log_prefix()} 切读已生效：双写窗口 {days:.1f} 天，"
                f"接管上下文与公开查询 API 都读合并存储"
            )
        else:
            remain = max(0.0, _SHADOW_READ_MIN_DAYS - max(days, 0.0))
            shown = "尚未开始记录（本次启动写入起点）" if days < 0 else f"{days:.1f} 天"
            logger.info(
                f"{self._log_prefix()} 切读已启用，但双写窗口还没满："
                f"当前 {shown}，还需约 {remain:.1f} 天；在此之前仍按只写不读运行"
            )

    async def _read_failed(self, exc: BaseException) -> None:
        """切读失败：回退旧表 + 限流告警（不能变成空上下文，也不能静默退化）。

        前 3 次逐次 WARNING；之后每 100 次再响一次——长期坏掉时不能永久沉到 DEBUG，
        否则"切读看起来生效"其实每轮都在回退，再没有任何可见信号。
        """
        count = int(getattr(self, "_shadow_read_failures", 0)) + 1
        self._shadow_read_failures = count
        # 带上异常消息（截断）：SQLite 的 OperationalError 文案是
        # "database is locked" / "no such column: x" 这类，不含聊天正文，
        # 只记类型会让"为什么回退"完全无从判断（线上踩过：只看到 OperationalError）。
        reason = str(exc).strip().replace("\n", " ")[:160]
        detail = (
            f"合并存储读取失败（第 {count} 次），本次已回退旧表: "
            f"{type(exc).__name__}{': ' + reason if reason else ''}"
        )
        if count <= 3 or count % 100 == 0:
            logger.warning(f"{self._log_prefix()} {detail}")
        else:
            logger.debug(f"{self._log_prefix()} {detail}")

    async def _read_with_switch(self, v2_call, v1_call, source: str = ""):
        """配置切到 v2 就先读合并存储，失败自动回退旧表；否则直接读旧表。

        ``source`` 显式给 ``"v1"`` / ``"v2"`` 时覆盖配置（对拍与灰度用）。
        """
        if self._resolve_read_source(source) != "v2":
            return await v1_call()
        try:
            return await v2_call()
        except Exception as exc:  # noqa: BLE001 - 回退旧表，绝不把读取失败变成空结果
            await self._read_failed(exc)
            return await v1_call()

    async def _takeover_query(
        self,
        umo: str,
        cid: str,
        user_id: str,
        persona_id: str = "",
        exclude_turn_id: str = "",
        force_current_session: bool = False,
        source: str = "",
    ) -> tuple[list[dict], str]:
        """按 cross_session / full_group / 配对模式 查询 CM 数据，返回扁平化 records 列表。

        两种查询模式：
        - **配对模式**（仅 llm_success）：用 ``query_rounds_raw`` 查配对轮次，按轮数切片
        - **混合模式**（含其他状态）：用 ``query_messages_raw`` 查全量，按条数切片

        ``limit_rounds`` 含义随模式变化：
        - 配对模式 → 轮数（user-assistant 一对为一轮）
        - 混合模式 → 消息数（单条记录）

        ``persona_id``：仅当 ``ct_filter_by_persona=True`` 时由调用方填入。
        ``exclude_turn_id``：混合模式排除本轮刚写入的 user，避免它同时出现在
        ``req.contexts`` 与当前 ``req.prompt`` 中。
        ``force_current_session``：忽略 ``cross_session``，只查当前 UMO + CID；用于
        ``full_group`` 下缺少 ``user_id`` 的只读公开调用，防止跨 UMO 范围失去用户约束。

        ``source``：空串（默认）跟随切读开关 ``shadow_read``；``"v1"`` 读旧表、
        ``"v2"`` 读合并存储（影子层）可显式覆盖。只换数据来源，scope / 状态 / 白名单 /
        persona / 撤回跳过等语义完全一致——对拍与切读就是靠这条路径。

        返回 ``(records, actual_source)``：``actual_source`` 是**这次真正用上的读源**。
        ``"v2"`` 读失败会整段回退 ``"v1"``（不是只回退失败的那一步），调用方必须拿这个
        返回值去取 Reply 目标与工具段，否则会出现"历史来自旧表、引用目标来自合并存储"。
        """
        limit = self.ct_limit_rounds
        status_set: set[str] = set(self.ct_llm_status_filter)
        include_kinds = self.ct_include_kinds  # set[str]
        all_match = self.ct_include_all_match
        filter_by_persona = self.ct_filter_by_persona

        # 判断配对模式：仅 llm_success
        is_pair_only = (status_set == {"llm_success"})

        # full_group 仅群聊生效
        effective_full_group = self.ct_full_group and self._is_group_umo(umo)

        # cross_session：跨 CID（cid=None）+ 跨 umo（cross_umo=True）
        # 跨 umo 按 platform_id + user_id 聚合，实现群私聊互通
        # 黑名单**双向**生效：名单内的会话不参与跨会话记忆——即便开了 cross_session，
        # 也只读本会话（同 CID）的记录；同时它也不会作为来源出现在别人的记忆里（由
        # storage 层的 scope 排除保证）。
        cross_read = effective_cross_session(
            self.ct_cross_session,
            umo,
            self._blacklist_entries(),
            force_current_session=force_current_session,
        )
        if session_blacklisted(umo, self._blacklist_entries()):
            self._note_blacklisted_session(umo)
        target_cid: Optional[str] = None if cross_read else cid
        cross_umo = cross_read

        reader_v2 = self._resolve_read_source(source) == "v2"

        async def _fetch(use_v2: bool) -> list[dict]:
            """按当前模式取数；读源只影响这一层，scope/状态/白名单一字不改。"""
            if is_pair_only:
                # 配对模式：按 user 时间排序整轮，并保持 user/assistant 相邻。
                # query_rounds_raw 已按 created_at ASC, id ASC 返回，无需再次排序。
                rounds = await (self.db.query_rounds_raw_v2 if use_v2
                                else self.db.query_rounds_raw)(
                    umo, target_cid, user_id, limit, include_kinds, all_match,
                    cross_umo=cross_umo, full_group=effective_full_group,
                    blacklist=self._blacklist_entries(),
                    persona_id=persona_id, filter_by_persona=filter_by_persona,
                    skip_recalled=self.ct_recall_skip_context,
                )
                return [msg for rnd in rounds for msg in rnd]
            # 混合模式：按消息数；overfetch 2x 给规整留余地（防先 LIMIT 后过滤导致空上下文）
            # 规整阶段会丢头部 assistant / 尾部 solo；overfetch 后再在 normalize 截到目标条数
            # query_messages_raw 已按全局时间线升序返回，无需再次排序。
            return await (self.db.query_messages_raw_v2 if use_v2
                          else self.db.query_messages_raw)(
                umo, target_cid, user_id, limit * 2, status_set, include_kinds, all_match,
                cross_umo=cross_umo, full_group=effective_full_group,
                blacklist=self._blacklist_entries(),
                persona_id=persona_id, filter_by_persona=filter_by_persona,
                exclude_turn_id=exclude_turn_id or None,
                skip_recalled=self.ct_recall_skip_context,
            )

        try:
            records = await _fetch(reader_v2)
        except Exception as exc:
            if not reader_v2:
                logger.warning(
                    f"{self._log_prefix()} 接管查询失败: {type(exc).__name__}"
                )
                raise
            # 切读失败**绝不能**变成空上下文：整段回退旧表，并把这件事留痕（告警计数）
            await self._read_failed(exc)
            records = await _fetch(False)
            reader_v2 = False

        # 事件子类过滤：kind 白名单只能到 event，子类（ban/member/…）
        # 记录在 relation_data.event.type，查询后过滤。
        allowed_event_types = self.ct_event_kinds
        if allowed_event_types != set(_ALL_EVENT_TYPES):
            records = [
                record
                for record in records
                if not self._is_event_record(record)
                or self._event_type_of(record) in allowed_event_types
            ]
        # 撤回感知（方案 A）：开启后撤回痕迹完全不出现在上下文——被撤回的原消息
        # （recalled_at 非空，已在 SQL 层过滤，这里兜底）与撤回事件都丢弃。
        if self.ct_recall_skip_context:
            records = [
                record
                for record in records
                if not str(record.get("recalled_at") or "").strip()
                and not (
                    self._is_event_record(record)
                    and self._event_type_of(record) == "recall"
                )
            ]
        return records, ("v2" if reader_v2 else "v1")

    @staticmethod
    def _is_event_record(record: dict) -> bool:
        kinds = record.get("content_kind") or []
        return "event" in kinds or "system_event" in kinds

    @staticmethod
    def _event_type_of(record: dict) -> str:
        relation = record.get("relation_data")
        event = relation.get("event") if isinstance(relation, dict) else None
        if not isinstance(event, dict):
            return "system"
        return str(event.get("type") or "system").strip() or "system"

    def _takeover_normalize(
        self,
        records: list[dict],
        umo: str,
        max_records: Optional[int] = None,
        max_chars: int = 0,
        current_user_id: str = "",
        full_group: bool = False,
        target_map: Optional[dict[tuple[str, str], dict]] = None,
        cross_session: bool = False,
        paired_rounds: bool = False,
    ) -> list[dict]:
        builder = TakeoverContextBuilder(
            media_kinds=_MEDIA_KINDS,
            current_user_id=current_user_id,
            full_group=full_group,
            current_umo=umo,
            cross_session=cross_session,
            paired_rounds=paired_rounds,
            proactive_status=_LLM_PROACTIVE,
            orphan_status=_LLM_ORPHAN,
            target_map=target_map,
        )
        return builder.normalize(
            records,
            max_records=max_records,
            max_chars=max_chars,
            # 内部路径保留 turn_id：用于把工具调用段插入对应轮次，返回前会剥掉。
            keep_turn_id=True,
        )

    @staticmethod
    def _append_general_rules(req: ProviderRequest) -> None:
        """通用规则（cm_ 结构化标签禁令）无条件追加，且同一请求只加一次。"""
        existing = (getattr(req, "system_prompt", "") or "").strip()
        if CM_GENERAL_RULES in existing:
            return
        req.system_prompt = (
            f"{existing}\n\n{CM_GENERAL_RULES}"
            if existing
            else CM_GENERAL_RULES
        )

    @staticmethod
    def _append_full_group_instruction(req: ProviderRequest) -> None:
        """把 full-group 转录解释规则追加到 system prompt，且同一请求只加一次。"""
        existing = (getattr(req, "system_prompt", "") or "").strip()
        if FULL_GROUP_CONTEXT_INSTRUCTION in existing:
            return
        req.system_prompt = (
            f"{existing}\n\n{FULL_GROUP_CONTEXT_INSTRUCTION}"
            if existing
            else FULL_GROUP_CONTEXT_INSTRUCTION
        )

    @staticmethod
    def _append_cross_session_instruction(req: ProviderRequest) -> None:
        """把跨会话来源规则追加到 system prompt，且同一请求只加一次。"""
        existing = (getattr(req, "system_prompt", "") or "").strip()
        if CROSS_SESSION_CONTEXT_INSTRUCTION in existing:
            return
        req.system_prompt = (
            f"{existing}\n\n{CROSS_SESSION_CONTEXT_INSTRUCTION}"
            if existing
            else CROSS_SESSION_CONTEXT_INSTRUCTION
        )

    @staticmethod
    def _has_cross_session_labels(contexts: list[dict]) -> bool:
        """仅在规整结果实际含其他/未知来源时启用跨会话提示词。"""
        return any(
            CROSS_SESSION_SOURCE_MARKER_PREFIX
            in str(context.get("content") or "")
            for context in contexts
        )

    @staticmethod
    def _is_group_umo(umo: str) -> bool:
        if not umo:
            return False
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False
        return parts[1] == "GroupMessage"

    def _warn_builtin_ltm_coexist(self, event: AstrMessageEvent) -> None:
        """检测 AstrBot 内置「群聊上下文感知」与 CM 接管共存，进程内提示一次。

        内置功能经 extra_user_content_parts 以纯文本格式额外注入群聊历史，
        与 CM 接管注入的 <cm_*> 结构化历史重复且格式冲突。仅群聊事件检测；
        始终启用（不可配置），配置读取失败静默，不干扰主流程。
        """
        if self._builtin_ltm_warned:
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not self._is_group_umo(umo):
            return
        try:
            settings = self.context.get_config(umo=umo).get(
                "provider_ltm_settings"
            ) or {}
            group_icl_enable = bool(settings.get("group_icl_enable", False))
        except Exception:
            return
        if not group_icl_enable:
            return
        self._builtin_ltm_warned = True
        logger.warning(
            f"{self._log_prefix(event)} 检测到 AstrBot 内置「群聊上下文感知」已启用："
            f"群聊历史会以纯文本格式额外注入，与 chat_memory 接管注入重复且格式冲突；"
            f"建议在 Dashboard 关闭「群聊上下文感知」或关闭本插件接管"
            f"（本次运行仅提示一次）"
        )

    @staticmethod
    def _record_umo(record: dict, fallback_umo: str = "") -> str:
        """从消息审计字段还原 Reply 目标所在的原始 UMO。

        Reply 只能引用同一会话内的消息。跨会话 takeover 展示历史 Reply 时，
        必须按被引用记录自身的来源查询目标，不能拿当前请求所在的 UMO 代替。
        当前会话旧记录若缺少部分审计字段，可安全回退到当前 UMO。外部来源
        字段不完整时也只会尝试当前 UMO；内部 turn_id 查不到即自然降级，
        不使用昵称、时间或正文猜测来源。
        """
        fallback_parts = str(fallback_umo or "").split(":", 2)
        platform_id = str(record.get("platform_id") or "").strip()
        message_type = str(record.get("message_type") or "").strip()
        session_id = str(
            record.get("session_id") or record.get("group_id") or ""
        ).strip()
        if len(fallback_parts) == 3 and session_id == fallback_parts[2]:
            platform_id = platform_id or fallback_parts[0]
            message_type = message_type or fallback_parts[1]
        if not (platform_id and message_type and session_id):
            return ""
        return f"{platform_id}:{message_type}:{session_id}"

    async def _safe_reset_history(self, umo: str, cid: str):
        try:
            await self.context.conversation_manager.update_conversation(
                umo, conversation_id=cid, history=[]
            )
        except Exception as e:
            # 只记异常类型：异常串可能带出待写入的 history 正文
            logger.warning(f"{self._log_prefix()} 清理 native history 失败: {type(e).__name__}")

    # ── 捕获 BOT 回复 + 检测 reset/new ──────────────────

    @filter.on_decorating_result(priority=10000)
    async def capture_bot(self, event: AstrMessageEvent):
        """尽早捕获 BOT 回复；LLM 文本清除泄漏的 cm_ 标签后再落库。"""
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return

        # /reset / /new 检测：信任 AstrBot 核心设置的会话清理标志，再从当前
        # 事件文本中识别具体命令；不依赖可能被本地化或改写的 Bot 回复文本。
        if event.get_extra("_clean_group_context_session"):
            control_command = self._get_context_control_command(event)
            if control_command:
                await self._on_reset_or_new(event, umo, control_command)
                return
            # 核心已清理会话，但我们分不清 reset/new：宁可不动（/new 时清库会误删），
            # 但要留下可诊断的原文（WARNING，完整不截断），避免"reset 没反应"无从查起。
            # 同一形态只 WARNING 一次（花式写法如 /reset@bot 会反复命中），之后降 DEBUG。
            raw_text = self._get_context_control_text(event)
            log_fn = (
                logger.debug if self._warned_reset_unknown else logger.warning
            )
            self._warned_reset_unknown = True
            log_fn(
                f"{self._log_prefix(event)} 核心会话清理标志存在，但事件文本无法区分 "
                f"reset/new，CM 未清理存档（原文：{raw_text!r}）"
            )
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        is_llm_result = bool(result.is_llm_result())
        if is_llm_result:
            # 只清洗一次：直接改写发送用的组件链，落库与发送共用清理后的数据，
            # 用户看到的与存档的一致，都不会带泄漏的 <cm_*> 结构。
            if self._clean_sent_chain(result.chain):
                logger.debug(f"{self._log_prefix(event)} assistant 已裁剪 cm_ XML 标签")
        asst_kind, bot_text = self._classify_assistant_chain(result.chain)
        bot_text = self._strip_reasoning_prefix(bot_text)
        if not asst_kind:
            # 完全空 chain（无 Plain 也无任何媒体组件）才跳过；纯图 / 纯语音仍入库
            return

        user_id = event.get_sender_id() or ""
        if not user_id:
            return

        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not cid:
            return

        user_msg_id = event.get_extra("chat_memory_user_msg_id") or ""
        llm_triggered = bool(event.get_extra("chat_memory_llm_triggered"))
        no_mid = bool(event.get_extra("chat_memory_no_mid"))
        capture_attempted = bool(event.get_extra("chat_memory_capture_attempted"))
        captured = bool(event.get_extra("chat_memory_captured"))
        # 只有成功捕获的 user 才共享其 turn_id；orphan/proactive 必须保持单边，
        # 避免后续 user 重试成功后把历史 orphan assistant 错配进正常轮次。
        turn_id = (
            (event.get_extra("chat_memory_turn_id") or uuid.uuid4().hex)
            if captured
            else uuid.uuid4().hex
        )

        # 判定 assistant.llm_status + pair_id
        if not capture_attempted:
            # 没经过 capture_user → 主动消息（含 cron）
            asst_status = _LLM_PROACTIVE
            pair_id: Optional[str] = None
            logger.debug(f"{self._log_prefix(event)} assistant 标 proactive（主动消息）")
        elif not captured:
            # 经过 capture_user 但落库失败 → 漏存
            asst_status = _LLM_ORPHAN
            pair_id = None
            logger.warning(
                f"{self._log_prefix(event)} assistant 标 orphan（user 漏存：DB 写入失败）"
            )
        elif llm_triggered and is_llm_result:
            asst_status = _LLM_SUCCESS
            pair_id = user_msg_id if not no_mid else None
        else:
            # 走 capture_user 但没走 LLM（命令回复、set_result 等）
            asst_status = _LLM_DEFAULT
            pair_id = user_msg_id if not no_mid else None
            if no_mid:
                logger.debug(f"{self._log_prefix(event)} assistant 平台无 mid，使用 turn_id 配对")

        content = self._truncate(bot_text) if bot_text else self._content_placeholder(asst_kind)
        # persona_id：优先从 extras（capture_user 已缓存）；兜底重查
        persona_id = event.get_extra("chat_memory_persona_id")
        if persona_id is None:
            persona_id = await self._get_effective_persona(umo, event, cid)
        audit = self._collect_audit_fields(event)
        self._remember_cid(umo, str(cid), self._get_group_id(event))
        event.set_extra("chat_memory_assistant_turn_id", turn_id)
        event.set_extra("chat_memory_assistant_started_at", int(time.time()))
        event.set_extra("chat_memory_assistant_llm_status", asst_status)
        event.set_extra("chat_memory_assistant_persona_id", persona_id or "")
        ok = await self._safe_insert(
            umo, cid, user_id, "assistant", content,
            message_id=None, pair_id=pair_id,
            llm_status=asst_status, content_kind=asst_kind,
            persona_id=persona_id or None,
            turn_id=turn_id,
            send_status=_SEND_PREPARED,
            update_user_llm_status=(
                _LLM_SUCCESS if asst_status == _LLM_SUCCESS else None
            ),
            **audit,
        )
        if not ok:
            logger.warning(
                f"{self._log_prefix(event)} assistant prepared 写入失败，turn={turn_id}"
            )
        self._note_active_turn(
            turn_id=str(turn_id), cid=str(cid), persona_id=persona_id or "",
            audit=audit, target_id=str(user_id or ""), llm_status=asst_status,
        )
        logger.debug(
            f"{self._log_prefix(event)} bot[{asst_status or 'default'}] -> "
            f"{user_id}@{cid}: {content}"
        )

    @filter.after_message_sent()
    async def mark_send_attempted(self, event: AstrMessageEvent):
        """标记 assistant 已完成 AstrBot 发送流程。

        AstrBot 的 RespondStage 即使捕获平台发送异常也会触发此 Hook，因此状态名严格
        使用 ``send_attempted``，不宣称平台已送达。流式/主动发送若绕过此 Hook，则保持
        ``prepared``，供后续诊断。
        """
        turn_id = event.get_extra("chat_memory_assistant_turn_id")
        if not turn_id:
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not cid:
            return
        await self._safe_update_send_status(umo, cid, turn_id, _SEND_ATTEMPTED)
        # 这一轮如果影子层没有任何 assistant 实发行（被禁言/平台拒收/结果被别的插件清空），
        # 就把计划成员标成"已尝试发送"——判据必须用**影子侧参与行**，不能用上面那条旧表
        # UPDATE 的 rowcount：有计划成员就必定有发送前的 prepared 行，rowcount 恒 ≥1，
        # 用它会永远进不了这个分支（这样 `<cm_unsent/>` 就成了一条不可达的死逻辑）。
        try:
            await self.db.mark_plan_send_attempted(umo, cid, turn_id)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响发送流程
            logger.debug(
                f"{self._log_prefix()} 计划成员发送标记失败: {type(exc).__name__}"
            )
        self._close_active_turn(turn_id=str(turn_id), audit=self._collect_audit_fields(event))
        self._schedule_sent_history_fetch(event)

    # ── 发送侧记账（push 回显的轮次归属依据）────────────

    @staticmethod
    def _turn_key(audit: dict) -> tuple[str, str, str]:
        return (
            str(audit.get("platform_id") or ""),
            str(audit.get("message_type") or ""),
            str(audit.get("session_id") or ""),
        )

    def _turn_ledger(self) -> dict:
        """发送侧记账（懒初始化：__init__ 之外构造的实例/重载路径也不会炸）。"""
        ledger = getattr(self, "_active_turns", None)
        if ledger is None:
            ledger = {}
            self._active_turns = ledger
        return ledger

    def _note_active_turn(
        self, *, turn_id: str, cid: str, persona_id: str = "", audit: dict,
        target_id: str = "", llm_status: str = "",
    ) -> None:
        """装饰完成时记账：这个会话当前这一轮是谁（push 回显据此归属）。

        persona 也要记：回显事件（`message_sent`）本身没有 persona，只能在发送侧记账时
        带过来——不然回显写进去的那一段在按 persona 过滤的读路径里会整段消失（线上 111 行）。
        ``target_id`` 是**被回复的那个人**：回显事件里只有发送者（＝BOT 自己），把它当
        target 会让这一段落不进"当前用户"的读范围（切读后只剩计划全文）。
        """
        if not (turn_id and cid):
            return
        key = self._turn_key(audit)
        if not all(key):
            return
        self._turn_ledger()[key] = {
            "turn_id": turn_id,
            "cid": cid,
            "persona_id": str(persona_id or ""),
            "target_id": str(target_id or ""),
            # 这一轮回复的 LLM 状态：push 回显那一路只有从这里才拿得到
            "llm_status": str(llm_status or ""),
            # opened_at 是兜底过期时间：`after_message_sent` 没来（别的插件 stop_event /
            # hook 抛异常 / 主动发送绕过 hook）时，账不能永远留着——否则这个会话之后所有
            # 回显都会被归到这条老轮次（连 persona 一起错）。
            "opened_at": int(time.time()),
            "closed_at": 0,
        }

    def _close_active_turn(self, *, turn_id: str, audit: dict) -> None:
        """发送流程结束时打时间戳：之后 _ECHO_CLAIM_GRACE_SEC 内的回显仍算本轮。"""
        key = self._turn_key(audit)
        entry = self._turn_ledger().get(key)
        if not entry or str(entry.get("turn_id") or "") != str(turn_id or ""):
            return
        entry["closed_at"] = int(time.time())

    def _claim_turn_for_echo(self, audit: dict) -> dict:
        """回显归属：本会话最近一轮（未关闭，或关闭未超过宽限）→ 返回 turn_id/cid。

        发送是串行的（同一条事件链顺序 await），所以"最近一轮"就是这条回显所属的那一轮；
        不再用正文相等/时间窗去配对，也就不需要为它设计任何字段。
        """
        ledger = self._turn_ledger()
        key = self._turn_key(audit)
        entry = ledger.get(key)
        if not entry:
            return {}
        # 过期判据用 closed_at，没有就用 opened_at：hook 没走完（没来 after_message_sent）
        # 的那条账也有寿命，不会把之后所有回显都吸到这一轮。
        stamp = int(entry.get("closed_at") or entry.get("opened_at") or 0)
        if stamp and int(time.time()) - stamp > _ECHO_CLAIM_GRACE_SEC:
            ledger.pop(key, None)
            return {}
        return {
            "turn_id": str(entry.get("turn_id") or ""),
            "cid": str(entry.get("cid") or ""),
            "persona_id": str(entry.get("persona_id") or ""),
            "target_id": str(entry.get("target_id") or ""),
            "llm_status": str(entry.get("llm_status") or ""),
        }

    # ── 自己发言的平台化（push：订阅平台自身消息回显）──

    # ── 群名（现查，不落库）──

    async def group_name_map(self, *, refresh: bool = False) -> dict[str, str]:
        """返回 ``{群号: 群名}``：OneBot ``get_group_list`` 现查，进程内缓存 6 小时。

        拿不到（平台没连、客户端不支持、没在群里）就返回已有的/空字典——群名只是
        展示用的糖，任何失败都不影响查询台本身。
        """
        cache = getattr(self, "_group_names", None)
        if cache is None:
            cache = self._group_names = GroupNameCache()
        # 库里的快照作为底座：平台没连上/快照库都能把群号翻成名字
        stored = await self._load_stored_group_names()
        if not refresh and (cache.fresh() or cache.cooling()):
            return {**stored, **cache.names}
        calls: list[tuple[str, object]] = []
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception:  # noqa: BLE001 - 平台管理器没就绪：当没有群名
            insts = []
        for inst in insts:
            bot = None
            platform_name = ""
            try:
                meta = inst.meta()
                platform_id = str(getattr(meta, "id", "") or "")
                platform_name = str(getattr(meta, "name", "") or "")
                bot = self._platform_bots.get(platform_id)
            except Exception:  # noqa: BLE001 - 元信息坏了跳过这个实例
                bot = None
            if bot is None:
                try:
                    bot, _ = self._find_onebot_client(inst)
                except Exception:  # noqa: BLE001
                    bot = None
            call = getattr(bot, "call_action", None)
            if callable(call):
                calls.append((platform_name, call))
        if not calls:
            cache.mark_failed()
            return {**stored, **cache.names}
        platform_name, pairs = await _first_group_names(calls)
        if not pairs:
            cache.mark_failed()
            return {**stored, **cache.names}
        cache.update(pairs)
        await self._store_group_names(pairs, platform_name=platform_name)
        return {**stored, **cache.names}

    async def _load_stored_group_names(self) -> dict[str, str]:
        """库里已有的群名快照（拿不到返回空字典）。"""
        try:
            return await self.db.load_group_names()
        except Exception:  # noqa: BLE001 - 群名只是展示用
            return {}

    async def _store_group_names(
        self, pairs: list[tuple[str, str]], *, platform_name: str = ""
    ) -> None:
        """把平台刚给的群名写进快照表（失败只记 DEBUG）。

        ``pairs`` 是 ``[(群号, 群名)]``；``platform_name`` 来自**给出这批结果的那个实例**
        （见 ``_first_group_names``）——用别的实例的名字会把行挂到错误的平台下。
        """
        rows: list[tuple[str, str, str]] = []
        for group_id, name in pairs:
            if group_id and name:
                rows.append((platform_name or "unknown", str(group_id), str(name)))
        if not rows:
            return
        try:
            await self.db.upsert_group_names(rows)
        except Exception as exc:  # noqa: BLE001 - 快照写失败不影响展示
            logger.debug(f"{self._log_prefix()} 群名落库失败: {type(exc).__name__}")

    def _spawn_shadow_task(self, coro) -> None:
        """后台影子写入（状态登记/拉取）：统一登记便于 terminate 取消。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:      # 没有运行中的事件循环：放弃这次登记，绝不外抛
            coro.close()
            return
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def _echo_subscribe_loop(self) -> None:
        """回显订阅巡检：启动立刻订一次，之后周期补齐（平台重载会换 bot 对象）。

        刻意**不做成"收到消息才订阅"**：测试端常常没有真实消息输入，那样等于永远不触发。
        """
        fast_rounds = _ECHO_WATCH_FAST_ROUNDS
        while True:
            try:
                await self._poll_echo_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 巡检不能因为一次异常永久停摆
                logger.warning(
                    f"{self._log_prefix()} 回显订阅巡检异常（下一轮继续）: "
                    f"{type(exc).__name__}"
                )
            delay = (
                _ECHO_WATCH_FAST_INTERVAL_SEC if fast_rounds > 0
                else _ECHO_WATCH_INTERVAL_SEC
            )
            fast_rounds = max(0, fast_rounds - 1)
            await asyncio.sleep(delay)

    @staticmethod
    def _find_onebot_client(inst) -> tuple[Any, str]:
        """从平台实例上找 OneBot 形态的客户端（同时具备 ``on`` 与 ``call_action``）。

        适配器把 CQHttp 挂在 ``self.bot`` 上，但**枚举出来的实例未必是 aiocqhttp 适配器**
        （线上实测：插件加载那一刻只有一个常驻实例、且它没有 bot）。所以除了 ``inst.bot``
        再按能力扫一遍实例属性，并允许用"从事件里学到的 bot"兜底。
        """
        bot = getattr(inst, "bot", None)
        if bot is not None and callable(getattr(bot, "on", None)):
            return bot, "inst.bot"
        try:
            attrs = list(vars(inst).items())
        except Exception:  # noqa: BLE001 - 拿不到属性字典就只认 inst.bot
            attrs = []
        for name, value in attrs:
            if callable(getattr(value, "on", None)) and callable(
                getattr(value, "call_action", None)
            ):
                return value, f"inst.{name}"
        return None, ""

    async def _poll_echo_subscriptions(self) -> int:
        """跑一轮订阅巡检，返回本轮平台实例数（0 表示平台还没加载完）。

        巡检结果**变化时记 INFO**（前 3 轮每轮都记）——没有真实消息输入的环境里，日志是
        唯一能说明"巡检到底看到了什么"的地方（曾经整轮静默循环，看起来像"平台没给回显"，
        实际是我们连实例都没枚举到）。
        """
        insts: list = []
        manager_error = ""
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception as exc:  # noqa: BLE001 - 平台管理器未就绪：下一轮再试
            manager_error = type(exc).__name__
        bot_ready = 0
        subscribed = 0
        no_bot = 0
        for inst in insts:
            try:
                meta = inst.meta()
                platform_id = str(getattr(meta, "id", "") or "")
            except Exception as exc:  # noqa: BLE001 - 元信息坏了不影响别的实例
                logger.debug(f"{self._log_prefix()} 平台元信息读取失败: {type(exc).__name__}")
                continue
            bot, source = None, ""
            if platform_id in self._platform_bots:
                bot, source = self._platform_bots[platform_id], "learned"
            if bot is None:
                bot, source = self._find_onebot_client(inst)
            if bot is not None:
                # 登记下来：卸载时要按这张表摘订阅（巡检路径拿到的 bot 原来不入表，
                # terminate 时就漏摘，旧回调会留在总线上按老代码继续写回显）。
                self._platform_bots[str(platform_id)] = bot
                bot_ready += 1
            else:
                no_bot += 1
            try:
                if self._ensure_echo_subscription(platform_id, bot, source=source):
                    subscribed += 1
                    self._push_subscribed_platforms.add(str(platform_id))
            except Exception as exc:  # noqa: BLE001 - 单个实例失败不影响其它实例
                logger.debug(
                    f"{self._log_prefix()} 订阅巡检异常（platform={platform_id}）: "
                    f"{type(exc).__name__}"
                )
        names: list[str] = []
        for inst in insts:
            try:
                name = str(getattr(inst.meta(), "name", "") or "?")
            except Exception:  # noqa: BLE001 - 名字取不到不影响巡检
                name = "?"
            if name not in names:
                names.append(name)
        if bot_ready and not (getattr(self, "_bot_nicknames", None) or {}):
            # 首轮平台还没就绪时 initialize 那次拿不到，这里补
            try:
                await self._refresh_bot_nicknames()
            except Exception:  # noqa: BLE001 - 拿不到昵称不影响巡检
                pass
        detail = (
            f"insts={len(insts)} bot_ready={bot_ready} no_bot={no_bot} "
            f"subscribed={subscribed} pull={'on' if self.sent_capture_history else 'off'} "
            f"types={','.join(names[:4]) or '-'}"
        )
        if manager_error:
            detail += f" manager_error={manager_error}"
        self._echo_poll_rounds += 1
        self._declare_push_missing(bot_ready=bot_ready)
        if detail != self._echo_poll_last or self._echo_poll_rounds <= 3:
            self._echo_poll_last = detail
            logger.debug(
                f"{self._log_prefix()} 回显订阅巡检（第 {self._echo_poll_rounds} 轮）: {detail}"
            )
        return len(insts)

    def _remember_cid(self, umo: str, cid: str, group_id: str = "") -> None:
        """记住"这个实例在这个会话里的 conversation_id"（push 回显没有事件可读 cid）。

        入库时机：入站捕获、发送路径、拉取通道——凡是我们知道 cid 的地方都记一下。
        """
        if not (umo and cid):
            return
        platform_id, message_type, session_id = self._parse_umo(umo)
        session = session_id or group_id
        if platform_id and message_type and session:
            self._conversation_ids[(platform_id, message_type, session)] = cid

    def _declare_push_missing(self, *, bot_ready: int) -> bool:
        """声明一次"满效率需要 NapCat 回显"（没订上时）。

        只在"有客户端就绪、连续若干轮巡检一个实例都没订上"时打一次 WARNING——这是
        用户唯一能知道"自己发言为什么只剩计划全文"的地方；订上了就永不打扰。
        返回本次是否真的打了（便于自检）。
        """
        if (
            not bot_ready
            or self._push_missing_declared
            or self._echo_poll_rounds < _PUSH_DECLARE_ROUNDS
            or self._push_subscribed_platforms
        ):
            return False
        self._push_missing_declared = True
        logger.warning(
            f"{self._log_prefix()} 未订阅到任何平台自身消息回显（push）：自己发言只能靠"
            f"计划全文 / pull 兜底，效率不满——请确认 NapCat 已启用自身消息上报"
            f"（message_sent）"
        )
        return True

    def _ensure_echo_subscription(
        self, platform_id: str, bot, source: str = ""
    ) -> bool:
        """给平台实例的事件总线订阅自身消息回显（幂等，返回本次是否新订阅）。

        平台重载会重建适配器与 `CQHttp`（总线跟着换），所以标记打在 **bot 对象**上，
        每个入站事件与每次发送都懒检查一次；拿不到 `bot.on` 就静默跳过（退回拉取通道）。
        """
        if not self.shared_store_shadow:
            return False
        if not str(getattr(self, "_echo_owner", "") or ""):
            self._echo_owner = uuid.uuid4().hex
        # 先摘掉上一个实例（重载前的自己）留下的回调：它还挂在总线上，会继续按老代码
        # 处理回显，而我们会因为"标记在 + 有回调"误判成"已经订好了"。
        self._drop_foreign_echo_handlers(platform_id, bot)
        ours = self._echo_handlers(bot)     # 清掉别人的之后还挂着的，就是我们自己的
        if ours:
            # 已订且总线里确实还在：不重复订阅
            return False
        if getattr(bot, _ECHO_SUBSCRIBED_MARK, None):
            # 标记只代表"有人订过"：总线里没有**我们的**回调就清掉，重新订阅
            try:
                setattr(bot, _ECHO_SUBSCRIBED_MARK, False)
            except Exception as exc:  # noqa: BLE001 - 清不掉也要继续尝试订阅
                logger.debug(
                    f"{self._log_prefix()} 清理回显订阅标记失败: {type(exc).__name__}"
                )
        if bot is None:
            # 实例上拿不到 bot（适配器形状不同）：退回拉取通道
            return False
        subscribe = getattr(bot, "on", None)
        if not callable(subscribe):
            # 适配器不是 aiocqhttp / 接口改名：退回拉取通道
            return False
        logger.debug(
            f"{self._log_prefix()} 回显订阅探测: platform={platform_id} "
            f"bot={type(bot).__name__} on={callable(subscribe)} "
            f"bus={hasattr(bot, '_bus')} bus_type={type(getattr(bot, '_bus', None)).__name__}"
        )
        try:
            subscribe(_ECHO_BUS_EVENT)(self._make_echo_handler(platform_id))
            # 标记写上**本实例**的归属 token：下一个实例据此判断"这是别人的订阅"
            setattr(bot, _ECHO_SUBSCRIBED_MARK, str(getattr(self, "_echo_owner", "") or True))
        except Exception as exc:  # noqa: BLE001 - 订阅不上就退回拉取通道
            # 订阅失败＝push 通道整个失效（只剩 pull 兜底），一次失败告警一次
            if platform_id not in self._echo_subscribe_failed:
                self._echo_subscribe_failed.add(platform_id)
                logger.warning(
                    f"{self._log_prefix()} 订阅自身消息回显失败（push 通道不可用，"
                    f"改为只靠发送后拉群历史）: {type(exc).__name__}"
                )
            return False
        logger.info(
            f"{self._log_prefix()} 已订阅平台自身消息回显（platform={platform_id}，"
            f"客户端来源={source or 'inst.bot'}，bot={type(bot).__name__}，"
            f"总线订阅数={self._echo_handler_registered(bot)}；仅用于合并存储，不进流水线）"
        )
        return True

    def _make_echo_handler(self, platform_id: str):
        """回显回调：**绝不向外抛**（总线用 asyncio.gather 并发跑订阅者）。"""

        async def handler(raw) -> None:
            try:
                await self._capture_push_echo(platform_id, raw)
            except Exception as exc:  # noqa: BLE001 - 回显失败不得影响平台连接
                logger.debug(
                    f"{self._log_prefix()} 推送回显处理失败: {type(exc).__name__}"
                )

        handler._chat_memory_echo_handler = True   # noqa: SLF001 - 总线上认领自身订阅
        # 归属标记：重载后旧实例的 bound method 仍挂在总线上（还被总线强引用着），
        # 靠它区分"我们自己的回调"与"上一个实例留下的"。
        handler._chat_memory_echo_owner = str(getattr(self, "_echo_owner", "") or "")
        return handler

    @staticmethod
    def _echo_handlers(bot) -> list:
        """总线上所有带我们标记的回显回调（可能含**上一个实例**留下的）。"""
        bus = getattr(bot, "_bus", None)
        subs = getattr(bus, "_subscribers", None)
        if not isinstance(subs, dict):
            return []
        handlers = subs.get(_ECHO_BUS_EVENT) or ()
        try:
            return [h for h in handlers if getattr(h, _ECHO_HANDLER_MARK, False)]
        except TypeError:      # 不是可迭代集合
            return []

    @staticmethod
    def _echo_owner_of(handler) -> str:
        return str(getattr(handler, "_chat_memory_echo_owner", "") or "")

    def _unsubscribe_echo(self, bot, handler) -> bool:
        """把某个回显回调从总线摘掉（没有退订接口/接口报错时退回直接摘订阅表）。"""
        bus = getattr(bot, "_bus", None)
        unsubscribe = getattr(bus, "unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe(_ECHO_BUS_EVENT, handler)
                return True
            except Exception:  # noqa: BLE001 - 接口形状变了就退回直接摘表
                pass
        subs = getattr(bus, "_subscribers", None)
        if isinstance(subs, dict):
            (subs.get(_ECHO_BUS_EVENT) or set()).discard(handler)
            return True
        return False

    def _drop_foreign_echo_handlers(self, platform_id: str, bot) -> int:
        """摘掉**别的实例**（重载前的自己）留在总线上的回显回调。

        重载不走总线退订：旧实例的 bound method 还挂在 `bot._bus` 上（并因此被总线强引用
        而活着），它继续收事件、继续按**老代码**处理；新实例又因为"标记在 + 总线上有回调"
        直接跳过订阅 → 新代码一条回显都收不到。线上表现：重载后 push 依旧 `no_cid`、
        `origin='echo'` 一行不落，而通道状态写着 subscribed。
        """
        mine = str(getattr(self, "_echo_owner", "") or "")
        dropped = 0
        for handler in self._echo_handlers(bot):
            if mine and self._echo_owner_of(handler) == mine:
                continue
            try:
                if self._unsubscribe_echo(bot, handler):
                    dropped += 1
            except Exception as exc:  # noqa: BLE001 - 摘不掉也不能影响订阅
                logger.debug(
                    f"{self._log_prefix()} 清理旧回显订阅失败: {type(exc).__name__}"
                )
        if dropped:
            logger.info(
                f"{self._log_prefix()} 清理上一个实例留下的回显订阅 {dropped} 个"
                f"（platform={platform_id}）"
            )
        return dropped

    def _release_echo_subscriptions(self) -> int:
        """卸载时摘掉自己的回显回调，交给下一个实例干净接管。

        不摘的后果与上面相反但同样糟：旧回调继续收事件（旧代码处理、重复写），
        而新实例以为"已经订过了"。
        """
        mine = str(getattr(self, "_echo_owner", "") or "")
        if not mine:
            return 0
        released = 0
        for _platform_id, bot in list(getattr(self, "_platform_bots", {}).items()):
            for handler in self._echo_handlers(bot):
                if self._echo_owner_of(handler) != mine:
                    continue
                try:
                    if self._unsubscribe_echo(bot, handler):
                        released += 1
                except Exception:  # noqa: BLE001 - 摘不掉不影响卸载
                    pass
            try:
                if getattr(bot, _ECHO_SUBSCRIBED_MARK, None) == mine:
                    setattr(bot, _ECHO_SUBSCRIBED_MARK, False)
            except Exception:  # noqa: BLE001 - 标记清不掉不影响卸载
                pass
        return released

    def _echo_handler_registered(self, bot) -> int:
        """总线上还挂着几个带我们标记的回显回调（0 表示订阅其实已经没了）。

        幂等标记（``bot._chat_memory_echo_subscribed``）只是"我们订过"的记忆，
        平台重载/总线替换后可能失真；真正的事实要看总线订阅表里还有没有我们的回调。
        """
        return len(self._echo_handlers(bot))

    async def _capture_push_echo(self, platform_id: str, raw) -> bool:
        """把推送回显写成影子行（`origin='inbound'` + 平台 uid，与观察侧同一 msg_uid）。"""
        if not isinstance(raw, dict):
            return False
        if str(raw.get("post_type") or "") != _ECHO_BUS_EVENT:
            # 订阅的是根名 message_sent，理论上只会收到自身消息事件；别的形态直接丢
            logger.debug(
                f"{self._log_prefix()} 回显丢弃：post_type 不符 "
                f"(platform_id={platform_id})"
            )
            return False
        self_id = str(raw.get("self_id") or "")
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id") or raw.get("user_id") or "")
        real_seq = str(raw.get("real_seq") or "")
        if not (self_id and sender_id == self_id):
            # 平台也会上报别的客户端（别的 BOT）的发送事件：不是本实例的发言，不入库
            return False
        if not real_seq:
            # 没有平台共享 id 就没法跟观察侧合并，宁可不写
            logger.debug(
                f"{self._log_prefix()} 回显丢弃：缺平台共享 id "
                f"(platform_id={platform_id})"
            )
            return False
        message_type = _MESSAGE_TYPE_BY_OB11.get(
            str(raw.get("message_type") or ""), ""
        )
        # 群聊取 group_id、私聊取对方 user_id；都没有就不写——回落 sender_id 等于
        # 用 BOT 自己的号当会话 id，会把消息记进一个不存在的会话。
        session_id = str(raw.get("group_id") or raw.get("user_id") or "")
        if not (message_type and session_id):
            logger.debug(
                f"{self._log_prefix()} 回显丢弃：会话类型或 id 缺失 "
                f"(platform_id={platform_id})"
            )
            return False
        umo = f"{platform_id}:{message_type}:{session_id}"
        cid = self._conversation_ids.get((platform_id, message_type, session_id), "")
        if not cid:
            # 内存表还是空的（冷启动/插件刚重载，而回显比入站事件先到）：按会话回查 DB。
            # 线上实测 18/18 条回显都死在这里——只靠内存表等于把 push 通道废掉。
            cid = await self.db.shadow_conversation_id(
                platform_id=platform_id, message_type=message_type, session_id=session_id
            )
            if cid:
                self._conversation_ids[(platform_id, message_type, session_id)] = cid
        if not cid:
            # 这个会话本实例还没有任何记录（真的是第一条）：交给发送后的拉取通道补
            logger.debug(
                f"{self._log_prefix()} 回显丢弃：会话尚无记录 "
                f"(platform_id={platform_id})"
            )
            return False
        platform_name = self._platform_name_of(platform_id)
        rendered = _render_ob11_segments(raw.get("message"))
        kind = rendered["kind"] or ["text"]
        content = rendered["text"] or self._content_placeholder(kind)
        kind, content = _normalize_echo_kind(kind, content)
        # 接收时刻（归一必需）：`normalize_record` 拿不到 rts 就整条拒收，回显会一条不落。
        # 平台时间优先（跨实例一致）；平台没给时间时回落本地接收时刻，宁可时间略偏也不丢消息。
        received_ts = int(raw.get("time") or 0) or int(time.time())
        # 轮次归属：查发送侧记账（push 主通道的全部归属依据，不用正文/时间窗猜）
        audit_fields = {
            "platform_id": platform_id,
            "platform_name": platform_name,
            "message_type": message_type,
            "session_id": session_id,
        }
        claimed = self._claim_turn_for_echo(audit_fields)
        # 回显事件里没有自己的显示名：写行前查一次（有缓存，命中是 dict 读）。
        # 只影响这一条新行，不改动任何历史行。
        own_name = await self._resolve_member_nickname(
            platform_id, message_type, session_id, self_id
        )
        if claimed.get("cid"):
            cid = str(claimed["cid"])
            self._conversation_ids[(platform_id, message_type, session_id)] = cid
        ok = await self._safe_insert_shadow_only(
            umo, cid, claimed.get("target_id") or sender_id, "assistant",
            self._truncate(content),
            content_kind=kind,
                claim_turn=bool(claimed.get("turn_id")),
            turn_id=claimed.get("turn_id") or None,
            persona_id=claimed.get("persona_id") or None,
            post_type="message_sent",
            platform_uid=real_seq,
            platform_timestamp=received_ts,
            raw_timestamp=received_ts,
            at_id=rendered["at_id"],
            reply_id=rendered["reply_id"],
            forward_id=rendered["forward_id"],
            send_status=_SEND_ATTEMPTED,
            llm_status=claimed.get("llm_status") or "",
            platform_id=platform_id,
            platform_name=platform_name,
            message_type=message_type,
            session_id=session_id,
            self_id=self_id,
            group_id=str(raw.get("group_id") or ""),
            sender_nickname=own_name,
        )
        if ok:
            logger.debug(
                f"{self._log_prefix()} 推送回显入库：{len(content)} 字（仅影子表）"
            )
        return ok

    def _platform_name_of(self, platform_id: str) -> str:
        """平台类型名（影子表用）：从平台实例的 meta 取，取不到回落 aiocqhttp。"""
        try:
            inst = self.context.get_platform_inst(platform_id)
            meta = inst.meta() if inst is not None else None
            name = getattr(meta, "name", "") if meta is not None else ""
            if name:
                return str(name)
        except Exception:  # noqa: BLE001 - 元信息拿不到不影响落库
            pass
        return "aiocqhttp"

    # ── 自己发言的平台化（pull：发送后拉群历史）──────

    def _schedule_sent_history_fetch(self, event: AstrMessageEvent) -> None:
        """把"自己实际发出的消息"从平台拉回来（后台任务，失败只记统计与异常）。"""
        if not (self.shared_store_shadow and self.sent_capture_history):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        turn_id = event.get_extra("chat_memory_assistant_turn_id") or ""
        cid = event.get_extra("chat_memory_cid") or ""
        platform_id, _mtype, _sid = self._parse_umo(umo)
        if not (turn_id and cid and platform_id):
            return
        if platform_id in self._sent_history_unsupported:
            return
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        group_id = self._get_group_id(event)
        self_id = self._get_self_id(event)
        if not (callable(call) and group_id and self_id):
            # 非群聊 / 非 OneBot：没有群历史可拉，保留装饰时写的计划全文行
            return
        send_bot = getattr(event, "bot", None)
        if send_bot is not None:
            self._platform_bots[str(platform_id)] = send_bot
        self._ensure_echo_subscription(str(platform_id), send_bot, source="event")
        started_at = int(event.get_extra("chat_memory_assistant_started_at") or 0)
        self._spawn_shadow_task(
            self._fetch_sent_history(
                call=call, umo=umo, cid=cid, group_id=str(group_id), self_id=str(self_id),
                turn_id=str(turn_id), platform_id=str(platform_id),
                llm_status=str(event.get_extra("chat_memory_assistant_llm_status") or ""),
                persona_id=str(event.get_extra("chat_memory_assistant_persona_id") or ""),
                target_id=event.get_sender_id() or "",
                started_at=started_at,
                audit=self._collect_audit_fields(event),
            )
        )

    async def _fetch_sent_history(
        self, *, call, umo: str, cid: str, group_id: str, self_id: str, turn_id: str,
        platform_id: str, llm_status: str, persona_id: str, target_id: str,
        started_at: int, audit: dict,
    ) -> None:
        """拉群历史 → 把其中"自己发的"补进影子表（**push 的兜底通道**，配置启用）。

        写的是**同一条平台行**（同一 real_seq → 同一个 msg_uid），所以不需要任何 pull
        专属字段：段序用平台号，归属用发送侧记账给的 turn_id（`claim_turn=True`）。
        去重靠主键；只接受"上次游标之后"的自己发言，避免旧段被后续轮次反复认领。
        """
        self._remember_cid(umo, cid, group_id)
        cursor = self._sent_history_cursor.get((platform_id, group_id), 0)
        floor = started_at - 5 if started_at else int(time.time()) - _SENT_HISTORY_WINDOW_SEC
        since_ts = max(cursor + 1, floor)
        picked: list[dict] = []
        last_error = ""
        for delay in _SENT_HISTORY_DELAYS:
            await asyncio.sleep(delay)
            try:
                # 客户端不响应时这条影子任务会一直挂着（每次回复一个，缓慢累积），
                # 所以给一个硬上限：超时就当这次拉取失败，下一轮再说。
                data = await asyncio.wait_for(
                    call(_SENT_HISTORY_ACTION, group_id=group_id, count=_SENT_HISTORY_COUNT),
                    timeout=_SENT_HISTORY_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                last_error = "TimeoutError"
                continue
            except Exception as exc:  # noqa: BLE001 - 拉取失败不得影响任何流程
                last_error = type(exc).__name__
                if last_error == "ActionFailed":
                    # 客户端不支持这个接口：记一次，之后不再尝试（避免每次回复都白调）
                    self._sent_history_unsupported.add(platform_id)
                    logger.debug(
                        f"{self._log_prefix()} 平台不支持 {_SENT_HISTORY_ACTION}，"
                        f"自己发出的消息只能保留计划全文行"
                    )
                    return
                continue
            items = data.get("messages") if isinstance(data, dict) else None
            picked = _select_own_history_items(
                items, self_id=self_id, since_ts=since_ts
            )
            if picked:
                break
        if not picked:
            # 通道的失败必须留痕：否则"pull 跑过但一条没拉到"在日志与 DB 里都看不出来
            # （曾经就因为完全静默，把"平台没给历史"误判成"兜底通道没跑"）。
            logger.debug(
                f"{self._log_prefix()} 群历史未拉到自己发言 "
                f"(turn={turn_id}, group={group_id}, error={last_error or '-'})"
            )
            return
        sent_at = int(time.time())
        # 拉回来的同样没有显示名：按会话粒度查一次（不是每条一次），只写进新行
        own_name = await self._resolve_member_nickname(
            str(audit.get("platform_id") or ""),
            str(audit.get("message_type") or ""),
            str(audit.get("session_id") or ""),
            self_id,
        )
        stored = 0
        for item in picked:
            rendered = _render_ob11_segments(item.get("message"))
            kind = rendered["kind"] or ["text"]       # 兜底与 content_kind 保持一致
            content = rendered["text"] or self._content_placeholder(kind)
            ok = await self._safe_insert_shadow_only(
                umo, cid, target_id or "", "assistant", self._truncate(content),
                content_kind=kind,
                post_type="message_sent",   # NapCat 对历史里的自己发言就是这么标的
                platform_uid=str(item.get("real_seq") or ""),
                platform_timestamp=int(item.get("time") or 0) or None,
                raw_timestamp=sent_at,
                turn_id=turn_id,
                claim_turn=True,
                llm_status=llm_status,
                persona_id=persona_id or None,
                send_status=_SEND_ATTEMPTED,
                at_id=rendered["at_id"],
                reply_id=rendered["reply_id"],
                forward_id=rendered["forward_id"],
                platform_id=audit.get("platform_id"),
                platform_name=audit.get("platform_name"),
                message_type=audit.get("message_type"),
                session_id=audit.get("session_id"),
                self_id=self_id,
                group_id=group_id,
                sender_nickname=own_name,
            )
            stored += 1 if ok else 0
            if not ok:
                # 没落库就不要推进游标：推进了这些消息再也不会被兜底通道捞回来
                continue
            try:
                item_ts = int(item.get("time") or 0)
            except (TypeError, ValueError):
                item_ts = 0
            if item_ts:
                self._sent_history_cursor[(platform_id, group_id)] = max(
                    self._sent_history_cursor.get((platform_id, group_id), 0), item_ts
                )
        logger.debug(
            f"{self._log_prefix()} 群历史拉回自己发言 {stored}/{len(picked)} 条 "
            f"(turn={turn_id}, group={group_id})"
        )

    # ── reset / new 处理 ─────────────────────────────

    @staticmethod
    def _get_context_control_text(event: AstrMessageEvent) -> str:
        """取事件文本（``get_message_str`` 失败时回退 ``message_str``）。"""
        try:
            message = event.get_message_str()
        except Exception:
            message = getattr(event, "message_str", "")
        return str(message or "")

    @classmethod
    def _get_context_control_command(cls, event: AstrMessageEvent) -> str:
        """在核心可信清理标志成立后，从事件文本区分 ``reset`` / ``new``。"""
        message = cls._get_context_control_text(event).lower()
        # 词边界匹配：避免 renew / news / preset 之类的子串误判。
        # 只认 AstrBot 自带命令的原文形态（/reset、/new）；平台后缀、全角斜杠等
        # 花式写法不做兼容——识别不出来时上面的钩子会打 WARNING 带原文。
        if re.search(r"(?:^|[\s/])reset(?:$|[\s])", message):
            return "reset"
        if re.search(r"(?:^|[\s/])new(?:$|[\s])", message):
            return "new"
        return ""

    async def _on_reset_or_new(
        self,
        event: AstrMessageEvent,
        umo: str,
        command: str,
    ):
        """按已经严格识别的 ``reset`` / ``new`` 分别处理。

        /reset: CID 不变，清空历史 → 清除该 CID 下所有存档记录（与 AstrBot 语义一致，
                只作用于当前会话；顺带级联清理工具/归档/解析结果与媒体文件）。
        /new:   产生新 CID → 旧 CID 记录保留。
        """
        cid = await self._get_curr_cid(umo)
        if not cid:
            return

        if command == "reset":
            # 直接删除并记录实际数量：一次 DELETE 完成，不再先 COUNT 预查。
            try:
                deleted, media_rows = await self.db.delete_by_conversation(umo, cid)
                await self.media_archiver.delete_files(media_rows)
            except Exception as exc:
                # 删除失败（DB 锁/IO 等）：本地留可诊断日志，用户侧不会"点了没反应还没日志"
                logger.warning(
                    f"{self._log_prefix(event)} /reset 删除失败（CID={cid}）: "
                    f"{type(exc).__name__}"
                )
                return
            if deleted > 0:
                logger.info(
                    f"{self._log_prefix(event)} /reset 完成：清除 CID={cid} "
                    f"下 {deleted} 条存档（不可逆）"
                )
            else:
                logger.debug(
                    f"{self._log_prefix(event)} /reset（CID={cid}），无存档记录可清除"
                )
            await self._log_reset_residue(event, umo, cid)
            await self._log_reset_leftovers(event, umo, cid)
        else:
            logger.debug(f"{self._log_prefix(event)} /new（CID={cid}），新对话开始")

    async def _log_reset_residue(
        self, event: AstrMessageEvent, umo: str, cid: str
    ) -> None:
        """自检：DELETE 之后立刻回查同一 (umo, cid)，把残留按 kind 打出来。

        这行日志能区分两种"reset 后还有消息"：
        - 有残留（WARNING 带 kind，例如 ``["event"]=3``）→ 删除与写入交错，属真实残留；
        - 无残留（DEBUG）→ 之后在界面上看到的记录都是**删除之后**才落库的事件
          （典型：平台重连后补发的旧系统通知），不是没删掉。
        """
        try:
            rows = await self.db.count_records_in_conversation(umo, cid)
        except Exception as exc:
            logger.debug(
                f"{self._log_prefix(event)} /reset 残留自检失败: {type(exc).__name__}"
            )
            return
        if not rows:
            logger.debug(
                f"{self._log_prefix(event)} /reset 残留自检：CID={cid} 已无可见记录"
            )
            return
        total = sum(item["count"] for item in rows)
        detail = ", ".join(
            f"{item['content_kind']}={item['count']}" for item in rows
        )
        logger.warning(
            f"{self._log_prefix(event)} /reset 残留自检：CID={cid} 删除后仍有 "
            f"{total} 条可见记录（按内容类型：{detail}）——可能是删除之后才落库的"
            f"事件（平台补发/并发写入），也可能删除未生效；请把这个连同当时时间反馈"
        )

    async def _log_reset_leftovers(
        self, event: AstrMessageEvent, umo: str, cid: str
    ) -> None:
        """诊断：/reset 只清当前 CID，同 UMO 其它 CID 的记录仍会保留。

        用户看到"reset 后还有消息"时，先看这行 DEBUG 就能判断残留是别的 CID
        （/new 之前的会话、或 Dashboard 删会话留下的）还是真的没删掉。
        """
        try:
            rows = await self.db.count_records_by_conversation(umo, exclude_cid=cid)
        except Exception as exc:
            logger.debug(
                f"{self._log_prefix(event)} 残留检查失败: {type(exc).__name__}"
            )
            return
        if not rows:
            return
        total = sum(item["count"] for item in rows)
        detail = ", ".join(
            f"{item['conversation_id']}={item['count']}" for item in rows
        )
        logger.debug(
            f"{self._log_prefix(event)} /reset 残留检查：同 UMO 其它 CID 仍有 "
            f"{total} 条**可见**存档（{detail}）——/reset 只清当前 CID，这些属历史会话；"
            f"如需彻底清理可在查询台按 CID 逐条软删除"
        )

    # ── 公开实例方法（供 context.get_registered_star 调用）───

    async def query_history(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[dict]:
        """查询会话历史。``user_id`` 为空时返回该会话所有用户的混合记录（群聊场景）。

        ``llm_status`` 支持 str 或 list[str]：按 LLM 状态过滤（list 用 IN）。
        ``content_kind`` 支持 str 或 list[str]；``content_kind_all_match=True``
        时使用严格白名单语义（非空且全部 kind 都在白名单内）。
        ``role_filter`` 给定时仅返回 role 匹配的记录（``'user'`` / ``'assistant'``）。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``（与 takeover 对齐）。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤时间窗口（含端点，tz-aware 自动转 UTC）。
        ``after_id`` 与 ``since`` 同时提供时使用严格 ``(created_at, id)`` 下界。
        ``from_oldest=True`` 时从最旧记录开始截取；返回顺序始终为时间正序。
        """
        return await self._read_with_switch(
            lambda: self.db.query_latest_v2(
                umo, conversation_id, user_id, limit, llm_status, content_kind,
                role_filter, persona_id=persona_id, since=since, until=until,
                from_oldest=from_oldest, after_id=after_id,
                content_kind_all_match=content_kind_all_match,
            ),
            lambda: self.db.query_latest(
                umo, conversation_id, user_id, limit, llm_status, content_kind,
                role_filter, persona_id=persona_id, since=since, until=until,
                from_oldest=from_oldest, after_id=after_id,
                content_kind_all_match=content_kind_all_match,
            ),
        )

    async def query_rounds(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[list[dict]]:
        """按轮次返回 user-assistant 配对。每轮 ``[user_dict, assistant_dict]`` 两条。

        ``llm_status`` / ``content_kind`` 仅过滤 user 侧（assistant 仍按配对字段返回）。
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``。user + assistant 都加。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤（user + assistant 都加）。
        ``after_id`` 与 ``since`` 同时提供时使用严格 ``(created_at, id)`` 下界；
        ``content_kind_all_match=True`` 时使用严格内容白名单语义。
        ``from_oldest=True`` 时从最旧完整轮次开始截取；返回顺序始终为时间正序。
        """
        return await self._read_with_switch(
            lambda: self.db.query_rounds_v2(
                umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
                persona_id=persona_id, since=since, until=until,
                from_oldest=from_oldest, after_id=after_id,
                content_kind_all_match=content_kind_all_match,
            ),
            lambda: self.db.query_rounds(
                umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
                persona_id=persona_id, since=since, until=until,
                from_oldest=from_oldest, after_id=after_id,
                content_kind_all_match=content_kind_all_match,
            ),
        )

    async def build_takeover_contexts(
        self,
        umo: str,
        user_id: str,
        conversation_id: Optional[str] = None,
        persona_id: str = "",
        exclude_turn_id: str = "",
        source: str = "",
    ) -> Optional[list[dict]]:
        """只读构建与当前 context takeover 完全一致的 LLM ``contexts``。

        返回值语义：

        - ``None``：``context_takeover.enable=false``；
        - ``[]``：接管已启用，但会话、用户范围或规整后的记录为空；
        - ``list[dict]``：已按当前接管配置完成查询、前缀增强与边界裁剪。开启
          ``cross_session`` 且结果含其他来源时，其他来源的 user 会带请求内匿名的
          ``<cm_source n="N"/>`` XML 元数据；paired assistant 继承前一条 user 的来源，
          当前会话不加来源元数据。

        本方法不清理 AstrBot native history、不修改请求对象，也不写数据库。调用方未提供
        ``conversation_id`` 时会读取 ``umo`` 的当前 conversation。空 ``user_id`` 仅在
        ``full_group`` 已启用且 ``umo`` 确为群聊时允许；此时即使配置了
        ``cross_session`` 也强制降级为当前 UMO + CID 的整群范围，避免空用户条件扩大到
        整个平台。

        ``source``：空串（默认）跟随切读开关 ``shadow_read``；也可显式传
        ``"v1"`` / ``"v2"`` 覆盖。``"v2"`` 从合并存储（影子层）读取，其余语义完全相同；
        对拍与灰度就靠它（``build_takeover_contexts_v2`` 是同一个入口的等价写法）。
        """
        if not self.ct_enable:
            return None
        if not umo:
            return []

        user_id = str(user_id or "").strip()
        effective_full_group = self.ct_full_group and self._is_group_umo(umo)
        if not user_id and not effective_full_group:
            return []

        cid = conversation_id or await self._get_curr_cid(umo)
        if not cid:
            return []

        requested = self._resolve_read_source(source)
        try:
            return await self._build_contexts_once(
                umo=umo, user_id=user_id, cid=cid, persona_id=persona_id,
                exclude_turn_id=exclude_turn_id, requested_source=requested,
            )
        except Exception as exc:  # noqa: BLE001 - 切读失败按整次请求回滚
            if requested != "v2":
                logger.warning(
                    f"{self._log_prefix()} 接管读取失败（旧表）: {type(exc).__name__}"
                )
                raise
            # 切读失败：**这次请求整段回滚旧表**（历史 / Reply 目标 / 工具段同一个读源）
            await self._read_failed(exc)
            return await self._build_contexts_once(
                umo=umo, user_id=user_id, cid=cid, persona_id=persona_id,
                exclude_turn_id=exclude_turn_id, requested_source="v1",
            )

    async def _build_contexts_once(
        self,
        *,
        umo: str,
        user_id: str,
        cid: str,
        persona_id: str,
        exclude_turn_id: str,
        requested_source: str,
    ) -> list[dict]:
        """按**单一读源**把一次请求的 contexts 拼完（历史 / 引用目标 / 工具段同源）。"""
        effective_full_group = self.ct_full_group and self._is_group_umo(umo)
        records, read_source = await self._takeover_query(
            umo,
            cid,
            user_id,
            persona_id,
            exclude_turn_id=exclude_turn_id,
            force_current_session=not bool(user_id),
            source=requested_source,
        )
        if not records:
            # 工具段必须跟随实际调用轮：CM 无历史即没有轮次可跟随，
            # 即使工具表有记录也不回放（不存在的轮次不配拥有工具上下文）。
            return []

        target_ids_by_umo: dict[str, list[str]] = {}
        for record in records:
            relation = record.get("relation_data")
            reply = relation.get("reply") if isinstance(relation, dict) else None
            if isinstance(reply, dict) and reply.get("resolution") == "turn":
                target_turn_id = str(reply.get("target_turn_id") or "").strip()
                target_umo = self._record_umo(record, fallback_umo=umo) or umo
                if target_turn_id:
                    target_ids_by_umo.setdefault(target_umo, []).append(target_turn_id)

        target_map: dict[tuple[str, str], dict] = {}
        for source_umo, target_ids in target_ids_by_umo.items():
            # 读源必须与历史一致；同源失败就让它抛出去由上层整段回滚，不在这里吞掉
            target_map.update(
                await (self.db.query_turn_targets_v2 if read_source == "v2"
                       else self.db.query_turn_targets)(source_umo, target_ids)
            )

        mixed_mode = set(self.ct_llm_status_filter) != {_LLM_SUCCESS}
        result = self._takeover_normalize(
            records,
            umo,
            max_records=self.ct_limit_rounds if mixed_mode else None,
            max_chars=self.ct_max_context_chars,
            current_user_id=user_id,
            full_group=effective_full_group,
            cross_session=effective_cross_session(
                self.ct_cross_session,
                umo,
                self._blacklist_entries(),
                force_current_session=not bool(user_id),
            ),
            paired_rounds=not mixed_mode,
            target_map=target_map,
        )
        # 工具调用上下文：从 CM 库回放最近 N 轮（assistant tool_calls + role=tool），
        # 按 turn_id 插入对应轮次内部（该轮 user 之后、最终回复之前），与 AstrBot
        # 原生历史顺序一致；**该轮不在当前历史里时整段丢弃**（见 _insert_tool_contexts：
        # 不存在的轮次不配拥有工具上下文，也绝不贴到别处）。工具段属于当前 umo + cid，
        # 不参与 cross_session / full_group 扩大范围，也不进入 user/assistant 配对。
        tool_contexts = await self._query_tool_contexts(umo, cid, source=read_source)
        if tool_contexts:
            result = self._insert_tool_contexts(result, tool_contexts)
        else:
            for context in result:
                context.pop("_turn_id", None)
        return result

    async def query_history_v2(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[dict]:
        """``query_history`` 的 V2：同样参数与语义，数据来自合并存储（影子层）。

        勾上切读（``shadow_read``）后，V1 入口内部也会改读这一层。
        V1 与 V2 并存是为了切读期对拍与回退；两者返回的 dict 同形，V2 额外带
        ``msg_uid`` / ``platform_seq`` / ``segments`` 等影子层字段。
        ``record_id`` 与 ``after_id`` 用影子层的**会话内序号**（V1 是旧表行号）。
        合并存储未启用时抛 ``RuntimeError``（不假装没数据）。
        """
        return await self.db.query_latest_v2(
            umo, conversation_id, user_id, limit, llm_status, content_kind, role_filter,
            persona_id=persona_id, since=since, until=until,
            from_oldest=from_oldest, after_id=after_id,
            content_kind_all_match=content_kind_all_match,
        )

    async def query_rounds_v2(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        llm_status: Optional[Union[str, list[str]]] = None,
        content_kind: Optional[Union[str, list[str]]] = None,
        persona_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        from_oldest: bool = False,
        after_id: Optional[int] = None,
        content_kind_all_match: bool = False,
    ) -> list[list[dict]]:
        """``query_rounds`` 的 V2：每轮 ``[user, assistant]``。

        assistant 侧是**合并后的一轮回复**：该轮实际发出的段按平台号（``platform_seq``）
        顺序拼接（群里看到的样子），该轮没有任何实际形态时才回落到计划全文行。
        ``segments`` / ``segment_count`` 给出拼接来源，便于调用方自行取舍。
        """
        return await self.db.query_rounds_v2(
            umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
            persona_id=persona_id, since=since, until=until,
            from_oldest=from_oldest, after_id=after_id,
            content_kind_all_match=content_kind_all_match,
        )

    async def build_takeover_contexts_v2(
        self,
        umo: str,
        user_id: str,
        conversation_id: Optional[str] = None,
        persona_id: str = "",
        exclude_turn_id: str = "",
    ) -> Optional[list[dict]]:
        """``build_takeover_contexts`` 的 V2：同一套规整/预算逻辑，改从合并存储取数。"""
        return await self.build_takeover_contexts(
            umo, user_id, conversation_id, persona_id, exclude_turn_id, source="v2"
        )

    # ── 内部工具 ──────────────────────────────────────

    async def _safe_insert(
        self, umo: str, cid: str, user_id: str, role: str, content: str,
        message_id: Optional[str] = None, pair_id: Optional[str] = None,
        llm_status: str = _LLM_DEFAULT, content_kind: Optional[list[str]] = None,
        platform_id: Optional[str] = None, platform_name: Optional[str] = None,
        message_type: Optional[str] = None, session_id: Optional[str] = None,
        self_id: Optional[str] = None, group_id: Optional[str] = None,
        sender_nickname: Optional[str] = None, raw_timestamp: Optional[int] = None,
        at_id: Optional[str] = None, reply_id: Optional[str] = None,
        forward_id: Optional[str] = None, persona_id: Optional[str] = None,
        turn_id: Optional[str] = None, send_status: str = "",
        relation_data: Optional[str] = None,
        update_user_llm_status: Optional[str] = None,
        platform_timestamp: Optional[int] = None,
        platform_uid: Optional[str] = None,
        post_type: str = "",
        shadow_only: bool = False,
    ) -> bool:
        try:
            # 返回值＝旧表是否真的插入了行：INSERT OR IGNORE 命中唯一键时是空操作，
            # 静默当成功会让"这一轮其实没写进去"完全看不见（assistant 还会升级状态）。
            inserted = await self.db.insert(
                umo, cid, user_id, role, content, message_id, pair_id,
                llm_status=llm_status, content_kind=content_kind,
                platform_id=platform_id, platform_name=platform_name,
                message_type=message_type, session_id=session_id,
                self_id=self_id, group_id=group_id, sender_nickname=sender_nickname,
                raw_timestamp=raw_timestamp,
                at_id=at_id, reply_id=reply_id, forward_id=forward_id,
                persona_id=persona_id,
                turn_id=turn_id, send_status=send_status,
                relation_data=relation_data,
                update_user_llm_status=update_user_llm_status,
                platform_timestamp=platform_timestamp,
                platform_uid=platform_uid,
                post_type=post_type,
                shadow_only=shadow_only,
            )
            return bool(inserted)
        except Exception as e:
            # 只记异常类型：SQLAlchemy 异常串会带出 SQL 参数（= 聊天正文
            # / 媒体元信息），WARNING 不承载内容，细节由 DB 层启动日志覆盖
            logger.warning(f"{self._log_prefix()} 写入失败: {type(e).__name__}")
            return False

    async def _safe_insert_shadow_only(
        self, umo: str, cid: str, user_id: str, role: str, content: str, **kwargs
    ) -> bool:
        """影子专用写入（不碰旧表）：供平台回显与实际逐条发送使用。

        返回影子行是否**真的**落库：归一被判数据不足（如缺接收时刻）时 insert 返回假，
        这里必须原样上报，否则调用方会把"跳过"记成成功（线上回显曾如此静默丢整条通道）。
        """
        kwargs.setdefault("post_type", "message_sent")
        try:
            return bool(
                await self.db.insert(
                    umo, cid, user_id, role, content, shadow_only=True, **kwargs
                )
            )
        except Exception as e:  # noqa: BLE001 - 与主写入同款隔离
            logger.warning(
                f"{self._log_prefix()} 影子专用写入失败: {type(e).__name__}"
            )
            return False

    async def _safe_update_llm_status_by_turn(
        self, umo: str, cid: str, turn_id: str, new_status: str,
    ) -> int:
        try:
            return await self.db.update_llm_status_by_turn(umo, cid, turn_id, new_status)
        except Exception as e:
            logger.warning(
                f"{self._log_prefix()} 按 turn_id 更新 llm_status 失败: "
                f"{type(e).__name__}"
            )
            return 0

    async def _safe_update_send_status(
        self, umo: str, cid: str, turn_id: str, new_status: str,
    ) -> int:
        try:
            return await self.db.update_send_status(umo, cid, turn_id, new_status)
        except Exception as e:
            logger.warning(
                f"{self._log_prefix()} 更新 send_status 失败: {type(e).__name__}"
            )
            return 0

    # ── 生命周期终止 ─────────────────────────────────

    async def terminate(self):
        """AstrBot 卸载/重载时调用：取消未完成任务 + 释放 DB 连接池。

        热重载场景下若不显式 dispose，aiosqlite 连接与 SQLAlchemy 引擎会泄漏，
        多次重载后可能耗尽文件描述符。
        """
        # 0. 摘掉自己的回显订阅：总线不会随插件卸载自动退订，留着旧回调会让下一个实例
        #    "看起来已订阅"而永远收不到事件（线上踩过）
        try:
            released = self._release_echo_subscriptions()
            if released:
                logger.info(
                    f"{self._log_prefix()} 已摘除自身消息回显订阅 {released} 个（交给下一个实例）"
                )
        except Exception as exc:  # noqa: BLE001 - 退订失败不影响卸载
            logger.debug(f"{self._log_prefix()} 摘除回显订阅失败: {type(exc).__name__}")

        # 0a. 取消登录昵称任务（不取消会在卸载后继续跑，重载时变成悬挂任务）
        try:
            task = getattr(self, "_nickname_task", None)
            if task is not None and not task.done():
                task.cancel()
        except Exception as exc:  # noqa: BLE001 - 取消失败不影响卸载
            logger.debug(f"{self._log_prefix()} 取消昵称任务失败: {type(exc).__name__}")

        # 0b. 查询台只读连接的关闭放在**后台任务取消/等待之后**（见 2.1）：
        #     预热走的是 asyncio.to_thread，线程无法被 cancel 中断；先关连接会让它继续用
        #     一个已关闭的 sqlite 连接（thread 内报错虽不外抛，但会把缓存写坏）。

        # 1. 取消周期清理 task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{self._log_prefix()} 清理 task 停止异常: {e}")
            self._cleanup_task = None

        # 2. 停止媒体归档（取消后台下载 worker，清 .tmp）与媒体清理 task
        if self._echo_watch_task and not self._echo_watch_task.done():
            self._echo_watch_task.cancel()
            try:
                await self._echo_watch_task
            except asyncio.CancelledError:
                pass
            self._echo_watch_task = None
        for task in list(self._shadow_tasks):
            if not task.done():
                task.cancel()
        if self._shadow_tasks:
            await asyncio.gather(*self._shadow_tasks, return_exceptions=True)
            self._shadow_tasks.clear()
        if self._media_cleanup_task and not self._media_cleanup_task.done():
            self._media_cleanup_task.cancel()
            try:
                await self._media_cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{self._log_prefix()} 媒体清理 task 停止异常: {e}")
            self._media_cleanup_task = None
        try:
            await self.media_archiver.stop()
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 媒体归档停止异常: {e}")

        # 2.1 关掉查询台复用的只读连接（旧历史去重缓存在它的 TEMP 表里）：不关的话
        #     重载后旧库文件一直被持有，Windows 上换库/删库会失败。
        #     放在后台任务之后：预热线程不可中断，先关会让它继续用已关闭的连接。
        try:
            if self.ui_repository is not None:
                self.ui_repository.close()
        except Exception as exc:  # noqa: BLE001 - 关连接失败不影响卸载
            logger.debug(f"{self._log_prefix()} 关闭查询台连接失败: {type(exc).__name__}")

        # 3. 释放 DB 连接池（关键写入均直接 await，不再维护后台写入任务）
        try:
            await self.db.engine.dispose()
        except Exception as e:
            logger.warning(f"{self._log_prefix()} engine.dispose 异常: {e}")

        logger.info(f"{self._log_prefix()} 已终止")
