"""群聊名：现查 OneBot get_group_list（不落库），进程内缓存；查询台按群号补名字。

回答"有 API 现查吗"：有。OneBot 11 的 get_group_list 一次返回机器人所在的所有群
（群号 + 群名），NapCat / LLOneBot 都支持。查询台只显示，不写库。
"""

import time
from typing import Any

from astrbot.api import logger

# 群名缓存有效期：群名几乎不变，但也不能一次拉到就永远信（改名/退群）
_GROUP_NAMES_TTL_SEC = 6 * 3600
# 调用失败后的重试冷却：平台没连上时不至于每次翻页都去撞一次
_GROUP_NAMES_RETRY_SEC = 300


class GroupNameCache:
    """进程内群号 → 群名缓存（不落库，重载即失效，符合"现查"语义）。"""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.fetched_at = 0.0
        self.failed_at = 0.0

    def fresh(self) -> bool:
        if not self.names:
            return False
        return time.time() - self.fetched_at < _GROUP_NAMES_TTL_SEC

    def cooling(self) -> bool:
        return time.time() - self.failed_at < _GROUP_NAMES_RETRY_SEC

    def update(self, pairs: list[tuple[str, str]]) -> None:
        self.names = {str(gid): str(name) for gid, name in pairs if gid and name}
        self.fetched_at = time.time()

    def mark_failed(self) -> None:
        self.failed_at = time.time()


async def fetch_group_names(call: Any) -> list[tuple[str, str]]:
    """调 get_group_list，返回 ``[(群号, 群名)]``；失败返回空列表（绝不外抛）。"""
    try:
        data = await call("get_group_list")
    except Exception as exc:  # noqa: BLE001 - 平台不支持/未连接都只是"没有群名"
        # 必须留痕：否则"客户端不支持该接口"与"Bot 不在任何群"在日志里完全同形。
        logger.debug("[ChatMemory][group_names] get_group_list 失败: %s", type(exc).__name__)
        return []
    groups = data if isinstance(data, list) else None
    if groups is None and isinstance(data, dict):
        groups = data.get("data")
    if not isinstance(groups, list):
        logger.debug(
            "[ChatMemory][group_names] get_group_list 返回形状异常: %s",
            type(data).__name__,
        )
        return []
    pairs: list[tuple[str, str]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        gid = item.get("group_id") or item.get("group")
        name = item.get("group_name") or item.get("name")
        if gid and name:
            pairs.append((str(gid), str(name)))
    return pairs
