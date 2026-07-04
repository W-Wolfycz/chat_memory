"""ChatMemory — 独立对话记录存档，以 UMO + conversation_id + 用户ID 为维度存储对话文本。

存储设计：每条记录带 ``message_id``（平台消息 id），assistant 记录额外带 ``pair_id``
（= 对应 user 的 message_id），``tag`` 字段标识消息的成因/形态：

- ``non_llm``       非 LLM 路径（命令、插件直发、未回复等）
- ``llm_pending``   LLM 触发但 assistant 未成功（孤儿，仅 user 侧出现）
- ``llm_success``   LLM 路径且 assistant 成功回复（user 与 assistant 双侧同步）
- ``media_only``    纯媒体消息（终态，走 LLM 后保持，不参与 llm_pending/llm_success 流转）
- ``no_message_id`` 平台未给 message_id（user 与 assistant 都标此值，无法 pair_id 配对）
- ``proactive``     主动消息（无前置 user 事件）
- ``orphan``        user 漏存（DB 写入失败）但 assistant 来了
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain

from .storage import DBManager

_TAG_NON_LLM = "non_llm"
_TAG_LLM_PENDING = "llm_pending"
_TAG_LLM_SUCCESS = "llm_success"
_TAG_MEDIA_ONLY = "media_only"
_TAG_NO_MID = "no_message_id"
_TAG_PROACTIVE = "proactive"
_TAG_ORPHAN = "orphan"


class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.max_len = config.get("max_content_length", 500)
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)

        log_conf = config.get("log_config", {})
        self.log_with_bot_id = log_conf.get("log_with_bot_id", False)
        self.debug_to_info = log_conf.get("debug_to_info", False)

        data_dir = Path(context.get_config().get("plugin.data_dir", "./data")) / "plugin_data" / "chat_memory"
        self.db = DBManager(data_dir)

        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_started = False

        cleanup_desc = (
            f"自动清理 {self.auto_cleanup_days} 天前的记录"
            if self.auto_cleanup_days > 0
            else "自动清理关闭"
        )
        logger.info(f"[ChatMemory] 对话记录存档已启用（{cleanup_desc}）")

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

    def _log(self, msg: str):
        if self.debug_to_info:
            logger.info(msg)
        else:
            logger.debug(msg)

    @staticmethod
    def _extract_text(event: AstrMessageEvent) -> str:
        chain = getattr(event, "message_chain", None)
        if chain:
            parts = [comp.text for comp in chain if isinstance(comp, Plain)]
            text = "".join(parts).strip()
            if text:
                return text
        return getattr(event, "message_str", "") or ""

    @staticmethod
    def _is_media_only(event: AstrMessageEvent) -> bool:
        """检测纯媒体消息：有非 Plain 组件，且 Plain 文本拼起来为空。"""
        chain = getattr(event, "message_chain", None) or []
        if not chain:
            return False
        has_text = any(isinstance(c, Plain) and (c.text or "").strip() for c in chain)
        has_media = any(not isinstance(c, Plain) for c in chain)
        return has_media and not has_text

    @staticmethod
    def _media_placeholder(event: AstrMessageEvent) -> str:
        """纯媒体消息的占位 content，取第一个非 Plain 组件的类型名。"""
        chain = getattr(event, "message_chain", None) or []
        for c in chain:
            if not isinstance(c, Plain):
                return f"[{type(c).__name__}]"
        return "[media]"

    async def _get_curr_cid(self, umo: str) -> str:
        try:
            conv_mgr = self.context.conversation_manager
            return await conv_mgr.get_curr_conversation_id(umo) or ""
        except Exception:
            return ""

    @staticmethod
    def _get_message_id(event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""
        return getattr(msg_obj, "message_id", "") or ""

    # ── 用户消息捕获（核心逻辑，可被多个钩子复用）──────

    async def _capture_user_internal(self, event: AstrMessageEvent) -> bool:
        """捕获 user 消息立即落库。返回 True 表示成功（或已捕获过）。

        幂等：通过 ``chat_memory_captured`` extra 防重复。
        ``chat_memory_capture_attempted`` 在入口处先设，让 capture_bot 区分
        "主动消息（attempted 未设）" vs "漏存（attempted 设但 captured 未设）"。
        """
        # 入口先标记 attempted，capture_bot 据此区分 orphan vs proactive
        if not event.get_extra("chat_memory_capture_attempted"):
            event.set_extra("chat_memory_capture_attempted", True)

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

        cid = await self._get_curr_cid(umo)
        if not cid:
            self._log(f"{self._log_prefix(event)} 跳过 user 捕获：cid 暂未创建（首条消息可能漏存）")
            return False

        user_text = self._extract_text(event)
        is_media_only = not user_text and self._is_media_only(event)
        if not user_text and not is_media_only:
            self._log(f"{self._log_prefix(event)} 跳过 user 捕获：消息为空（无文本/无媒体）")
            return False

        msg_id = self._get_message_id(event)
        no_mid = not msg_id

        # 决定终态 tag（这些 tag 不参与后续 llm_pending/llm_success 流转）
        if no_mid:
            user_tag = _TAG_NO_MID
        elif is_media_only:
            user_tag = _TAG_MEDIA_ONLY
        else:
            user_tag = _TAG_NON_LLM

        content = user_text if user_text else self._media_placeholder(event)

        # 用 await 拿到 bool，失败时不写 captured extras，让 capture_bot 走 orphan
        ok = await self._safe_insert(
            umo, cid, user_id, "user", self._truncate(content),
            message_id=msg_id or None, pair_id=None, tag=user_tag,
        )
        if not ok:
            logger.warning(f"{self._log_prefix(event)} user 写入失败，extras 未标记，assistant 将标 orphan")
            return False

        self._log(
            f"{self._log_prefix(event)} user[{msg_id[:8] or '-'}][{user_tag}] -> "
            f"{user_id}@{cid[:8]}: {content[:60]}"
        )

        event.set_extra("chat_memory_captured", True)
        event.set_extra("chat_memory_cid", cid)
        event.set_extra("chat_memory_user_msg_id", msg_id)
        event.set_extra("chat_memory_llm_triggered", False)
        event.set_extra("chat_memory_no_mid", no_mid)
        event.set_extra("chat_memory_is_media", is_media_only)
        return True

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def capture_user(self, event: AstrMessageEvent):
        """所有进入 ProcessStage 的 user 消息立即落库（默认 tag=non_llm）。"""
        await self._ensure_cleanup_started()
        await self._capture_user_internal(event)

    # ── LLM 触发标记 + 兜底捕获 ──────────────────────

    @filter.on_llm_request()
    async def mark_llm_triggered(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 调用时：兜底重试 user 捕获 + 把 tag 从 non_llm 改成 llm_pending。

        终态 tag（media_only / no_message_id）保持不变，仅设 llm_triggered extra。
        """
        if not event.get_extra("chat_memory_captured"):
            self._log(f"{self._log_prefix(event)} LLM 触发，补捕获 user（首条消息兜底）")
            ok = await self._capture_user_internal(event)
            if not ok:
                logger.warning(f"{self._log_prefix(event)} LLM 触发但 user 捕获失败，放弃 tag 更新")
                return

        umo = getattr(event, "unified_msg_origin", "")
        cid = event.get_extra("chat_memory_cid") or await self._get_curr_cid(umo)
        if not umo or not cid:
            return

        event.set_extra("chat_memory_llm_triggered", True)

        # 终态 tag：仅 LLM 触发标记，不改 tag
        if event.get_extra("chat_memory_no_mid"):
            self._log(f"{self._log_prefix(event)} user 保持 no_message_id（平台无 mid）")
            return
        if event.get_extra("chat_memory_is_media"):
            self._log(f"{self._log_prefix(event)} user 保持 media_only（纯媒体，走 LLM）")
            return

        msg_id = event.get_extra("chat_memory_user_msg_id")
        if not msg_id:
            return

        await self._safe_update_tag(umo, cid, msg_id, _TAG_LLM_PENDING)
        self._log(f"{self._log_prefix(event)} user[{msg_id[:8]}] tag -> llm_pending")

    # ── 捕获 BOT 回复 + 检测 reset/new ──────────────────

    @filter.on_decorating_result(priority=10)
    async def capture_bot(self, event: AstrMessageEvent):
        """BOT 回复捕获：插入 assistant 记录，按 extras 判定 tag 与 pair_id。"""
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

        bot_text = "".join(comp.text for comp in result.chain if isinstance(comp, Plain)).strip()
        if not bot_text:
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

        # 判定 assistant.tag（按"成因"细分，保证语义一致）
        if not capture_attempted:
            # 没经过 capture_user → 主动消息（无前置 user 事件）
            asst_tag = _TAG_PROACTIVE
            pair_id: Optional[str] = None
            logger.info(f"{self._log_prefix(event)} assistant 标 proactive（主动消息）")
        elif not captured:
            # 经过 capture_user 但落库失败 → 漏存
            asst_tag = _TAG_ORPHAN
            pair_id = None
            logger.warning(
                f"{self._log_prefix(event)} assistant 标 orphan（user 漏存：DB 写入失败）"
            )
        elif no_mid:
            # user 在库但平台无 mid → 无法 pair_id 配对，独立 tag
            asst_tag = _TAG_NO_MID
            pair_id = None
            self._log(f"{self._log_prefix(event)} assistant 标 no_message_id（平台无 mid）")
        elif llm_triggered and result.is_llm_result():
            asst_tag = _TAG_LLM_SUCCESS
            pair_id = user_msg_id
            # 仅普通文本 user 升级 llm_success；media_only 是终态 tag，保持不变
            if not event.get_extra("chat_memory_is_media"):
                asyncio.create_task(self._safe_update_tag(
                    umo, cid, user_msg_id, _TAG_LLM_SUCCESS,
                ))
        else:
            asst_tag = _TAG_NON_LLM
            pair_id = user_msg_id

        content = self._truncate(bot_text)
        asyncio.create_task(self._safe_insert(
            umo, cid, user_id, "assistant", content,
            message_id=None, pair_id=pair_id, tag=asst_tag,
        ))
        self._log(
            f"{self._log_prefix(event)} bot[{asst_tag}] -> "
            f"{user_id}@{cid[:8]}: {content[:60]}..."
        )

    # ── reset / new 处理 ─────────────────────────────

    async def _on_reset_or_new(self, event: AstrMessageEvent, umo: str):
        """区分 /reset 和 /new，分别处理。

        /reset: CID 不变，清空历史 → 清除该 CID 下所有存档记录。
        /new:   产生新 CID → 旧 CID 记录保留。
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
            deleted = await self.db.delete_by_conversation(umo, cid)
            if deleted > 0:
                logger.info(f"{self._log_prefix(event)} /reset（CID={cid[:8]}），清除 {deleted} 条存档记录")
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
        tag_filter: Optional[Union[str, list[str]]] = None,
        role_filter: Optional[str] = None,
    ) -> list[dict]:
        """查询会话历史。``user_id`` 为空时返回该会话所有用户的混合记录（群聊场景）。

        ``tag_filter`` 支持 str 或 list[str]：仅返回 tag 匹配的记录（list 用 IN）。
        ``role_filter`` 给定时仅返回 role 匹配的记录（``'user'`` / ``'assistant'``）。

        向后兼容：返回的 dict 在原 role/content/user_id/created_at 基础上**新增**
        message_id / pair_id / tag 三字段。老调用方读旧字段不受影响。
        """
        return await self.db.query_latest(umo, conversation_id, user_id, limit, tag_filter, role_filter)

    async def query_rounds(
        self,
        umo: str,
        conversation_id: str,
        user_id: Optional[str] = None,
        limit_rounds: int = 10,
        tag_filter: Optional[Union[str, list[str]]] = None,
    ) -> list[list[dict]]:
        """按轮次返回 user-assistant 配对。每轮 ``[user_dict, assistant_dict]`` 两条。

        ``tag_filter`` 支持 str 或 list[str]：仅过滤 user 侧的 tag。
        """
        return await self.db.query_rounds(umo, conversation_id, user_id, limit_rounds, tag_filter)

    # ── 内部工具 ──────────────────────────────────────

    async def _safe_insert(
        self, umo: str, cid: str, user_id: str, role: str, content: str,
        message_id: Optional[str] = None, pair_id: Optional[str] = None, tag: str = _TAG_NON_LLM,
    ) -> bool:
        try:
            await self.db.insert(umo, cid, user_id, role, content, message_id, pair_id, tag)
            return True
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 写入失败: {e}")
            return False

    async def _safe_update_tag(self, umo: str, cid: str, message_id: str, new_tag: str) -> int:
        try:
            return await self.db.update_tag(umo, cid, message_id, new_tag)
        except Exception as e:
            logger.warning(f"{self._log_prefix()} 更新 tag 失败: {e}")
            return 0
