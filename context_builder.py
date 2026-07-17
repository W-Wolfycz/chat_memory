"""ChatMemory takeover 上下文规整器。"""

from collections.abc import Iterable
from typing import Optional


FULL_GROUP_CONTEXT_INSTRUCTION = """[ChatMemory 群聊历史解释规则]
ChatMemory 提供的历史 contexts 中，role=user 可能是合并后的连续群聊发言，
不代表其中所有内容都来自当前用户。请严格依据每段的 [当前发言者] / [其他发言者]
标记分别归因，不得把其他发言者的行为、饮食、偏好、关系或承诺归到当前用户。
role=assistant 是你自己的历史回复；当前用户的新请求位于历史 contexts 之后。
无法确定事实归属时，不要擅自断言。"""


def strip_reasoning_prefix(text: str) -> str:
    """剥离 AstrBot 错误序列化进 Plain 的 reasoning parts 前缀。"""
    if not text.startswith("[{'type': 'think'"):
        return text
    depth = 0
    index = 0
    length = len(text)
    in_string = False
    quote = ""
    while index < length:
        char = text[index]
        if in_string:
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == quote:
                in_string = False
            index += 1
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[index + 1:].lstrip()
        index += 1
    return text


def extract_time_str(created_at) -> str:
    if not created_at:
        return ""
    value = str(created_at)
    if len(value) >= 19 and value[10] in (" ", "T"):
        return value[5:19].replace("-", "/").replace("T", " ")
    return value


def is_pure_media(record: dict, media_kinds: set[str]) -> bool:
    kinds = record.get("content_kind") or []
    if not kinds:
        return False
    return all(kind in media_kinds for kind in kinds)


class TakeoverContextBuilder:
    def __init__(
        self,
        media_kinds: Iterable[str],
        current_user_id: str = "",
        full_group: bool = False,
        proactive_status: str = "proactive",
        orphan_status: str = "orphan",
    ) -> None:
        self.media_kinds = set(media_kinds)
        self.current_user_id = str(current_user_id or "").strip()
        self.full_group = bool(full_group)
        self.proactive_status = proactive_status
        self.orphan_status = orphan_status

    def normalize(
        self,
        records: list[dict],
        max_records: Optional[int] = None,
        max_chars: int = 0,
    ) -> list[dict]:
        records = [
            record for record in records
            if not is_pure_media(record, self.media_kinds)
        ]

        while records and records[0].get("role") != "user":
            records.pop(0)
        while (
            records
            and records[-1].get("role") == "assistant"
            and records[-1].get("llm_status")
            in (self.proactive_status, self.orphan_status)
        ):
            records.pop()
        if max_records is not None:
            records = records[-max(1, int(max_records)):]
            while records and records[0].get("role") != "user":
                records.pop(0)

        formatted: list[dict] = []
        for record in records:
            content = strip_reasoning_prefix(record.get("content", "") or "")
            role = record.get("role", "user")
            llm_status = record.get("llm_status", "")
            is_solo = (
                role == "assistant"
                and llm_status in (self.proactive_status, self.orphan_status)
            )

            if role == "user":
                content = self._apply_prefix(record, content)
            elif is_solo:
                tag = "主动" if llm_status == self.proactive_status else "未配对"
                content = self._apply_solo_prefix(record, content, tag)

            formatted.append({"role": role, "content": content, "_solo": is_solo})

        formatted = self._merge_with_solo(formatted)
        while formatted and formatted[0]["role"] != "user":
            formatted.pop(0)
        while (
            formatted
            and formatted[-1]["role"] == "assistant"
            and formatted[-1].get("_solo")
        ):
            formatted.pop()

        if max_chars > 0:
            # 这是字符预算，不假装等价于 tokenizer token 数；始终保留最新一条
            # user，避免裁剪后上下文以 assistant 开头。
            while sum(
                len(str(item.get("content", ""))) for item in formatted
            ) > max_chars:
                next_user = next(
                    (
                        index for index, item in enumerate(formatted[1:], start=1)
                        if item.get("role") == "user"
                    ),
                    None,
                )
                if next_user is None:
                    break
                formatted = formatted[next_user:]

        for context in formatted:
            context.pop("_solo", None)
            context["_no_save"] = True
        return formatted

    def _apply_prefix(self, record: dict, content: str) -> str:
        parts: list[str] = []
        time_str = extract_time_str(record.get("created_at"))
        if time_str:
            parts.append(f"[{time_str}]")
        if self.full_group:
            record_user_id = str(record.get("user_id") or "").strip()
            if self.current_user_id:
                speaker_tag = (
                    "当前发言者"
                    if record_user_id == self.current_user_id
                    else "其他发言者"
                )
            else:
                speaker_tag = "发言者"
            parts.append(f"[{speaker_tag}]")
        sender = record.get("sender_nickname") or record.get("user_id") or "?"
        parts.append(f"{sender}:")
        prefix = " ".join(parts)
        return f"{prefix} {content}" if content else prefix

    def _apply_solo_prefix(self, record: dict, content: str, tag: str) -> str:
        parts: list[str] = []
        time_str = extract_time_str(record.get("created_at"))
        if time_str:
            parts.append(f"[{time_str}]")
        parts.append(f"[{tag}]")
        prefix = " ".join(parts)
        return f"{prefix} {content}" if content else prefix

    @staticmethod
    def _merge_with_solo(contexts: list[dict]) -> list[dict]:
        merged: list[dict] = []
        for context in contexts:
            last = merged[-1] if merged else None
            if (
                last
                and last["role"] == context["role"]
                and last.get("_solo") == context.get("_solo")
            ):
                last["content"] += "\n\n" + context["content"]
            else:
                merged.append(dict(context))
        return merged
