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
import inspect
import json
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
from astrbot.core.agent.message import TextPart

from .storage import DBManager
from .media_archive import MediaArchiver
from .message_classifier import (
    classify_assistant_chain as _classify_assistant_chain_impl,
    classify_content as _classify_content_impl,
    content_placeholder as _content_placeholder_impl,
    build_relation_seed as _build_relation_seed_impl,
    build_relation_seed_full as _build_relation_seed_full_impl,
    extract_text as _extract_text_impl,
    system_event_summary as _system_event_summary_fn,
)
from .context_builder import (
    CM_GENERAL_RULES,
    CROSS_SESSION_CONTEXT_INSTRUCTION,
    CROSS_SESSION_SOURCE_MARKER_PREFIX,
    FULL_GROUP_CONTEXT_INSTRUCTION,
    TakeoverContextBuilder,
    extract_time_str as _extract_time_str_impl,
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
    build_current_turn_xml,
    dump_relation_data,
    truncate_content_template,
)


_TAKEOVER_APPLIED_EXTRA = "chat_memory_takeover_applied"
_CURRENT_FOCUS_INJECTED_EXTRA = "chat_memory_current_focus_injected"

class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.max_len = config.get("max_content_length", 0)
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)
        # 日志前缀附加机器人 ID（顶层配置）：多 Bot 实例环境便于定位。
        # 日志等级不再由插件配置：AstrBot WebUI 可运行期修改插件日志等级。
        # 1.2.2 前该开关位于 log_config 组内：读时继承旧值，initialize 阶段
        # 执行一次性迁移写回（旧组并入顶层后删除），此后只有顶层键生效。
        self._config = config
        self.log_with_bot_id = self._resolve_log_with_bot_id(config)

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
        self.db = DBManager(data_dir, tz=self._tz)

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
            # 一次性配置迁移（1.2.2）：旧 log_config 组并入顶层并写回删除。
            # 迁移本身失败不阻断加载——读时继承已保证行为正确，仅下次启动重试。
            await self._migrate_log_config()
            await self.db.init_db()
            await self._ensure_cleanup_started()
            # 媒体归档：worker 与周期清理独立于记录自动清理（auto_cleanup_days 关闭也运行）
            self.media_archiver.start()
            if self.media_archiver.enabled:
                self._media_cleanup_task = asyncio.create_task(
                    self._media_cleanup_loop()
                )
        except BaseException:
            try:
                await self.db.engine.dispose()
            except Exception as dispose_error:
                logger.warning(
                    f"{self._log_prefix()} 初始化失败后的 engine.dispose 异常: "
                    f"{dispose_error}"
                )
            raise

    async def _migrate_log_config(self) -> None:
        """把旧 ``log_config`` 组迁移到顶层 ``log_with_bot_id`` 并写回删除旧组。

        规则：顶层键已存在时以顶层为准，旧组直接删除；顶层键不存在时先继承旧值。
        写回成功后配置文件中不再有旧组，后续 WebUI 修改只作用于顶层键。
        """
        config = self._config
        if not isinstance(config, dict) or "log_config" not in config:
            return
        legacy = config.get("log_config") or {}
        if config.get("log_with_bot_id") is None and "log_with_bot_id" in legacy:
            config["log_with_bot_id"] = bool(legacy["log_with_bot_id"])
            self.log_with_bot_id = bool(legacy["log_with_bot_id"])
        del config["log_config"]
        save = getattr(config, "save_config_async", None) or getattr(
            config, "save_config", None
        )
        if not callable(save):
            logger.warning(
                f"{self._log_prefix()} 配置迁移已在内存完成，但 config 对象不支持写回"
            )
            return
        try:
            result = save()
            if inspect.isawaitable(result):
                await result
            logger.info(
                f"{self._log_prefix()} 已迁移日志配置：log_config 组并入顶层并移除旧组"
            )
        except Exception as exc:
            logger.warning(f"{self._log_prefix()} 配置迁移写回失败: {exc}")

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
                    deleted, media_rows = await self.db.delete_old(cutoff)
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
        """解析日志实例前缀开关（含旧版 log_config 组的一次性迁移）。

        顶层 ``log_with_bot_id`` 存在时（WebUI 已保存过）以它为准；键不存在时
        继承旧 ``log_config.log_with_bot_id`` 的值，避免升级后功能静默关闭。
        """
        top_level = config.get("log_with_bot_id")
        if top_level is not None:
            return bool(top_level)
        legacy = (config.get("log_config") or {}).get("log_with_bot_id")
        return bool(legacy)

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
    def _classify_content(event: AstrMessageEvent) -> tuple[list[str], Optional[str], Optional[str], Optional[str]]:
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
                    f"kind={kind} id={media_id[:8]}: {exc}"
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
    def _system_event_summary_impl(event: AstrMessageEvent) -> str:
        return _system_event_summary_fn(event)

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
            logger.debug(f"{self._log_prefix(event)} 跳过 user 捕获：umo 或 user_id 为空")
            return False

        try:
            if user_id == event.get_self_id():
                logger.debug(f"{self._log_prefix(event)} 跳过 user 捕获：BOT 自身消息")
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
        user_text = user_template or self._extract_text(event)

        # content 决定：有文本用文本；否则用占位；empty（[] + 无引用字段）用空串。
        # system_event 优先使用 notice/request 事件的可读摘要（如 [撤回消息]），
        # kind 保持 system_event，不写成 text 类型。
        if user_text:
            content = user_text
        elif kind == [_K_SYSTEM]:
            content = self._system_event_summary_impl(event) or self._content_placeholder(kind)
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

        audit = self._collect_audit_fields(event)
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
        if at_id: ref_repr.append(f"at={at_id[:8]}")
        if reply_id: ref_repr.append(f"reply={reply_id[:8]}")
        if forward_id: ref_repr.append(f"fwd={forward_id[:8]}")
        ref_str = f"[{','.join(ref_repr)}]" if ref_repr else ""
        logger.debug(
            f"{self._log_prefix(event)} user[{msg_id[:8] or '-'}][{kind_repr}]{ref_str} -> "
            f"{user_id}@{cid[:8]}: {content[:60]}"
        )

        event.set_extra("chat_memory_captured", True)
        event.set_extra("chat_memory_cid", cid)
        event.set_extra("chat_memory_user_msg_id", msg_id)
        event.set_extra("chat_memory_llm_triggered", False)
        event.set_extra("chat_memory_no_mid", no_mid)
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
        logger.debug(f"{self._log_prefix(event)} turn[{turn_id[:8]}] llm_status -> llm_pending")

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

        tool_call_id 为 CM 自造（``cm_tool_<turn>_<n>``），只要求同一轮内
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
            call_ids = [f"cm_tool_{turn_id[:8]}_{i}" for i in range(len(recs))]
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
            await self.db.insert_tool_record(
                umo, cid, turn_id, call_index, tool_name, tool_args, tool_result
            )
            return True
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 工具调用写入失败: {e}")
            return False

    async def _query_tool_contexts(self, umo: str, cid: str) -> list[dict]:
        """按当前接管配置回放 CM 库中最近 N 轮工具调用为 OpenAI 格式 contexts。

        ``keep_tool_turns=0`` 时直接关闭回放（用户可显式填 0）。
        """
        if self.ct_keep_tool_turns <= 0:
            return []
        try:
            records = await self.db.query_tool_records(
                umo, cid, self.ct_keep_tool_turns
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 工具上下文查询失败: {e}")
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
        call_index = int(event.get_extra("chat_memory_tool_seq") or 0) + 1
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
                f"{cid[:8]}: {result_text[:60]}"
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
            f"cid={cid[:8]})"
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
                f"{self._log_prefix(event)} 当前轮焦点注入失败: {type(exc).__name__}: {exc}"
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
            logger.debug(f"{self._log_prefix(event)} 接管回退 native：{reason}")
            return

        req.contexts = []
        # 即使历史为空也注入通用规则，避免模型收到未解释的 cm_ 标签。
        self._append_general_rules(req)
        event.set_extra(_TAKEOVER_APPLIED_EXTRA, True)
        if self.ct_clear_native_history:
            await self._safe_reset_history(umo, cid)
        logger.debug(f"{self._log_prefix(event)} 严格接管 contexts=0：{reason}")

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
                # 配对模式：按 user 时间排序整轮，并保持 user/assistant 相邻。
                # query_rounds_raw 已按 created_at ASC, id ASC 返回，无需再次排序。
                rounds = await self.db.query_rounds_raw(
                    umo, target_cid, user_id, limit, include_kinds, all_match,
                    cross_umo=cross_umo, full_group=effective_full_group,
                    persona_id=persona_id, filter_by_persona=filter_by_persona,
                )
                records: list[dict] = [msg for rnd in rounds for msg in rnd]
            else:
                # 混合模式：按消息数；overfetch 2x 给规整留余地（防先 LIMIT 后过滤导致空上下文）
                # 规整阶段会丢头部 assistant / 尾部 solo；overfetch 后再在 normalize 截到目标条数
                # query_messages_raw 已按全局时间线升序返回，无需再次排序。
                records = await self.db.query_messages_raw(
                    umo, target_cid, user_id, limit * 2, status_set, include_kinds, all_match,
                    cross_umo=cross_umo, full_group=effective_full_group,
                    persona_id=persona_id, filter_by_persona=filter_by_persona,
                    exclude_turn_id=exclude_turn_id or None,
                )
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
    def _extract_time_str(created_at) -> str:
        return _extract_time_str_impl(created_at)

    @staticmethod
    def _is_group_umo(umo: str) -> bool:
        if not umo:
            return False
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False
        return parts[1] == "GroupMessage"

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
            logger.warning(f"{self._log_prefix()} 清理 native history 失败: {e}")

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
            logger.warning(
                f"{self._log_prefix(event)} 核心会话清理标志存在，但事件文本无法区分 reset/new"
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
        logger.debug(
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

    @staticmethod
    def _get_context_control_command(event: AstrMessageEvent) -> str:
        """在核心可信清理标志成立后，从事件文本区分 ``reset`` / ``new``。"""
        try:
            message = event.get_message_str()
        except Exception:
            message = getattr(event, "message_str", "")
        message = str(message or "").lower()
        if "reset" in message:
            return "reset"
        if "new" in message:
            return "new"
        return ""

    async def _on_reset_or_new(
        self,
        event: AstrMessageEvent,
        umo: str,
        command: str,
    ):
        """按已经严格识别的 ``reset`` / ``new`` 分别处理。

        /reset: CID 不变，清空历史 → 清除该 CID 下所有存档记录。
        /new:   产生新 CID → 旧 CID 记录保留。
        """
        cid = await self._get_curr_cid(umo)
        if not cid:
            return

        if command == "reset":
            # 直接删除并记录实际数量：一次 DELETE 完成，不再先 COUNT 预查。
            deleted, media_rows = await self.db.delete_by_conversation(umo, cid)
            await self.media_archiver.delete_files(media_rows)
            if deleted > 0:
                logger.info(
                    f"{self._log_prefix(event)} /reset 完成：清除 CID={cid[:8]} "
                    f"下 {deleted} 条存档（不可逆）"
                )
            else:
                logger.debug(f"{self._log_prefix(event)} /reset（CID={cid[:8]}），无存档记录可清除")
        else:
            logger.debug(f"{self._log_prefix(event)} /new（CID={cid[:8]}），新对话开始")

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
        return await self.db.query_latest(
            umo, conversation_id, user_id, limit, llm_status, content_kind, role_filter,
            persona_id=persona_id, since=since, until=until,
            from_oldest=from_oldest,
            after_id=after_id,
            content_kind_all_match=content_kind_all_match,
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
        return await self.db.query_rounds(
            umo, conversation_id, user_id, limit_rounds, llm_status, content_kind,
            persona_id=persona_id, since=since, until=until,
            from_oldest=from_oldest,
            after_id=after_id,
            content_kind_all_match=content_kind_all_match,
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
        - ``list[dict]``：已按当前接管配置完成查询、前缀增强与边界裁剪。开启
          ``cross_session`` 且结果含其他来源时，其他来源的 user 会带请求内匿名的
          ``<cm_source n="N"/>`` XML 元数据；paired assistant 继承前一条 user 的来源，
          当前会话不加来源元数据。

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
            try:
                target_map.update(
                    await self.db.query_turn_targets(source_umo, target_ids)
                )
            except Exception as exc:
                logger.warning(
                    f"{self._log_prefix()} Reply 目标批量查询失败："
                    f"{type(exc).__name__}"
                )

        mixed_mode = set(self.ct_llm_status_filter) != {_LLM_SUCCESS}
        result = self._takeover_normalize(
            records,
            umo,
            max_records=self.ct_limit_rounds if mixed_mode else None,
            max_chars=self.ct_max_context_chars,
            current_user_id=user_id,
            full_group=effective_full_group,
            cross_session=self.ct_cross_session and bool(user_id),
            paired_rounds=not mixed_mode,
            target_map=target_map,
        )
        # 工具调用上下文：从 CM 库回放最近 N 轮（assistant tool_calls + role=tool），
        # 按 turn_id 插入对应轮次内部（该轮 user 之后、最终回复之前），与 AstrBot
        # 原生历史顺序一致；历史中无该轮时回退追加尾部。工具段属于当前 umo + cid，
        # 不参与 cross_session / full_group 扩大范围，也不进入 user/assistant 配对。
        tool_contexts = await self._query_tool_contexts(umo, cid)
        if tool_contexts:
            result = self._insert_tool_contexts(result, tool_contexts)
        else:
            for context in result:
                context.pop("_turn_id", None)
        return result

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
                relation_data=relation_data,
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

        # 2. 停止媒体归档（取消后台下载 worker，清 .tmp）与媒体清理 task
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

        # 3. 释放 DB 连接池（关键写入均直接 await，不再维护后台写入任务）
        try:
            await self.db.engine.dispose()
        except Exception as e:
            logger.warning(f"{self._log_prefix()} engine.dispose 异常: {e}")

        logger.info(f"{self._log_prefix()} 已终止")
