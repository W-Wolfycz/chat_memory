"""cache 友好型上下文窗口：块对齐的锚点计算与进程内缓存。

思路（与 docs/ 里那份设计一致）：

- 窗口上限 ``total``（配置的 ``limit_rounds``，轮或条由调用方决定）；
- 保留比例 ``retain_pct``（1~100）。触发裁剪时把窗口回退到
  ``low = ceil(total * retain_pct / 100)``，留出 ``total - low`` 的空间让新消息**追加**；
- 只要没越过 ``total``，窗口起点（锚点）**一个字节都不动** —— 这是前缀缓存命中的全部来源；
- 越过 ``total`` 才把锚点前移一块（回到 ``low``），因此每 ``total - low + 1`` 个单位
  才重建一次前缀。

本模块只做纯计算与内存缓存，不碰数据库。取数/计数由调用方（storage）完成：
判断"是否超限"不需要 COUNT —— 取 ``total + 1`` 个单位的行，看够不够就够了。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# 锚点表上限：一个进程里正常也就几十个会话 × 几种过滤组合。
# 超了就整体清空（重建一次的成本是微秒级，不值得为它写淘汰策略）。
_MAX_ANCHORS = 1000

# storage 对 limit_* 的统一钳制上限：超过它就无法"多取一条"判断是否超限
_MAX_FETCH_UNITS = 1000

# 单位
UNIT_ROUND = "round"
UNIT_MESSAGE = "message"


def row_coord(row: dict, key_field: str = "record_id") -> int:
    """取一行的窗口坐标：合并存储的会话内 ``seq``，落在 ``record_id`` 字段里。
    缺失（桩数据/老格式）返回 0，调用方据此跳过锚点写入。

    全库只在这里解释"坐标从哪个字段取"，避免调用方各自兜底导致语义漂移。
    """
    try:
        return int(row.get(key_field) or row.get("seq") or row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def block_low(total: int, retain_pct: int) -> int:
    """裁剪后回到的窗口大小：``ceil(total * retain_pct / 100)``。

    向上取整而不是向下：``retain_pct`` 是「至少保留这么多比例」的承诺，
    向下取整会让实际保留率低于配置值（例如 total=33、retain=80 → floor=26，
    实际下界 78.8% < 80%）。
    """
    total = max(1, int(total))
    retain_pct = max(1, min(100, int(retain_pct)))
    return max(1, min(total, math.ceil(total * retain_pct / 100)))


def window_disabled_reason(total: int, retain_pct: int) -> str:
    """块对齐是否应当停用；返回空串表示可用，否则给出停用原因。

    两种情形都退化成「逐条裁剪」——那等于旧行为，必须让人看得见，不能静默按
    "周期 1" 跑（每条消息都移动锚点，缓存收益为零）。
    """
    if retain_pct >= 100:
        return "retain_ratio=100（等价于关闭块对齐）"
    if block_low(total, retain_pct) >= max(1, int(total)):
        # 例如 total=2、retain=80 → low=2：窗口一满就回退到满，周期为 1。
        return f"窗口过小（total={total}、retain={retain_pct}%）无法留出增量空间"
    if total >= _MAX_FETCH_UNITS:
        # 取数侧（storage）统一钳到 1000：多取一条的判断会失效，块对齐无意义。
        return f"窗口上限 {total} 达到取数硬上限 {_MAX_FETCH_UNITS}"
    return ""


@dataclass
class AnchorState:
    """一个窗口的锚点状态（只存在内存里）。"""

    anchor: int                                  # 窗口起点（会话内 seq）
    hits: int = 0                                # 复用次数（诊断用）
    advances: int = 0                            # 跳块次数（诊断用）

    def as_dict(self) -> dict:
        return {"anchor": self.anchor, "hits": self.hits, "advances": self.advances}


class AnchorStore:
    """进程内锚点表：``scope_key -> AnchorState``。

    丢了就重建（一次查询，微秒级），所以不持久化——持久化反而要处理
    「库被替换/回滚后锚点指向不存在的行」这类一致性问题。
    """

    def __init__(self, max_entries: int = _MAX_ANCHORS) -> None:
        self._map: dict[str, AnchorState] = {}
        self._max = max(1, int(max_entries))

    def get(self, key: str, *, floor: int = 0) -> Optional[int]:
        """取锚点；``floor`` 是本次查询能取到的最小坐标（会话内 ``seq`` 从 1 起）。

        锚点小于 floor 说明库被重建过（结构线迁移把 seq 归零）或会话被清空过，
        此时返回 None 让调用方重新起算——不能拿一个指向"未来"的锚点去查。
        """
        state = self._map.get(key)
        if state is None:
            return None
        if state.anchor < floor:
            self._map.pop(key, None)
            return None
        state.hits += 1
        return state.anchor

    def set(self, key: str, anchor: int) -> None:
        if len(self._map) >= self._max and key not in self._map:
            self._map.clear()          # 极简淘汰：整体清空，重建成本可忽略
        state = self._map.get(key)
        if state is None:
            self._map[key] = AnchorState(anchor=anchor)
        else:
            if anchor != state.anchor:
                state.advances += 1
            state.anchor = anchor

    def forget(self, key: str) -> None:
        self._map.pop(key, None)

    def clear(self) -> None:
        self._map.clear()

    def size(self) -> int:
        return len(self._map)

    def snapshot(self) -> dict[str, dict]:
        return {key: state.as_dict() for key, state in self._map.items()}


def scope_key(
    *,
    umo: str,
    cid: Optional[str],
    user_id: Optional[str],
    persona_id: Optional[str],
    cross_umo: bool,
    full_group: bool,
    include_kinds: Optional[set[str]],
    statuses: Optional[set[str]],
    all_match: bool,
    unit: str,
    total: int,
    retain_pct: int,
) -> str:
    """构造锚点的作用域键。

    **只有真正参与过滤的项才进 key**——未启用的配置（比如没开
    ``filter_by_persona`` 时的 ``persona_id``、没开白名单时的 ``include_kinds``）
    一律不写进去，否则同一份可见集合会因为无关配置变化而重建前缀。

    键里必须包含：可见集合的边界（umo/cid/user/persona/scope）+ 过滤条件 +
    窗口参数（单位/上限/比例）。窗口参数变化等于用户换了窗口，锚点自然失效。

    """
    kinds = ",".join(sorted(include_kinds)) if include_kinds else ""
    stat = ",".join(sorted(statuses)) if statuses else ""
    parts = [
        f"unit={unit}",
        f"umo={umo}",
        f"cid={cid or ''}",
        f"user={user_id or ''}",
        f"persona={persona_id or ''}",
        f"cross={int(bool(cross_umo))}",
        f"group={int(bool(full_group))}",
        f"kinds={kinds}",
        f"status={stat}",
        f"all={int(bool(all_match))}",
        f"total={int(total)}",
        f"retain={int(retain_pct)}",
    ]
    return "|".join(parts)


@dataclass
class WindowPlan:
    """一次查询的窗口决策结果。"""

    enabled: bool
    total: int
    low: int
    retain_pct: int
    anchor: Optional[int] = None
    advanced: bool = False
    reason: str = ""

    @property
    def fetch_limit(self) -> int:
        """取数上限：多取一条用于判断"是否超限"。

        不需要 COUNT —— 取到 ``total + 1`` 说明越过上限，取不到说明没越。
        """
        return max(2, min(_MAX_FETCH_UNITS, self.total + 1))


def plan_window(
    *,
    total: int,
    retain_pct: int,
    store: AnchorStore,
    key: str,
    floor: int = 0,
) -> WindowPlan:
    """组装窗口决策：算出 low、取当前锚点、判断是否需要停用。"""
    total = max(1, int(total))
    retain_pct = max(1, min(100, int(retain_pct)))
    reason = window_disabled_reason(total, retain_pct)
    if reason:
        return WindowPlan(enabled=False, total=total, low=total,
                          retain_pct=retain_pct, reason=reason)
    low = block_low(total, retain_pct)
    anchor = store.get(key, floor=floor)
    return WindowPlan(enabled=True, total=total, low=low,
                      retain_pct=retain_pct, anchor=anchor)


def advance_anchor(
    *,
    plan: WindowPlan,
    store: AnchorStore,
    key: str,
    rows: list[dict],
    key_field: str = "record_id",
) -> list[dict]:
    """按「是否超限」裁一次窗口，返回要保留的行（时间正序）。

    ``rows`` 是本次按 ``anchor`` 取回的行（条数 = ``total + 1`` 或不足）。
    ``total + 1`` 这一条就是为了"不 COUNT 也能判断是否超限"。

    - 未超限：锚点不动，返回全部行（块内前缀稳定的来源）；
    - 超限：把窗口裁到 ``low``，锚点前移到裁剪点，并做**轮首对齐**：
      若裁剪点落在某轮 assistant 段中间，就看这一轮 user 是否也在本次结果里
      （一定在：旧锚点本身就是轮首），在就前移过去；不在就按"独立轮首"处理。
      若前移后条数仍 ≤ ``total``（正常情况）就用对齐后的锚点，否则退回未对齐的位置
      —— 宁可切一轮，也不超配置上限。
    """
    if not plan.enabled:
        return rows

    def coord(row: dict) -> int:
        return row_coord(row, key_field)

    if len(rows) <= plan.total:
        if plan.anchor is None and rows:
            # 首次起算：窗口首行若是某轮的第 N 段（新会话由 bot 主动开场），
            # 锚点要落到紧接着的那条 user 上，否则之后每轮都从半轮开始。
            start = 0
            if str(rows[0].get("role") or "") == "assistant":
                for index, row in enumerate(rows):
                    if str(row.get("role") or "") == "user":
                        start = index
                        break
            if start < len(rows) and coord(rows[start]):
                store.set(key, coord(rows[start]))
        return rows

    cut = len(rows) - plan.low
    aligned = cut
    first = rows[cut]
    if str(first.get("role") or "") == "assistant":
        # 裁剪点落在某轮的第 N 段 → 向前找这一轮的 user 行（一定在 rows[:cut] 里：
        # 旧锚点本身就是轮首，所以新锚点所在轮的轮首不会早于它）
        turn = str(first.get("turn_id") or "")
        if turn:
            for idx in range(cut - 1, -1, -1):
                row = rows[idx]
                if str(row.get("turn_id") or "") != turn:
                    continue
                if str(row.get("role") or "") != "user":
                    continue
                if len(rows) - idx <= plan.total:
                    aligned = idx      # 前移整轮仍在上限内 → 对齐到轮首
                # 前移会超上限 → 保持 cut，接受从半轮开始（配置上限优先）
                break
    kept = rows[aligned:]
    if kept and coord(kept[0]):
        plan.anchor = coord(kept[0])
        plan.advanced = True
        store.set(key, plan.anchor)
    return kept
