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
    / ``'face'`` / ``'forward'`` / ``'system_event'``
  - ``[]`` 空数组 = empty（如纯 @ 无文字、纯 Reply 无文字）
  - ``'at'`` / ``'reply'`` 不入 content_kind，用独立字段 ``at_id`` / ``reply_id`` 表达

assistant 配对：``pair_id`` = 对应 user 的 ``message_id``；平台无 mid 时 NULL，无法配对。
"""

import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import (
    Plain, Image, Video, Record, File, Face, At, AtAll, Reply, Forward,
)

from .storage import DBManager

# ── llm_status 取值 ─────────────────────────────────
_LLM_DEFAULT = ""              # 默认（未走 LLM）
_LLM_PENDING = "llm_pending"   # LLM 触发但 assistant 未成功
_LLM_SUCCESS = "llm_success"   # LLM 路径且成功
_LLM_PROACTIVE = "proactive"   # 主动消息（assistant 单边）
_LLM_ORPHAN = "orphan"         # user 漏存

# ── content_kind 取值 ───────────────────────────────
_K_TEXT = "text"
_K_IMAGE = "image"
_K_VIDEO = "video"
_K_VOICE = "voice"
_K_FILE = "file"
_K_FACE = "face"
_K_FORWARD = "forward"
_K_SYSTEM = "system_event"

# 接管时需过滤的媒体 kind 集合（不含 system_event / 空数组）
_MEDIA_KINDS = {_K_IMAGE, _K_VIDEO, _K_VOICE, _K_FILE, _K_FACE, _K_FORWARD}

# terminate 时给 fire-and-forget task 的 flush 窗口（秒）：保护在写的 assistant 记录
_TERMINATE_FLUSH_TIMEOUT = 5.0


class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.max_len = config.get("max_content_length", 500)
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)

        log_conf = config.get("log_config", {})
        self.log_with_bot_id = log_conf.get("log_with_bot_id", False)
        self.debug_to_info = log_conf.get("debug_to_info", False)

        ct_conf = config.get("context_takeover", {}) or {}
        self.ct_enable = bool(ct_conf.get("enable", False))
        self.ct_cross_session = bool(ct_conf.get("cross_session", False))
        self.ct_full_group = bool(ct_conf.get("full_group", False))
        # limit_rounds 钳到 [1, 100]：负数会让 SQLite LIMIT -1 等价无限制
        self.ct_limit_rounds = max(1, min(100, int(ct_conf.get("limit_rounds", 30))))
        self.ct_clear_native_history = bool(ct_conf.get("clear_native_history", True))
        ct_status = ct_conf.get("llm_status_filter", ["llm_success"])
        # "no_llm" 是 UI 占位符，DB 实际值是空串 ""
        ct_status_list = list(ct_status) if ct_status else ["llm_success"]
        self.ct_llm_status_filter = ["" if s == "no_llm" else s for s in ct_status_list]
        self.ct_prefix_enhance = str(ct_conf.get("prefix_enhance", "time_sender"))
        # Kind 白名单：选中=需要；默认 ["text"]；空集合 = 不过滤（全部进入）
        self.ct_include_kinds: set[str] = set(ct_conf.get("include_content_kinds", ["text"]) or [])
        # ALL 模式：content_kind 必须 ⊆ 白名单（且非空）；False = ANY（任一交集即进）
        self.ct_include_all_match = bool(ct_conf.get("include_all_match", False))
        # persona 过滤：开启后查询严格按当前 persona_id 过滤；persona_id 为空时跳过（兜底）
        # 与 cross_session=T 协同可获完整 persona 隔离体验（切 persona + /new + 切回仍可见旧数据）
        self.ct_filter_by_persona = bool(ct_conf.get("filter_by_persona", False))

        # 读取 AstrBot 全局时区配置（IANA 名称如 "Asia/Shanghai"），用于 created_at 生成
        try:
            tz_name = context.get_config().get("timezone", "Asia/Shanghai")
            self._tz = ZoneInfo(tz_name)
        except Exception:
            self._tz = ZoneInfo("Asia/Shanghai")

        data_dir = Path(context.get_config().get("plugin.data_dir", "./data")) / "plugin_data" / "chat_memory"
        self.db = DBManager(data_dir)

        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_started = False
        # fire-and-forget 任务的引用保活：CPython 不保证无引用 task 不被 GC
        self._pending_tasks: set[asyncio.Task] = set()

        cleanup_desc = (
            f"自动清理 {self.auto_cleanup_days} 天前的记录"
            if self.auto_cleanup_days > 0
            else "自动清理关闭"
        )
        logger.info(f"[ChatMemory] 对话记录存档已启用（{cleanup_desc}）")

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

    # ── fire-and-forget 任务管理 ─────────────────────

    def _spawn_tasks(self, *coros) -> None:
        """并发调度多个协程并保活引用。

        asyncio.create_task 创建的任务若无人持有引用，可能在完成前被 GC
        （CPython 实现细节，3.11+ 相对稳定但仍不保证）。这里维护一个 set，
        done callback 中自动清理，避免静默丢失。
        """
        for coro in coros:
            task = asyncio.create_task(coro)
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

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
                cutoff = datetime.now() - timedelta(days=self.auto_cleanup_days)
                try:
                    deleted = await self.db.delete_old(cutoff)
                    if deleted > 0:
                        logger.info(
                            f"{self._log_prefix()} 自动清理：删除 {deleted} 条 "
                            f"早于 {self.auto_cleanup_days} 天的记录"
                        )
                    else:
                        self._log(f"{self._log_prefix()} 自动清理：本轮无可清理记录")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"{self._log_prefix()} 自动清理失败: {e}")
        except asyncio.CancelledError:
            pass

    # ── 日志/工具辅助 ───────────────────────────────────

    def _log_prefix(self, event=None) -> str:
        if self.log_with_bot_id and event is not None:
            try:
                return f"[ChatMemory:{event.get_platform_id()}]"
            except Exception:
                pass
        return "[ChatMemory]"

    def _truncate(self, text: str) -> str:
        if self.max_len <= 0:
            return text
        return text[:self.max_len]

    def _now(self) -> datetime:
        """返回当前时刻的 naive datetime（按 AstrBot 配置时区）。"""
        return datetime.now(self._tz).replace(tzinfo=None)

    @staticmethod
    def _strip_reasoning_prefix(text: str) -> str:
        """剥离 AstrBot 错误序列化的 reasoning parts 前缀。

        AstrBot 部分 Provider（reasoning 模型如 GLM/DeepSeek/o1）会把 content parts 列表
        ``[{'type': 'think', 'content': '...', 'encrypted': None}]`` 整体 str() 后塞到 Plain
        组件，紧跟实际回复文本。这里字符级跟踪括号平衡（处理字符串/转义）找到列表结束位置，
        返回剩余部分。

        非该前缀格式直接返回原文；解析失败也返回原文（保守）。
        """
        if not text.startswith("[{'type': 'think'"):
            return text
        depth = 0
        i = 0
        n = len(text)
        in_str = False
        quote = ""
        while i < n:
            c = text[i]
            if in_str:
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == quote:
                    in_str = False
                i += 1
                continue
            if c in ("'", '"'):
                in_str = True
                quote = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[i + 1:].lstrip()
            i += 1
        return text

    def _log(self, msg: str):
        if self.debug_to_info:
            logger.info(msg)
        else:
            logger.debug(msg)

    @staticmethod
    def _extract_text(event: AstrMessageEvent) -> str:
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

    @staticmethod
    def _classify_content(event: AstrMessageEvent) -> tuple[list[str], Optional[str], Optional[str], Optional[str]]:
        """从消息组件链提取内容形态分类 + 上下文引用字段。

        返回 ``(content_kind, at_id, reply_id, forward_id)``：
        - ``content_kind``：去重后的 kind 列表（保持首次出现顺序）
        - ``at_id`` / ``reply_id`` / ``forward_id``：对应组件的 ID（无则 None）

        组件白名单：Plain/Image/Video/Record/File/Face/Forward 入 content_kind；
        At/Reply 不入 kind，单独提取 ID；其他组件（AtAll/Poke/Json/Unknown 等）忽略。

        OTHER_MESSAGE 类型（poke / 请求 / 通知）：强制 content_kind=['system_event']，覆盖其他形态。
        """
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []
        kind: list[str] = []
        at_id: Optional[str] = None
        reply_id: Optional[str] = None
        forward_id: Optional[str] = None

        def _push(k: str):
            if k not in kind:
                kind.append(k)

        for comp in chain:
            if isinstance(comp, Plain):
                if (comp.text or "").strip():
                    _push(_K_TEXT)
            elif isinstance(comp, Image):
                _push(_K_IMAGE)
            elif isinstance(comp, Video):
                _push(_K_VIDEO)
            elif isinstance(comp, Record):
                _push(_K_VOICE)
            elif isinstance(comp, File):
                _push(_K_FILE)
            elif isinstance(comp, Face):
                _push(_K_FACE)
            elif isinstance(comp, Forward):
                _push(_K_FORWARD)
                if forward_id is None:
                    fid = getattr(comp, "id", None)
                    if fid:
                        forward_id = str(fid)
            elif isinstance(comp, AtAll):
                # @全体成员：不入 at_id（避免 caller 误以为是 at 某个 ID）
                pass
            elif isinstance(comp, At):
                if at_id is None:
                    qq = getattr(comp, "qq", None)
                    if qq:
                        at_id = str(qq)
            elif isinstance(comp, Reply):
                if reply_id is None:
                    rid = getattr(comp, "id", None)
                    if rid:
                        reply_id = str(rid)
            # 其他组件（AtAll / Poke / Json / Unknown 等）忽略

        # 回退：AstrBot 部分 Provider/适配器把 user 文本放在 event.message_str，
        # message 组件链里没有非空 Plain（或 chain 为空）。此时若已无任何 kind，
        # 但 message_str 非空，则补 text（与 _extract_text 的回退逻辑对齐）。
        if not kind:
            msg_str = (getattr(event, "message_str", "") or "").strip()
            if msg_str:
                kind.append(_K_TEXT)

        # OTHER_MESSAGE（poke / 加好友请求 / 通知等）：强制 system_event
        try:
            mt = event.get_message_type()
            mt_value = getattr(mt, "value", str(mt))
            if mt_value == "OtherMessage":
                kind = [_K_SYSTEM]
        except Exception:
            pass

        return kind, at_id, reply_id, forward_id

    @staticmethod
    def _classify_assistant_chain(chain) -> tuple[list[str], str]:
        """从 BOT 回复组件链提取 ``(content_kind, text)``。

        与 user 端 ``_classify_content`` 的差异：
        - 不提取 ``at_id`` / ``reply_id`` / ``forward_id``（bot 主动回复一般不携带这些）
        - 不做 ``message_str`` 回退（``result.chain`` 即为真相源）
        - 不做 ``OtherMessage`` 强制覆盖（result 必然是 assistant 输出）

        返回 ``(kind, text)``：
        - ``kind``：去重后保留首次出现顺序；空列表表示完全空 chain
        - ``text``：所有 Plain 组件拼接并 strip
        """
        kind: list[str] = []
        parts: list[str] = []

        def _push(k: str):
            if k not in kind:
                kind.append(k)

        for comp in chain:
            if isinstance(comp, Plain):
                if (comp.text or "").strip():
                    _push(_K_TEXT)
                    parts.append(comp.text)
            elif isinstance(comp, Image):
                _push(_K_IMAGE)
            elif isinstance(comp, Video):
                _push(_K_VIDEO)
            elif isinstance(comp, Record):
                _push(_K_VOICE)
            elif isinstance(comp, File):
                _push(_K_FILE)
            elif isinstance(comp, Face):
                _push(_K_FACE)
            elif isinstance(comp, Forward):
                _push(_K_FORWARD)
            # At / AtAll / Reply / Poke / Json / Unknown 等：assistant 端忽略
        return kind, "".join(parts).strip()

    @staticmethod
    def _content_placeholder(kind: list[str]) -> str:
        """非文本内容的占位字符串（取第一个 kind）。"""
        if not kind:
            return ""
        return f"[{kind[0]}]"

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
            cfg = self.context.get_config(umo=umo).get("provider_settings", {})
            resolved, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=cfg,
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

    def _collect_audit_fields(self, event: AstrMessageEvent) -> dict:
        """从 event 提取审计/上下文字段，供 INSERT 使用。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        platform_id, message_type, session_id = self._parse_umo(umo)
        return {
            "platform_id": platform_id,
            "platform_name": self._get_platform_name(event),
            "message_type": message_type,
            "session_id": session_id,
            "self_id": self._get_self_id(event),
            "group_id": self._get_group_id(event),
            "sender_nickname": self._get_sender_nickname(event),
            "raw_timestamp": self._get_raw_timestamp(event),
        }

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
            self._log(f"{self._log_prefix(event)} 跳过 user 捕获：umo 或 user_id 为空")
            return False

        try:
            if user_id == event.get_self_id():
                self._log(f"{self._log_prefix(event)} 跳过 user 捕获：BOT 自身消息")
                return False
        except Exception:
            pass

        # cron 平台：跳过 user capture 且不标 attempted → capture_bot 走 proactive 分支
        if self._get_platform_name(event) == "cron":
            self._log(f"{self._log_prefix(event)} cron 平台，跳过 user 捕获（assistant 将标 proactive）")
            return False

        # 以下路径都是"真正尝试过 capture"：cid 未就绪、内容空、写库失败都属 orphan
        if not event.get_extra("chat_memory_capture_attempted"):
            event.set_extra("chat_memory_capture_attempted", True)

        cid = await self._get_curr_cid(umo)
        if not cid:
            self._log(f"{self._log_prefix(event)} 跳过 user 捕获：cid 暂未创建（首条消息可能漏存）")
            return False

        kind, at_id, reply_id, forward_id = self._classify_content(event)
        user_text = self._extract_text(event)

        # content 决定：有文本用文本；否则用占位；empty（[] + 无引用字段）用空串
        if user_text:
            content = user_text
        elif kind:
            content = self._content_placeholder(kind)
        else:
            content = ""

        # empty 且无任何引用字段：什么都没存，跳过
        if not content and at_id is None and reply_id is None and forward_id is None:
            self._log(f"{self._log_prefix(event)} 跳过 user 捕获：消息完全为空")
            return False

        msg_id = self._get_message_id(event)
        no_mid = not msg_id

        # 取当前生效 persona_id 缓存到 extras，capture_bot 复用避免二次查询
        persona_id = await self._get_effective_persona(umo, event, cid)
        event.set_extra("chat_memory_persona_id", persona_id)

        audit = self._collect_audit_fields(event)
        ok = await self._safe_insert(
            umo, cid, user_id, "user", self._truncate(content),
            message_id=msg_id or None, pair_id=None,
            llm_status=_LLM_DEFAULT, content_kind=kind,
            at_id=at_id, reply_id=reply_id, forward_id=forward_id,
            persona_id=persona_id or None,
            created_at=self._now(),
            **audit,
        )
        if not ok:
            logger.warning(f"{self._log_prefix(event)} user 写入失败，extras 未标记，assistant 将标 orphan")
            return False

        kind_repr = "/".join(kind) if kind else "empty"
        ref_repr = []
        if at_id: ref_repr.append(f"at={at_id[:8]}")
        if reply_id: ref_repr.append(f"reply={reply_id[:8]}")
        if forward_id: ref_repr.append(f"fwd={forward_id[:8]}")
        ref_str = f"[{','.join(ref_repr)}]" if ref_repr else ""
        self._log(
            f"{self._log_prefix(event)} user[{msg_id[:8] or '-'}][{kind_repr}]{ref_str} -> "
            f"{user_id}@{cid[:8]}: {content[:60]}"
        )

        event.set_extra("chat_memory_captured", True)
        event.set_extra("chat_memory_cid", cid)
        event.set_extra("chat_memory_user_msg_id", msg_id)
        event.set_extra("chat_memory_llm_triggered", False)
        event.set_extra("chat_memory_no_mid", no_mid)
        return True

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def capture_user(self, event: AstrMessageEvent):
        """所有进入 ProcessStage 的 user 消息立即落库（默认 llm_status=''）。"""
        await self._ensure_cleanup_started()
        await self._capture_user_internal(event)

    # ── LLM 触发标记 + 兜底捕获 ──────────────────────

    @filter.on_llm_request()
    async def mark_llm_triggered(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 调用时：兜底重试 user 捕获 + 把 user.llm_status 从 '' 改成 'llm_pending'。

        平台无 mid 时跳过升级（无法用 message_id 定位行）。
        """
        if not event.get_extra("chat_memory_captured"):
            self._log(f"{self._log_prefix(event)} LLM 触发，补捕获 user（首条消息兜底）")
            ok = await self._capture_user_internal(event)
            if not ok:
                logger.warning(f"{self._log_prefix(event)} LLM 触发但 user 捕获失败，放弃 llm_status 更新")
                return

        umo = getattr(event, "unified_msg_origin", "")
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not umo or not cid:
            return

        event.set_extra("chat_memory_llm_triggered", True)

        # 平台无 mid 时跳过升级（无法用 message_id 定位）
        if event.get_extra("chat_memory_no_mid"):
            self._log(f"{self._log_prefix(event)} user 保持 ''（平台无 mid，跳过 llm_pending 升级）")
            return

        msg_id = event.get_extra("chat_memory_user_msg_id")
        if not msg_id:
            return

        await self._safe_update_llm_status(umo, cid, msg_id, _LLM_PENDING)
        self._log(f"{self._log_prefix(event)} user[{msg_id[:8]}] llm_status -> llm_pending")

    # ── 上下文接管 ───────────────────────────────────

    @filter.on_llm_request(priority=-100)
    async def take_over_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """接管 req.contexts，注入 CM 数据 + 清空 native history。

        priority=-100 确保最后执行（AstrBot 高值先执行），覆盖其他插件对 contexts 的修改。
        与 mark_llm_triggered(默认 0) 顺序：先标记 llm_pending，后接管（CM 已落库再读取）。
        """
        if not self.ct_enable:
            return

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        cid = await self._get_curr_cid(umo)
        if not cid:
            self._log(f"{self._log_prefix(event)} 接管跳过：cid 未就绪（首条消息）")
            return

        user_id = event.get_sender_id() or ""
        persona_id = ""
        if self.ct_filter_by_persona:
            persona_id = await self._get_effective_persona(umo, event, cid)
            if not persona_id:
                logger.warning(
                    f"{self._log_prefix(event)} filter_by_persona=True 但当前生效 persona_id 为空，"
                    f"将仅匹配 persona_id IS NULL OR '' 的记录（老数据/未分配 persona 的消息）"
                )
        records = await self._takeover_query(umo, cid, user_id, persona_id)
        if not records:
            self._log(f"{self._log_prefix(event)} 接管跳过：CM 无数据")
            return

        contexts = self._takeover_normalize(records, umo)
        if not contexts:
            self._log(f"{self._log_prefix(event)} 接管跳过：规整后为空")
            return

        req.contexts = contexts

        if self.ct_clear_native_history:
            self._spawn_tasks(self._safe_reset_history(umo, cid))

        self._log(
            f"{self._log_prefix(event)} 接管 contexts={len(contexts)} "
            f"(cross_session={self.ct_cross_session}, full_group={self.ct_full_group}, "
            f"cid={cid[:8]})"
        )

    async def _takeover_query(
        self, umo: str, cid: str, user_id: str, persona_id: str = "",
    ) -> list[dict]:
        """按 cross_session / full_group / 配对模式 查询 CM 数据，返回扁平化 records 列表。

        两种查询模式：
        - **配对模式**（仅 llm_success）：用 ``query_rounds_raw`` 查配对轮次，按轮数切片
        - **混合模式**（含其他状态）：用 ``query_messages_raw`` 查全量，按条数切片

        ``limit_rounds`` 含义随模式变化：
        - 配对模式 → 轮数（user-assistant 一对为一轮）
        - 混合模式 → 消息数（单条记录）

        ``persona_id``：仅当 ``ct_filter_by_persona=True`` 时由调用方填入（取自
        ``req.conversation.persona_id``）；为空时 storage 层跳过 persona 过滤。
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
        target_cid: Optional[str] = None if self.ct_cross_session else cid
        cross_umo = self.ct_cross_session

        try:
            if is_pair_only:
                # 配对模式：按轮数
                rounds = await self.db.query_rounds_raw(
                    umo, target_cid, user_id, limit, include_kinds, all_match,
                    cross_umo=cross_umo, full_group=effective_full_group,
                    persona_id=persona_id, filter_by_persona=filter_by_persona,
                )
                records: list[dict] = [msg for rnd in rounds for msg in rnd]
            else:
                # 混合模式：按消息数；overfetch 2x 给规整留余地（防先 LIMIT 后过滤导致空上下文）
                # 规整阶段会丢头部 assistant / 尾部 solo / 纯媒体；overfetch 后再在 normalize 截到目标条数
                records = await self.db.query_messages_raw(
                    umo, target_cid, user_id, limit * 2, status_set, include_kinds, all_match,
                    cross_umo=cross_umo, full_group=effective_full_group,
                    persona_id=persona_id, filter_by_persona=filter_by_persona,
                )

            # 防御性全局排序（混合模式下 messages_raw 已排序，但保持一致）
            if records:
                records.sort(key=lambda r: r.get("created_at") or "")
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 接管查询失败: {e}")
            return []

        return records

    def _takeover_normalize(self, records: list[dict], umo: str) -> list[dict]:
        """规整流水线：

        过滤纯媒体 → user/solo-asst 加前缀 → 合并同 role（solo 自成一条）→
        丢头部非 user → 丢尾部 solo assistant → 标 _no_save。

        - **纯媒体过滤**：``content_kind`` 全是媒体 kind 的丢；图文混合（['text','image']）保留文本部分。
        - **user 前缀**：``[MM/DD HH:MM:SS] Sender:``（按 prefix_enhance 配置）。
        - **solo assistant 前缀**：proactive/orphan 这类单边 assistant 加 ``[主动]``/``[未配对]`` 标识，
          可选带时间戳；让 LLM 知道"这是 bot 单方面说的，没有前置 user"。
        - **合并**：连续同 role + 同 solo 标记的合并；solo 与非 solo 不合并（语义不同）。
        - **丢尾部 solo assistant**：OpenAI 格式要求 messages 末尾不能是 assistant 单边（无对应 user），
          否则 LLM 困惑"我刚主动说完话怎么又来 user"。末尾若是有配对的 llm_success assistant 保留（agent runner 追加 user_now 后合法）。
        """
        # 过滤纯媒体（图文混合保留）
        records = [r for r in records if not self._is_pure_media(r)]

        formatted: list[dict] = []
        for r in records:
            content = self._strip_reasoning_prefix(r.get("content", "") or "")
            role = r.get("role", "user")
            llm_status = r.get("llm_status", "")
            is_solo = role == "assistant" and llm_status in (_LLM_PROACTIVE, _LLM_ORPHAN)

            if role == "user":
                content = self._apply_prefix(r, content)
            elif is_solo:
                tag = "主动" if llm_status == _LLM_PROACTIVE else "未配对"
                content = self._apply_solo_prefix(r, content, tag)

            formatted.append({"role": role, "content": content, "_solo": is_solo})

        formatted = self._merge_with_solo(formatted)

        # 丢头部非 user
        while formatted and formatted[0]["role"] != "user":
            formatted.pop(0)

        # 丢尾部 solo assistant（proactive/orphan 这种单边不能结尾）
        while formatted and formatted[-1]["role"] == "assistant" and formatted[-1].get("_solo"):
            formatted.pop()

        # 清理临时字段 + 标 _no_save
        for c in formatted:
            c.pop("_solo", None)
            c["_no_save"] = True

        return formatted

    def _apply_prefix(self, record: dict, content: str) -> str:
        """按 prefix_enhance 模式给 user content 加前缀。"""
        mode = self.ct_prefix_enhance
        if mode == "off" or not content:
            return content

        parts: list[str] = []
        if mode in ("time", "time_sender"):
            time_str = self._extract_time_str(record.get("created_at"))
            if time_str:
                parts.append(f"[{time_str}]")
        if mode in ("sender", "time_sender"):
            sender = record.get("sender_nickname") or record.get("user_id") or "?"
            parts.append(f"{sender}:")

        if parts:
            return f"{' '.join(parts)} {content}"
        return content

    def _apply_solo_prefix(self, record: dict, content: str, tag: str) -> str:
        """给单边 assistant（proactive/orphan）加前缀：可选时间戳 + [tag] 标识。

        与 user 前缀风格一致但用方括号 tag 而非 sender（bot 自身昵称冗余）。
        """
        mode = self.ct_prefix_enhance
        parts: list[str] = []
        if mode in ("time", "time_sender"):
            time_str = self._extract_time_str(record.get("created_at"))
            if time_str:
                parts.append(f"[{time_str}]")
        parts.append(f"[{tag}]")
        prefix = " ".join(parts)
        return f"{prefix} {content}" if content else prefix

    @staticmethod
    def _extract_time_str(created_at) -> str:
        """从 created_at 字符串提取 ``MM/DD HH:MM:SS``。

        支持 SQLite CURRENT_TIMESTAMP 格式 ``2026-07-08 14:30:25``、
        ISO ``2026-07-08T14:30:25``。其他格式回退到原值字符串。
        跨天对话（cross_session 拉跨天数据）时日期才有意义；同一天内秒位冗余但保留便于精确定位。
        """
        if not created_at:
            return ""
        s = str(created_at)
        if len(s) >= 19 and s[10] in (" ", "T"):
            # s[5:19] = "MM-DD HH:MM:SS"（空格）或 "MM-DDTHH:MM:SS"（ISO）
            return s[5:19].replace("-", "/").replace("T", " ")
        return s

    @staticmethod
    def _is_pure_media(r: dict) -> bool:
        """判定是否纯媒体：content_kind 非空且全部属于媒体 kind。

        图文混合（如 ['text','image']）保留文本部分，不丢。
        """
        kind = r.get("content_kind") or []
        if not kind:
            return False
        return all(k in _MEDIA_KINDS for k in kind)

    @staticmethod
    def _merge_with_solo(contexts: list[dict]) -> list[dict]:
        """合并连续同 role + 同 solo 标记的消息。

        - 连续 user 合并（每条独立前缀已应用，合并后空行分隔）
        - 连续非 solo assistant 合并（如多轮 llm_success 紧邻）
        - 连续 solo assistant 合并（如多条 proactive 推送）
        - solo 与非 solo assistant 不合并（语义不同，分开保留可读性）
        """
        merged: list[dict] = []
        for c in contexts:
            last = merged[-1] if merged else None
            if (last and last["role"] == c["role"]
                    and last.get("_solo") == c.get("_solo")):
                last["content"] += "\n\n" + c["content"]
            else:
                merged.append(dict(c))
        return merged

    @staticmethod
    def _is_group_umo(umo: str) -> bool:
        if not umo:
            return False
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False
        return parts[1] == "GroupMessage"

    async def _safe_reset_history(self, umo: str, cid: str):
        try:
            await self.context.conversation_manager.update_conversation(
                umo, conversation_id=cid, history=[]
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 清理 native history 失败: {e}")

    # ── 捕获 BOT 回复 + 检测 reset/new ──────────────────

    @filter.on_decorating_result(priority=10)
    async def capture_bot(self, event: AstrMessageEvent):
        """BOT 回复捕获：插入 assistant 记录，按 extras 判定 llm_status 与 pair_id。"""
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return

        # /reset / /new 检测
        if event.get_extra("_clean_group_context_session"):
            await self._on_reset_or_new(event, umo)
            return

        result = event.get_result()
        if not result or not result.chain:
            return

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

        # 判定 assistant.llm_status + pair_id
        if not capture_attempted:
            # 没经过 capture_user → 主动消息（含 cron）
            asst_status = _LLM_PROACTIVE
            pair_id: Optional[str] = None
            logger.info(f"{self._log_prefix(event)} assistant 标 proactive（主动消息）")
        elif not captured:
            # 经过 capture_user 但落库失败 → 漏存
            asst_status = _LLM_ORPHAN
            pair_id = None
            logger.warning(
                f"{self._log_prefix(event)} assistant 标 orphan（user 漏存：DB 写入失败）"
            )
        elif llm_triggered and result.is_llm_result():
            asst_status = _LLM_SUCCESS
            pair_id = user_msg_id if not no_mid else None
            # user 从 llm_pending 升级到 llm_success（无 mid 时跳过：无法定位）
            if not no_mid and user_msg_id:
                self._spawn_tasks(self._safe_update_llm_status(
                    umo, cid, user_msg_id, _LLM_SUCCESS,
                ))
        else:
            # 走 capture_user 但没走 LLM（命令回复、set_result 等）
            asst_status = _LLM_DEFAULT
            pair_id = user_msg_id if not no_mid else None
            if no_mid:
                self._log(f"{self._log_prefix(event)} assistant 平台无 mid，pair_id 留空")

        content = self._truncate(bot_text) if bot_text else self._content_placeholder(asst_kind)
        # persona_id：优先从 extras（capture_user 已缓存）；兜底重查
        persona_id = event.get_extra("chat_memory_persona_id")
        if persona_id is None:
            persona_id = await self._get_effective_persona(umo, event, cid)
        audit = self._collect_audit_fields(event)
        self._spawn_tasks(self._safe_insert(
            umo, cid, user_id, "assistant", content,
            message_id=None, pair_id=pair_id,
            llm_status=asst_status, content_kind=asst_kind,
            persona_id=persona_id or None,
            created_at=self._now(),
            **audit,
        ))
        self._log(
            f"{self._log_prefix(event)} bot[{asst_status or 'default'}] -> "
            f"{user_id}@{cid[:8]}: {content[:60]}..."
        )

    # ── reset / new 处理 ─────────────────────────────

    async def _on_reset_or_new(self, event: AstrMessageEvent, umo: str):
        """区分 /reset 和 /new，分别处理。

        /reset: CID 不变，清空历史 → 清除该 CID 下所有存档记录。
        /new:   产生新 CID → 旧 CID 记录保留。

        ⚠️ 已知脆弱点：AstrBot 仅提供 ``_clean_group_context_session`` extra 标识
        "属于 reset/new 类"，不区分具体哪个。这里靠 result 文本含 "reset" 区分——
        若 bot 回复内容里碰巧含 "reset" 字样（如"已重置"翻译为英文回复），会误判为 /reset
        并清库。AstrBot 当前无官方区分 API，这是无奈之举。
        """
        cid = await self._get_curr_cid(umo)
        if not cid:
            return

        result = event.get_result()
        result_text = ""
        if result and result.chain:
            result_text = "".join(
                comp.text for comp in result.chain if isinstance(comp, Plain)
            )

        if "reset" in result_text.lower():
            # 删除前 SELECT count + warning（审计痕迹，CR1 P1-2）：误判时日志可追溯
            count = await self.db.count_by_conversation(umo, cid)
            if count > 0:
                logger.warning(
                    f"{self._log_prefix(event)} /reset 即将清除 CID={cid[:8]} 下 {count} 条存档（不可逆，result_text 命中 'reset'）"
                )
                deleted = await self.db.delete_by_conversation(umo, cid)
                logger.info(f"{self._log_prefix(event)} /reset 完成：实际清除 {deleted} 条")
            else:
                self._log(f"{self._log_prefix(event)} /reset（CID={cid[:8]}），无存档记录可清除")
        else:
            self._log(f"{self._log_prefix(event)} /new（CID={cid[:8]}），新对话开始")

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
    ) -> list[dict]:
        """查询会话历史。``user_id`` 为空时返回该会话所有用户的混合记录（群聊场景）。

        ``llm_status`` 支持 str 或 list[str]：按 LLM 状态过滤（list 用 IN）。
        ``content_kind`` 支持 str 或 list[str]：返回 content_kind JSON 数组中**任一包含**这些值的记录。
        ``role_filter`` 给定时仅返回 role 匹配的记录（``'user'`` / ``'assistant'``）。
        ``persona_id`` 给定时按 persona 严格过滤（None / 空串跳过）。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤时间窗口（含端点，tz-aware 自动转 UTC）。
        """
        return await self.db.query_latest(
            umo, conversation_id, user_id, limit, llm_status, content_kind, role_filter,
            persona_id=persona_id, since=since, until=until,
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
    ) -> list[list[dict]]:
        """按轮次返回 user-assistant 配对。每轮 ``[user_dict, assistant_dict]`` 两条。

        ``llm_status`` / ``content_kind`` 仅过滤 user 侧（assistant 仍按配对字段返回）。
        ``persona_id`` 给定时按 persona 过滤（user + assistant 都加，保证配对同 persona）。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤（user + assistant 都加，保证配对
        在时间窗口内；EXISTS 子查不限时间，保持"有配对"语义）。
        """
        return await self.db.query_rounds(
            umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
            persona_id=persona_id, since=since, until=until,
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
        created_at: Optional[datetime] = None,
    ) -> bool:
        try:
            await self.db.insert(
                umo, cid, user_id, role, content, message_id, pair_id,
                llm_status=llm_status, content_kind=content_kind,
                platform_id=platform_id, platform_name=platform_name,
                message_type=message_type, session_id=session_id,
                self_id=self_id, group_id=group_id, sender_nickname=sender_nickname,
                raw_timestamp=raw_timestamp,
                at_id=at_id, reply_id=reply_id, forward_id=forward_id,
                persona_id=persona_id, created_at=created_at,
            )
            return True
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 写入失败: {e}")
            return False

    async def _safe_update_llm_status(
        self, umo: str, cid: str, message_id: str, new_status: str,
    ) -> int:
        try:
            return await self.db.update_llm_status(umo, cid, message_id, new_status)
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 更新 llm_status 失败: {e}")
            return 0

    # ── 生命周期终止 ─────────────────────────────────

    async def terminate(self):
        """AstrBot 卸载/重载时调用：取消未完成任务 + 释放 DB 连接池。

        热重载场景下若不显式 dispose，aiosqlite 连接与 SQLAlchemy 引擎会泄漏，
        多次重载后可能耗尽文件描述符。
        """
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

        # 2. fire-and-forget task：先给 flush 窗口（保护在写的 assistant 记录），超时才 cancel
        pending = [t for t in self._pending_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_TERMINATE_FLUSH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self._pending_tasks.clear()

        # 3. 释放 DB 连接池
        try:
            await self.db.engine.dispose()
        except Exception as e:
            logger.warning(f"{self._log_prefix()} engine.dispose 异常: {e}")

        logger.info(f"{self._log_prefix()} 已终止")
