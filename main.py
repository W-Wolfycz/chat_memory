"""ChatMemory — 独立对话记录存档，以 UMO + conversation_id + 用户ID 为维度存储对话文本。"""

import asyncio
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain

from .storage import DBManager

# ── 模块级单例，供其他插件直接 import 调用 ────────────────

_db: Optional[DBManager] = None
_max_len: int = 500


async def query_history(umo: str, conversation_id: str, user_id: str, limit: int = 20) -> list[dict]:
    """查询指定用户在指定会话+对话的对话历史。返回 [{role, content, created_at}, ...]"""
    if _db is None:
        return []
    return await _db.query_latest(umo, conversation_id, user_id, limit)


async def query_latest(umo: str, conversation_id: str, user_id: str, limit: int = 10) -> list[dict]:
    """查询最近 N 条记录。"""
    if _db is None:
        return []
    return await _db.query_latest(umo, conversation_id, user_id, limit)


async def count_records(umo: str, conversation_id: str, user_id: str) -> int:
    """统计记录数。"""
    if _db is None:
        return 0
    return await _db.count(umo, conversation_id, user_id)


class ChatMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        global _db, _max_len

        self.max_len = max(1, config.get("max_content_length", 500))
        self.auto_cleanup_days = config.get("auto_cleanup_days", 0)
        self.debug_mode = config.get("debug_mode", False)

        data_dir = Path(context.get_data_dir()) / "chat_memory"
        self.db = DBManager(data_dir)
        _db = self.db
        _max_len = self.max_len

        logger.info("[ChatMemory] 对话记录存档已启用")

    def _log(self, msg: str):
        if self.debug_mode:
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

    async def _get_curr_cid(self, umo: str) -> str:
        try:
            conv_mgr = self.context.conversation_manager
            return await conv_mgr.get_curr_conversation_id(umo) or ""
        except Exception:
            return ""

    # ── 捕获用户消息 ───────────────────────────────────

    @filter.on_llm_request()
    async def capture_user(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.get_extra("chat_memory_captured"):
            return

        user_text = self._extract_text(event)
        if not user_text:
            return

        umo = getattr(event, "unified_msg_origin", "")
        user_id = event.get_sender_id() or ""
        if not umo or not user_id:
            return

        cid = await self._get_curr_cid(umo)
        if not cid:
            return

        event.set_extra("chat_memory_captured", True)
        event.set_extra("chat_memory_cid", cid)
        event.set_extra("chat_memory_umo", umo)
        event.set_extra("chat_memory_uid", user_id)
        event.set_extra("chat_memory_user_text", user_text[: self.max_len])

    # ── 捕获 BOT 回复 + 检测 reset/new ──────────────────

    @filter.on_decorating_result(priority=10)
    async def capture_bot(self, event: AstrMessageEvent):
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return

        # 检测 /reset 或 /new：AstrBot 内置命令会设置此标记
        if event.get_extra("_clean_group_context_session"):
            await self._on_reset_or_new(event, umo)
            return

        # 正常 LLM 回复捕获：BOT 回复成功时，成对写入 user + assistant
        cid = event.get_extra("chat_memory_cid")
        user_id = event.get_extra("chat_memory_uid")
        user_text = event.get_extra("chat_memory_user_text")
        if not cid or not user_id:
            return

        result = event.get_result()
        if not result or not result.is_llm_result():
            return

        chain = result.chain
        if not chain:
            return

        bot_text = "".join(comp.text for comp in chain if isinstance(comp, Plain)).strip()
        if not bot_text:
            return

        if user_text:
            asyncio.create_task(self._safe_insert(umo, cid, user_id, "user", user_text))
            self._log(f"[ChatMemory] user -> {user_id}@{cid[:8]}: {user_text[:60]}...")

        content = bot_text[: self.max_len]
        asyncio.create_task(self._safe_insert(umo, cid, user_id, "assistant", content))
        self._log(f"[ChatMemory] bot -> {user_id}@{cid[:8]}: {content[:60]}...")

    # ── reset / new 处理 ─────────────────────────────

    async def _on_reset_or_new(self, event: AstrMessageEvent, umo: str):
        """区分 /reset 和 /new，分别处理。

        /reset: CID 不变，清空历史 → 清除该 CID 下所有存档记录。
        /new:   产生新 CID → 旧 CID 记录保留，不需要操作。
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
            # /reset: CID 不变，清空该对话存档
            deleted = await self.db.delete_by_conversation(umo, cid)
            if deleted > 0:
                logger.info(f"[ChatMemory] /reset（CID={cid[:8]}），清除 {deleted} 条存档记录")
        else:
            # /new: 新 CID 已创建，旧记录自然保留，无需操作
            self._log(f"[ChatMemory] /new（CID={cid[:8]}），新对话开始")

    # ── 公开实例方法（供 context.get_registered_star 调用）───

    async def query_history(self, umo: str, conversation_id: str, user_id: str, limit: int = 20) -> list[dict]:
        return await self.db.query_latest(umo, conversation_id, user_id, limit)

    async def query_latest(self, umo: str, conversation_id: str, user_id: str, limit: int = 10) -> list[dict]:
        return await self.db.query_latest(umo, conversation_id, user_id, limit)

    async def count_records(self, umo: str, conversation_id: str, user_id: str) -> int:
        return await self.db.count(umo, conversation_id, user_id)

    # ── 内部工具 ──────────────────────────────────────

    async def _safe_insert(self, umo: str, cid: str, user_id: str, role: str, content: str):
        try:
            await self.db.insert(umo, cid, user_id, role, content)
        except Exception as e:
            logger.warning(f"[ChatMemory] 写入失败: {e}")
