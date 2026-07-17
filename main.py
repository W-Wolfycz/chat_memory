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

assistant 配对：新记录优先用内部 ``turn_id``；旧记录用 ``pair_id`` = 对应 user 的
``message_id`` 回退。平台无 mid 时也可通过 ``turn_id`` 配对。
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.star import Star, Context, StarTools
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain

from .storage import DBManager
from .message_classifier import (
    classify_assistant_chain as _classify_assistant_chain_impl,
    classify_content as _classify_content_impl,
    content_placeholder as _content_placeholder_impl,
    extract_text as _extract_text_impl,
)
from .context_builder import (
    FULL_GROUP_CONTEXT_INSTRUCTION,
    TakeoverContextBuilder,
    extract_time_str as _extract_time_str_impl,
    is_pure_media as _is_pure_media_impl,
    strip_reasoning_prefix as _strip_reasoning_prefix_impl,
)
from .models import (
    LLM_DEFAULT as _LLM_DEFAULT,
    LLM_ORPHAN as _LLM_ORPHAN,
    LLM_PENDING as _LLM_PENDING,
    LLM_PROACTIVE as _LLM_PROACTIVE,
    LLM_SUCCESS as _LLM_SUCCESS,
    MEDIA_KINDS as _MEDIA_KINDS,
    SEND_ATTEMPTED as _SEND_ATTEMPTED,
    SEND_PREPARED as _SEND_PREPARED,
)

class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.max_len = config.get("max_content_length", 0)
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)

        log_conf = config.get("log_config", {})
        self.log_with_bot_id = log_conf.get("log_with_bot_id", False)
        self.debug_to_info = log_conf.get("debug_to_info", False)

        ct_conf = config.get("context_takeover", {}) or {}
        self.ct_enable = bool(ct_conf.get("enable", False))
        self.ct_cross_session = bool(ct_conf.get("cross_session", False))
        self.ct_full_group = bool(ct_conf.get("full_group", False))
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
        # Kind 白名单：选中=需要；默认 ["text"]；空集合 = 不过滤（全部进入）
        self.ct_include_kinds: set[str] = set(ct_conf.get("include_content_kinds", ["text"]) or [])
        # ALL 模式：content_kind 必须 ⊆ 白名单（且非空）；False = ANY（任一交集即进）
        self.ct_include_all_match = bool(ct_conf.get("include_all_match", False))
        # persona 过滤：开启后查询严格按当前 persona_id 过滤；persona_id 为空时跳过（兜底）
        # 与 cross_session=T 协同可获完整 persona 隔离体验（切 persona + /new + 切回仍可见旧数据）
        self.ct_filter_by_persona = bool(ct_conf.get("filter_by_persona", False))

        # 读取 AstrBot 全局时区配置（IANA 名称如 "Asia/Shanghai"），传给 DBManager
        # 做查询输出转换：存储统一 UTC naive，返回时转此 tz naive
        try:
            tz_name = context.get_config().get("timezone", "Asia/Shanghai")
            self._tz = ZoneInfo(tz_name)
        except Exception:
            self._tz = ZoneInfo("Asia/Shanghai")

        data_dir = StarTools.get_data_dir("chat_memory")
        self.db = DBManager(data_dir, tz=self._tz)

        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_started = False

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

    async def initialize(self) -> None:
        """在插件加载阶段完成数据库迁移并启动后台服务。

        初始化失败必须继续向外抛出，让 AstrBot 将插件标记为加载失败；否则插件可能
        表面可用、实际从第一条消息开始持续漏存。失败前释放已创建的数据库连接。
        """
        try:
            await self.db.init_db()
            await self._ensure_cleanup_started()
        except BaseException:
            try:
                await self.db.engine.dispose()
            except Exception as dispose_error:
                logger.warning(
                    f"{self._log_prefix()} 初始化失败后的 engine.dispose 异常: "
                    f"{dispose_error}"
                )
            raise

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
                cutoff = datetime.now(dt_timezone.utc).replace(tzinfo=None) - timedelta(days=self.auto_cleanup_days)
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

    @staticmethod
    def _strip_reasoning_prefix(text: str) -> str:
        return _strip_reasoning_prefix_impl(text)

    def _log(self, msg: str):
        if self.debug_to_info:
            logger.info(msg)
        else:
            logger.debug(msg)

    @staticmethod
    def _extract_text(event: AstrMessageEvent) -> str:
        return _extract_text_impl(event)

    @staticmethod
    def _classify_content(event: AstrMessageEvent) -> tuple[list[str], Optional[str], Optional[str], Optional[str]]:
        return _classify_content_impl(event)

    @staticmethod
    def _classify_assistant_chain(chain) -> tuple[list[str], str]:
        return _classify_assistant_chain_impl(chain)

    @staticmethod
    def _content_placeholder(kind: list[str]) -> str:
        return _content_placeholder_impl(kind)

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
        # 内部 turn_id 不依赖平台 message_id；无 mid 平台也能建立 user/assistant 配对。
        turn_id = event.get_extra("chat_memory_turn_id") or uuid.uuid4().hex
        event.set_extra("chat_memory_turn_id", turn_id)

        audit = self._collect_audit_fields(event)
        ok = await self._safe_insert(
            umo, cid, user_id, "user", self._truncate(content),
            message_id=msg_id or None, pair_id=None,
            llm_status=_LLM_DEFAULT, content_kind=kind,
            at_id=at_id, reply_id=reply_id, forward_id=forward_id,
            persona_id=persona_id or None,
            turn_id=turn_id,
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
        """LLM 调用时：兜底重试 user 捕获并按 ``turn_id`` 升级为 ``llm_pending``。"""
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

        turn_id = event.get_extra("chat_memory_turn_id")
        if not turn_id:
            logger.warning(f"{self._log_prefix(event)} user 已捕获但缺少 turn_id，跳过 llm_status 更新")
            return
        await self._safe_update_llm_status_by_turn(umo, cid, turn_id, _LLM_PENDING)
        self._log(f"{self._log_prefix(event)} turn[{turn_id[:8]}] llm_status -> llm_pending")

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
        current_turn_id = event.get_extra("chat_memory_turn_id") or ""
        contexts = await self.build_takeover_contexts(
            umo=umo,
            user_id=user_id,
            conversation_id=cid,
            persona_id=persona_id,
            exclude_turn_id=current_turn_id,
        )
        if not contexts:
            await self._handle_empty_takeover(event, req, umo, cid, "CM 无数据")
            return

        if self.ct_full_group and self._is_group_umo(umo):
            self._append_full_group_instruction(req)
        req.contexts = contexts

        if self.ct_clear_native_history:
            await self._safe_reset_history(umo, cid)

        self._log(
            f"{self._log_prefix(event)} 接管 contexts={len(contexts)} "
            f"(cross_session={self.ct_cross_session}, full_group={self.ct_full_group}, "
            f"cid={cid[:8]})"
        )

    async def _handle_empty_takeover(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        umo: str,
        cid: str,
        reason: str,
    ) -> None:
        """处理 takeover 空结果。

        严格模式显式清空 ``req.contexts``，保证 CM 仍是唯一上下文源；兼容模式保留
        AstrBot 已装载的 native contexts。两种模式都遵循 ``clear_native_history`` 配置。
        """
        if self.ct_fallback_to_native_on_empty:
            self._log(f"{self._log_prefix(event)} 接管回退 native：{reason}")
            return

        req.contexts = []
        if self.ct_clear_native_history:
            await self._safe_reset_history(umo, cid)
        self._log(f"{self._log_prefix(event)} 严格接管 contexts=0：{reason}")

    async def _takeover_query(
        self,
        umo: str,
        cid: str,
        user_id: str,
        persona_id: str = "",
        exclude_turn_id: str = "",
        force_current_session: bool = False,
    ) -> list[dict]:
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
        effective_cross_session = self.ct_cross_session and not force_current_session
        target_cid: Optional[str] = None if effective_cross_session else cid
        cross_umo = effective_cross_session

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
                    exclude_turn_id=exclude_turn_id or None,
                )

            # 防御性全局排序（混合模式下 messages_raw 已排序，但保持一致）
            if records:
                records.sort(key=lambda r: r.get("created_at") or "")
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 接管查询失败: {e}")
            return []

        return records

    def _takeover_normalize(
        self,
        records: list[dict],
        umo: str,
        max_records: Optional[int] = None,
        max_chars: int = 0,
        current_user_id: str = "",
        full_group: bool = False,
    ) -> list[dict]:
        builder = TakeoverContextBuilder(
            media_kinds=_MEDIA_KINDS,
            current_user_id=current_user_id,
            full_group=full_group,
            proactive_status=_LLM_PROACTIVE,
            orphan_status=_LLM_ORPHAN,
        )
        return builder.normalize(
            records,
            max_records=max_records,
            max_chars=max_chars,
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
    def _extract_time_str(created_at) -> str:
        return _extract_time_str_impl(created_at)

    @staticmethod
    def _is_pure_media(r: dict) -> bool:
        return _is_pure_media_impl(r, _MEDIA_KINDS)

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
        """BOT 回复捕获：插入 prepared assistant，按 extras 判定状态与 turn 配对。"""
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
        else:
            # 走 capture_user 但没走 LLM（命令回复、set_result 等）
            asst_status = _LLM_DEFAULT
            pair_id = user_msg_id if not no_mid else None
            if no_mid:
                self._log(f"{self._log_prefix(event)} assistant 平台无 mid，使用 turn_id 配对")

        content = self._truncate(bot_text) if bot_text else self._content_placeholder(asst_kind)
        # persona_id：优先从 extras（capture_user 已缓存）；兜底重查
        persona_id = event.get_extra("chat_memory_persona_id")
        if persona_id is None:
            persona_id = await self._get_effective_persona(umo, event, cid)
        audit = self._collect_audit_fields(event)
        event.set_extra("chat_memory_assistant_turn_id", turn_id)
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
                f"{self._log_prefix(event)} assistant prepared 写入失败，turn={turn_id[:8]}"
            )
        self._log(
            f"{self._log_prefix(event)} bot[{asst_status or 'default'}] -> "
            f"{user_id}@{cid[:8]}: {content[:60]}..."
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
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``（与 takeover 对齐）。
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
        ``persona_id``：None 不过滤；非空按值过滤；空串严格过滤 ``IS NULL OR ''``。user + assistant 都加。
        ``since`` / ``until`` 给定时按 ``created_at`` 过滤（user + assistant 都加，保证配对
        在时间窗口内；EXISTS 子查不限时间，保持"有配对"语义）。
        """
        return await self.db.query_rounds(
            umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
            persona_id=persona_id, since=since, until=until,
        )

    async def build_takeover_contexts(
        self,
        umo: str,
        user_id: str,
        conversation_id: Optional[str] = None,
        persona_id: str = "",
        exclude_turn_id: str = "",
    ) -> Optional[list[dict]]:
        """只读构建与当前 context takeover 完全一致的 LLM ``contexts``。

        返回值语义：

        - ``None``：``context_takeover.enable=false``；
        - ``[]``：接管已启用，但会话、用户范围或规整后的记录为空；
        - ``list[dict]``：已按当前接管配置完成查询、前缀增强与边界裁剪。

        本方法不清理 AstrBot native history、不修改请求对象，也不写数据库。调用方未提供
        ``conversation_id`` 时会读取 ``umo`` 的当前 conversation。空 ``user_id`` 仅在
        ``full_group`` 已启用且 ``umo`` 确为群聊时允许；此时即使配置了
        ``cross_session`` 也强制降级为当前 UMO + CID 的整群范围，避免空用户条件扩大到
        整个平台。
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

        records = await self._takeover_query(
            umo,
            cid,
            user_id,
            persona_id,
            exclude_turn_id=exclude_turn_id,
            force_current_session=not bool(user_id),
        )
        if not records:
            return []

        mixed_mode = set(self.ct_llm_status_filter) != {_LLM_SUCCESS}
        return self._takeover_normalize(
            records,
            umo,
            max_records=self.ct_limit_rounds if mixed_mode else None,
            max_chars=self.ct_max_context_chars,
            current_user_id=user_id,
            full_group=effective_full_group,
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
        update_user_llm_status: Optional[str] = None,
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
                persona_id=persona_id,
                turn_id=turn_id, send_status=send_status,
                update_user_llm_status=update_user_llm_status,
            )
            return True
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 写入失败: {e}")
            return False

    async def _safe_update_llm_status_by_turn(
        self, umo: str, cid: str, turn_id: str, new_status: str,
    ) -> int:
        try:
            return await self.db.update_llm_status_by_turn(umo, cid, turn_id, new_status)
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 按 turn_id 更新 llm_status 失败: {e}")
            return 0

    async def _safe_update_send_status(
        self, umo: str, cid: str, turn_id: str, new_status: str,
    ) -> int:
        try:
            return await self.db.update_send_status(umo, cid, turn_id, new_status)
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 更新 send_status 失败: {e}")
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

        # 2. 释放 DB 连接池（关键写入均直接 await，不再维护后台写入任务）
        try:
            await self.db.engine.dispose()
        except Exception as e:
            logger.warning(f"{self._log_prefix()} engine.dispose 异常: {e}")

        logger.info(f"{self._log_prefix()} 已终止")
