"""cache 友好窗口的纯逻辑：块对齐、锚点缓存、轮首对齐。

这些错误都属于「跑一遍看不出来」：保留比例悄悄低于配置、锚点悄悄漂移导致
前缀缓存失效、窗口从半轮开始 —— 都不会抛异常，只会让成本变高或回答质量变差。
"""

import pathlib
import sys
import types

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
PKG = "chat_memory"
if PKG not in sys.modules:
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = pkg

mod = types.ModuleType(f"{PKG}.context_window")
sys.modules[f"{PKG}.context_window"] = mod
exec(compile((PLUGIN_DIR / "context_window.py").read_text(encoding="utf-8"),
             "context_window.py", "exec"), mod.__dict__)
setattr(sys.modules[PKG], "context_window", mod)


def _rows(coords):
    """构造最小行：交替 user / assistant，同轮 assistant 与 user 共享 turn_id。"""
    out = []
    for index, coord in enumerate(coords):
        role = "user" if index % 2 == 0 else "assistant"
        out.append({
            "record_id": coord,
            "role": role,
            "turn_id": f"t{index // 2}",
        })
    return out


def test_block_low_rounds_up():
    """守的静默失效：向下取整会让实际保留率低于配置（T=33、R=80 → 26 轮 = 78.8%），
    用户以为配了 80% 却在多付重建成本。
    """
    assert mod.block_low(33, 80) == 27      # 26.4 → 27，不是 26
    assert mod.block_low(30, 80) == 24      # 整数不跳
    assert mod.block_low(100, 90) == 90
    assert mod.block_low(1, 80) == 1        # 不会小于 1
    assert mod.block_low(10, 100) == 10     # 不超过 total


def test_window_disabled_cases():
    """守的静默失效：R=100 或窗口过小时，块对齐会退化成「每条消息都移动锚点」——
    用户以为开着优化、实际缓存收益为零，所以必须显式停用并留原因。
    """
    assert "retain_ratio=100" in mod.window_disabled_reason(30, 100)  # 明确是"关闭"而非"窗口过小"
    assert mod.window_disabled_reason(30, 100) != ""
    assert mod.window_disabled_reason(2, 80) != ""        # low == total，周期 1
    assert mod.window_disabled_reason(30, 80) == ""       # 正常可用
    # 达到 storage 的取数硬上限后无法"多取一条"，块对齐必须显式停用
    assert "取数硬上限" in mod.window_disabled_reason(1000, 80)
    # 停用时 plan 不启用，调用方走旧路径
    plan = mod.plan_window(total=30, retain_pct=100,
                           store=mod.AnchorStore(), key="k")
    assert plan.enabled is False and plan.reason


def test_anchor_stable_inside_block():
    """守的静默失效（核心不变量）：块内锚点必须一动不动，否则每来一条消息
    前缀就变一次，缓存命中率归零——而功能看起来完全正常。
    """
    store = mod.AnchorStore()
    key = "k"
    plan = mod.plan_window(total=10, retain_pct=80, store=store, key=key)
    assert plan.enabled and plan.low == 8

    # 第一次：11 条 → 超限，裁到 8，锚点 = 第 4 条
    rows = _rows(range(1, 12))
    kept = mod.advance_anchor(plan=plan, store=store, key=key, rows=rows)
    assert plan.low <= len(kept) <= plan.total, len(kept)
    anchor = store.get(key)
    assert anchor == kept[0]["record_id"]

    # 块内再加两条（9 条、10 条 ≤ 上限）：锚点必须一动不动
    for extra in (11, 12):
        plan2 = mod.plan_window(total=10, retain_pct=80, store=store, key=key)
        assert plan2.anchor == anchor, "块内锚点漂移了"
        kept2 = mod.advance_anchor(plan=plan2, store=store, key=key,
                                   rows=_rows(range(anchor, extra + 1)))
        assert kept2[0]["record_id"] == anchor
        assert plan2.advanced is False, "块内不该跳块"

    # 第 11 条（> total）才跳块，锚点前移一块
    plan3 = mod.plan_window(total=10, retain_pct=80, store=store, key=key)
    kept3 = mod.advance_anchor(plan=plan3, store=store, key=key,
                               rows=_rows(range(anchor, 14)))
    assert plan3.advanced is True
    assert kept3[0]["record_id"] != anchor
    # 裁到 low 后可能因轮首对齐多带几条，但不越过上限
    assert plan3.low <= len(kept3) <= plan3.total


def test_first_anchor_skips_leading_assistant():
    """守的静默失效：首次起算（还没有锚点、窗口未超限）时若首行是某轮的第 N 段
    （新会话由 bot 主动开场），锚点会落在轮中间——之后每轮都从半轮开始，模型
    看到"没有问题的回答"。锚点必须落到紧接着的那条 user 上。
    """
    store = mod.AnchorStore()
    plan = mod.plan_window(total=30, retain_pct=80, store=store, key="k")
    rows = [
        {"record_id": 11, "role": "assistant", "turn_id": "proactive1"},
        {"record_id": 12, "role": "user", "turn_id": "t1"},
        {"record_id": 13, "role": "assistant", "turn_id": "t1"},
    ]
    mod.advance_anchor(plan=plan, store=store, key="k", rows=rows)
    assert store.get("k") == 12, "锚点落在 assistant 段上了"

    # 首行本来就是 user 时不动
    store2 = mod.AnchorStore()
    plan2 = mod.plan_window(total=30, retain_pct=80, store=store2, key="k2")
    mod.advance_anchor(plan=plan2, store=store2, key="k2", rows=[
        {"record_id": 21, "role": "user", "turn_id": "t2"},
        {"record_id": 22, "role": "assistant", "turn_id": "t2"},
    ])
    assert store2.get("k2") == 21


def test_advance_on_overflow():
    """守的静默失效：超过上限却不裁 → 窗口无限增长（token 爆掉）。"""
    store = mod.AnchorStore()
    key = "k"
    plan = mod.plan_window(total=10, retain_pct=80, store=store, key=key)
    rows = _rows(range(1, 20))            # 19 条 > 10
    kept = mod.advance_anchor(plan=plan, store=store, key=key, rows=rows)
    assert plan.low <= len(kept) <= plan.total, len(kept)
    assert plan.advanced is True
    assert kept[0]["role"] in ("user", "assistant")


def test_advance_aligns_to_turn_head():
    """守的静默失效：裁到 low 条时若落在 assistant 段中间，上下文会从「半轮」
    开始——用户问了一句，模型只看到后半句回答。必须前移到该轮 user。
    """
    store = mod.AnchorStore()
    key = "k"
    plan = mod.plan_window(total=6, retain_pct=83, store=store, key=key)  # low = 5
    assert plan.low == 5

    # 构造：第 8 条是 assistant 的第 2 段，low=5 → cut 落在它上面
    rows = [
        {"record_id": 1, "role": "user", "turn_id": "a"},
        {"record_id": 2, "role": "assistant", "turn_id": "a"},
        {"record_id": 3, "role": "user", "turn_id": "b"},
        {"record_id": 4, "role": "assistant", "turn_id": "b"},
        {"record_id": 5, "role": "user", "turn_id": "c"},
        {"record_id": 6, "role": "assistant", "turn_id": "c"},
        {"record_id": 7, "role": "user", "turn_id": "d"},
        {"record_id": 8, "role": "assistant", "turn_id": "d"},
        {"record_id": 9, "role": "assistant", "turn_id": "d"},
    ]
    kept = mod.advance_anchor(plan=plan, store=store, key=key, rows=rows)
    # cut = 9-5 = 4 → rows[4] 是 user（turn c），本身对齐，锚点 5
    assert kept[0]["record_id"] == 5
    assert len(kept) == 5

    # 换一组：cut 正好落在某轮的**第 2 段 assistant** 上 → 必须前移到该轮 user
    store2 = mod.AnchorStore()
    plan2 = mod.plan_window(total=6, retain_pct=67, store=store2, key="k2")  # low = 5
    rows2 = [
        {"record_id": 1, "role": "user", "turn_id": "a"},
        {"record_id": 2, "role": "assistant", "turn_id": "a"},
        {"record_id": 3, "role": "user", "turn_id": "b"},
        {"record_id": 4, "role": "assistant", "turn_id": "b"},
        {"record_id": 5, "role": "assistant", "turn_id": "b"},   # b 的第 2 段
        {"record_id": 6, "role": "user", "turn_id": "c"},
        {"record_id": 7, "role": "assistant", "turn_id": "c"},
    ]
    # cut = 7-5 = 2（rows[2] 是 user(b)），人为把裁剪点挪到 rows[3] 来验证对齐
    plan2.low = 4                       # 让 cut = 3 → 落在 assistant(b)
    kept2 = mod.advance_anchor(plan=plan2, store=store2, key="k2", rows=rows2)
    assert kept2[0]["role"] == "user", "切到了 assistant 段中间"
    assert kept2[0]["record_id"] == 3   # 前移到 b 轮的 user

    # 前移整轮会超过上限时退回（配置上限优先，宁可切轮）
    store3 = mod.AnchorStore()
    plan3 = mod.plan_window(total=4, retain_pct=25, store=store3, key="k3")  # low=1
    plan3.low = 4                        # cut = 6-4 = 2 → rows[2] 是 assistant(a)
    rows3 = [
        {"record_id": 1, "role": "user", "turn_id": "a"},
        {"record_id": 2, "role": "assistant", "turn_id": "a"},
        {"record_id": 3, "role": "assistant", "turn_id": "a"},   # cut 落在这里
        {"record_id": 4, "role": "assistant", "turn_id": "a"},
        {"record_id": 5, "role": "assistant", "turn_id": "a"},
        {"record_id": 6, "role": "assistant", "turn_id": "a"},
    ]
    kept3 = mod.advance_anchor(plan=plan3, store=store3, key="k3", rows=rows3)
    # 轮首在 idx0，前移后 6 条 > total(4) → 退回未对齐位置（接受切轮）
    assert kept3[0]["record_id"] == 3
    assert kept3[0]["role"] == "assistant"


def test_anchor_invalidated_below_floor():
    """守的静默失效：库被重建（seq 归零）后旧锚点指向不存在的坐标，
    拿它去查会返回空上下文——必须当无效处理并重新起算。
    """
    store = mod.AnchorStore()
    store.set("k", 100)
    assert store.get("k", floor=0) == 100
    assert store.get("k", floor=200) is None      # 锚点 < floor → 失效
    assert store.get("k") is None                  # 已被移除


def test_scope_key_skips_unused_config():
    """守的静默失效：把未启用的配置写进 key，会让同一份可见集合因为无关配置
    变化而重建前缀（缓存白丢）。
    """
    base = dict(
        umo="BOT1:GroupMessage:g1", cid="c1", user_id="u1",
        persona_id=None, cross_umo=False, full_group=False,
        include_kinds={"text"}, statuses={"llm_success"}, all_match=False,
        unit=mod.UNIT_MESSAGE, total=30, retain_pct=80,
    )
    k1 = mod.scope_key(**base)
    # 未启用 persona 过滤时 persona_id 传 None，与另一个 None 不同值也不应影响…
    k2 = mod.scope_key(**{**base, "persona_id": None})
    assert k1 == k2
    # 但真正启用后必须区分
    k3 = mod.scope_key(**{**base, "persona_id": "p_demo"})
    assert k1 != k3
    # 过滤条件/窗口参数变化必须换 key
    assert k1 != mod.scope_key(**{**base, "include_kinds": {"text", "image"}})
    assert k1 != mod.scope_key(**{**base, "total": 40})
    assert k1 != mod.scope_key(**{**base, "retain_pct": 70})
    assert k1 != mod.scope_key(**{**base, "unit": mod.UNIT_ROUND})
