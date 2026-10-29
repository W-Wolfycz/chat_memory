"""合并存储（影子层）只读查询——**V2 读路径**，旧表的 V1 读路径保持不变。

切换读取时两边要能对拍、能回退，所以 V1 一行不改，V2 全部从
``chat_memory_messages``（物理消息，跨实例唯一）+ ``chat_memory_message_bots``
（每实例参与行：role / llm_status / send_status / persona / target / 本实例 mid 与时刻）
+ ``chat_memory_reply_messages``（本轮的计划正文与轮次状态，一轮一条）读。

两个要点：

1. **每实例视图**：同一条物理消息只有一行，参与行每实例一份。查询按 ``(msg_uid, umo)``
   取一行（同一实例换过账号时会有多份 self_id，取信息最全的那份），于是"某个会话看到的
   样子"与旧表的每实例视图等价。
2. **一轮一条逻辑回复**：按 ``turn_id`` 归组（``turn_id`` 是发送侧生成的 uuid4，全局唯一），
   实际发出的每段并进同一组，段序用平台号 ``platform_seq``。所以一轮的"实际形态"＝该轮
   平台键行按 ``platform_seq`` 拼接（就是群里看到的样子），该轮没有任何平台键行时才回落
   计划成员行。写路径不再维护 ``reply_group``（结构线 6 删除该列）——"哪条消息是同一轮
的回复"由 ``turn_id`` 表达，组合键 `rg:<turn_id>` 在读侧现推即可。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from astrbot.api import logger

from .relation_codec import (
    parse_relation_data,
    render_content_template,
    strip_relation_tokens,
)

# 让 ``storage._scope_filter`` 生成的条件（裸列名 umo / platform_id / user_id /
# message_type / session_id）能直接套在这一层上——scope 语义两边必须一致，
# 所以不重写一份，只把影子层投影成同名列。
_VIEW_CTE = """
WITH picked AS (
    SELECT b.msg_uid, b.role, b.llm_status, b.send_status, b.persona_id, b.target_id,
           b.platform_id, b.self_id, b.umo, b.conversation_id, b.turn_id,
           b.platform_message_id, b.raw_timestamp,
           ROW_NUMBER() OVER (
               PARTITION BY b.msg_uid, b.umo
               ORDER BY (COALESCE(b.platform_message_id, '') <> '') DESC,
                        (b.role = 'assistant') DESC,
                        b.id ASC
           ) AS rn
    FROM chat_memory_message_bots b
),
view AS (
    SELECT m.msg_uid, m.seq, m.content, m.content_kind, m.created_at,
           m.platform_timestamp, m.platform_seq,
           m.sender_id, m.sender_nickname, m.relation_data, m.recalled_at,
           m.deleted_at, m.platform_name, m.message_type, m.session_id,
           p.role, p.llm_status, p.send_status, p.persona_id, p.target_id,
           p.platform_id, p.self_id, p.umo, p.conversation_id, p.turn_id,
           p.platform_message_id, p.raw_timestamp,
           CASE WHEN p.role = 'user' THEN m.sender_id
                ELSE COALESCE(p.target_id, '') END AS user_id,
           CASE WHEN m.message_type = 'GroupMessage' THEN m.session_id
                ELSE '' END AS group_id
    FROM picked p JOIN chat_memory_messages m ON m.msg_uid = p.msg_uid
    WHERE p.rn = 1
)
"""

# 视图列（顺序即行下标，_C 由它派生，两者不会写歪）
_COLS = (
    "msg_uid", "seq", "content", "content_kind", "created_at", "platform_timestamp",
    "platform_seq", "sender_id", "sender_nickname",
    "relation_data", "recalled_at", "deleted_at", "platform_name", "message_type",
    "session_id", "role", "llm_status", "send_status", "persona_id", "target_id",
    "platform_id", "self_id", "umo", "conversation_id", "turn_id",
    "platform_message_id", "raw_timestamp", "user_id", "group_id",
)
_VIEW_COLS = ", ".join(_COLS)
_C = {name: index for index, name in enumerate(_COLS)}

_PLAN_PREFIX = "l:"        # 发送前写的计划全文（本地键）
_PLATFORM_PREFIX = "p:"    # 平台上真实存在的那条消息（平台键）
# 一轮 assistant 侧有"计划行 + 1~3 段实际形态"，按逻辑条数取数时要按倍数多取
_LOGICAL_OVERFETCH = 4
# "交付未知"的宽限期：发送流程结束后回显可能还没到，这段时间里"没有实发段"不代表没发出去。
# 早于这个时长仍无 assistant 实发行的计划成员才标 <cm_unsent/>（禁言/拒收/结果被清空）。
_UNSENT_GRACE_SEC = 180


def _kinds_of(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _parse_created_at(raw: Any) -> Optional[datetime]:
    """``created_at``（UTC naive 字符串）→ aware datetime；解析不了返回 None。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("T", " "))
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _time_pair(raw: Any, tz: Optional[ZoneInfo]) -> tuple[str, str]:
    """``(配置时区 naive 字符串, UTC ISO 8601)``——与 V1 ``_row_to_dict`` 口径一致。"""
    if tz is None or not raw:
        return str(raw or ""), str(raw or "")
    try:
        dt = datetime.fromisoformat(str(raw).replace("T", " "))
        iso = dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        local = dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        return local, iso
    except (ValueError, TypeError):
        return str(raw), str(raw)


def _record(row: Any, tz: Optional[ZoneInfo]) -> dict:
    """影子视图行 → 与 V1 ``query_history`` 同形的 dict（额外带 V2 字段）。"""
    template = str(row[_C["content"]] or "")
    relation = parse_relation_data(row[_C["relation_data"]]) or {}
    created_at, created_at_utc = _time_pair(row[_C["created_at"]], tz)
    return {
        "role": row[_C["role"]],
        "content": render_content_template(template, relation),
        "content_template": template,
        "user_id": row[_C["user_id"]],
        "message_id": row[_C["platform_message_id"]],
        "pair_id": None,
        "llm_status": row[_C["llm_status"]],
        "content_kind": _kinds_of(row[_C["content_kind"]]),
        "platform_id": row[_C["platform_id"]],
        "platform_name": row[_C["platform_name"]],
        "message_type": row[_C["message_type"]],
        "session_id": row[_C["session_id"]],
        "self_id": row[_C["self_id"]],
        "group_id": row[_C["group_id"]],
        "sender_nickname": row[_C["sender_nickname"]],
        "raw_timestamp": row[_C["raw_timestamp"]],
        "at_id": str(relation.get("at_id") or ""),
        "reply_id": str(relation.get("reply_id") or ""),
        "forward_id": str(relation.get("forward_id") or ""),
        "persona_id": row[_C["persona_id"]],
        "created_at": created_at,
        "created_at_utc": created_at_utc,
        "turn_id": row[_C["turn_id"]],
        "send_status": row[_C["send_status"]],
        "relation_data": relation,
        # takeover 规整阶段按它兜底过滤"撤回不进上下文"
        "recalled_at": row[_C["recalled_at"]],
        # V2 专有：影子层身份与段序（V1 没有这些列）
        "record_id": int(row[_C["seq"]] or 0),
        "msg_uid": row[_C["msg_uid"]],
        "platform_seq": row[_C["platform_seq"]],
        "instance_ts": row[_C["raw_timestamp"]],
        "conversation_id": row[_C["conversation_id"]],
        "recalled": bool(row[_C["recalled_at"]]),
        "segments": [row[_C["msg_uid"]]],
        "segment_count": 1,
        "source": "shadow",
    }


async def _select(
    session: AsyncSession,
    *,
    conditions: list[str],
    params: dict,
    expanding: tuple[str, ...] = (),
    order: str = "",
    limit: Optional[int] = None,
):
    """跑一次视图查询；conditions 里的裸列名都来自 ``view``。"""
    sql_text = text(
        _VIEW_CTE
        + f"SELECT {_VIEW_COLS} FROM view WHERE " + " AND ".join(conditions)
        + (f" {order}" if order else "")
        + (" LIMIT :lim" if limit is not None else "")
    )
    for name in expanding:
        sql_text = sql_text.bindparams(bindparam(name, expanding=True))
    payload = dict(params)
    if limit is not None:
        payload["lim"] = int(limit)
    return (await session.execute(sql_text, payload)).fetchall()


def _filter_conditions(
    *,
    statuses: Optional[set[str]],
    include_kinds: Optional[set[str]],
    all_match: bool,
    role_filter: Optional[str],
    persona_id: Optional[str],
    filter_by_persona: bool,
    exclude_turn_id: Optional[str],
    skip_recalled: bool,
    since: Optional[datetime],
    until: Optional[datetime],
    after_id: Optional[int],
) -> tuple[list[str], dict, tuple[str, ...]]:
    """公共 WHERE 片段（消息级 + 参与行级），与 V1 同名参数一一对应。"""
    conditions = ["deleted_at IS NULL"]
    params: dict[str, Any] = {}
    expanding: list[str] = []

    if skip_recalled:
        conditions.append("recalled_at IS NULL")
    if role_filter:
        conditions.append("role = :role")
        params["role"] = role_filter
    if exclude_turn_id:
        conditions.append("(turn_id IS NULL OR turn_id != :exclude_turn_id)")
        params["exclude_turn_id"] = exclude_turn_id
    if filter_by_persona:
        if persona_id:
            conditions.append("persona_id = :persona_id")
            params["persona_id"] = persona_id
        else:
            conditions.append("(persona_id IS NULL OR persona_id = '')")
    if statuses:
        conditions.append("llm_status IN :statuses")
        params["statuses"] = list(statuses)
        expanding.append("statuses")
    if include_kinds:
        if all_match:
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(content_kind)) "
                "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) "
                "WHERE value NOT IN :kinds)"
            )
        else:
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value IN :kinds)"
            )
        params["kinds"] = list(include_kinds)
        expanding.append("kinds")
    if since is not None:
        if after_id is None:
            conditions.append("created_at >= :since")
        else:
            conditions.append(
                "(created_at > :since OR (created_at = :since AND seq > :after_id))"
            )
            params["after_id"] = int(after_id)
        params["since"] = since
    if until is not None:
        conditions.append("created_at <= :until")
        params["until"] = until
    return conditions, params, tuple(expanding)


async def fetch_messages(
    session: AsyncSession,
    *,
    scope_cond: str,
    scope_params: dict,
    tz: Optional[ZoneInfo] = None,
    cid: Optional[str] = None,
    limit: int = 20,
    statuses: Optional[set[str]] = None,
    include_kinds: Optional[set[str]] = None,
    all_match: bool = False,
    role_filter: Optional[str] = None,
    persona_id: Optional[str] = None,
    filter_by_persona: bool = False,
    exclude_turn_id: Optional[str] = None,
    skip_recalled: bool = False,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    from_oldest: bool = False,
    after_id: Optional[int] = None,
    collapse_turns: bool = True,
) -> list[dict]:
    """按 V1 ``query_latest`` / ``query_messages_raw`` 的语义从影子层取消息。

    ``collapse_turns``：assistant 行按"一轮一条逻辑回复"合并（计划行让位、平台键行按
    ``platform_seq`` 拼接）。**必须默认开**——影子层一轮有"计划行 + N 条实际形态行"，
    不合并的话切读后同一句回复会重复出现 N+1 次（V1 那边一轮只有一行）。逻辑行比物理行
    少，所以取数时按倍数多取，再按逻辑条数截断。
    """
    conditions, params, expanding = _filter_conditions(
        statuses=statuses, include_kinds=include_kinds, all_match=all_match,
        role_filter=role_filter, persona_id=persona_id,
        filter_by_persona=filter_by_persona, exclude_turn_id=exclude_turn_id,
        skip_recalled=skip_recalled, since=since, until=until, after_id=after_id,
    )
    conditions.append(f"({scope_cond})")
    if cid:
        conditions.append("conversation_id = :cid")
        params["cid"] = cid
    params = {**scope_params, **params}
    wanted = max(1, min(1000, int(limit)))
    fetch_limit = min(1000, wanted * _LOGICAL_OVERFETCH) if collapse_turns else wanted
    direction = "ASC" if from_oldest else "DESC"
    rows = list(
        await _select(
            session, conditions=conditions, params=params, expanding=expanding,
            # 平手用 seq（会话内序号）稳定排序；V1 用的是旧表自增 id，语义一致
            order=f"ORDER BY created_at {direction}, seq {direction}",
            limit=fetch_limit,
        )
    )
    if not from_oldest:
        rows.reverse()
    records = _collapse_rows(rows, tz=tz) if collapse_turns else [_record(r, tz) for r in rows]
    if len(records) > wanted:
        records = records[:wanted] if from_oldest else records[-wanted:]
    plan_fallback = collapse_turns and role_filter != "user"
    if plan_fallback and after_id is not None:
        # 计划成员行没有可比较的会话序号（seq 由消息表分配），用 after_id 做游标的增量
        # 拉取里补进来会被重复消费，所以这里不补；但不能静默——留一条 DEBUG 说明。
        logger.debug(
            "[ChatMemory][shadow_read] after_id 增量取数跳过计划兜底："
            "计划行没有可比游标（可能有轮次只有计划、无实发段）"
        )
    if plan_fallback and after_id is None:
        # 只有计划、没有任何实际段的轮次（回显还没到 / 这台机器上就没订上 push）：
        # 用计划成员兜底，一轮一条。漏掉的话这些轮次在切读后**整句消失**，而 V1 那边
        # 是看得到的（计划行本来就写进旧表）。只补"本轮别的行已经在范围内"的轮次——
        # 这样用户过滤/会话过滤由那行自己带来，不需要在链接表上重算一遍范围条件。
        # ``role_filter="user"`` 时**不能**补：那是配对查询在取 user 侧，补进来就成了
        # 把 assistant 行当 user 行返回（一轮双份）。
        in_scope = {str(r[_C["turn_id"]] or "") for r in rows}
        with_reply = {
            str(r[_C["turn_id"]] or "") for r in rows if r[_C["role"]] == "assistant"
        }
        need_plan = [turn for turn in in_scope - with_reply if turn]
        if need_plan:
            # 按**这一轮自己的 umo** 分组取计划成员：跨会话（cross_session）时同一个查询
            # 会覆盖多个 umo，不能统一按 scope 里的当前 umo 过滤。
            need_set = set(need_plan)
            by_umo: dict[str, list[str]] = {}
            for row in rows:
                turn = str(row[_C["turn_id"]] or "")
                if turn in need_set:
                    by_umo.setdefault(str(row[_C["umo"]] or ""), []).append(turn)
            plans: dict[str, dict] = {}
            for row_umo, turns in by_umo.items():
                plans.update(await _fetch_plan_members(
                    session, cid=cid, turn_ids=turns, umo=row_umo,
                    since=since, until=until, persona_id=persona_id,
                    filter_by_persona=filter_by_persona,
                ))
            extra = [
                _collapse_turn([_plan_row(plan)], tz=tz)
                for plan in plans.values()
                if _plan_passes_filters(
                    plan, statuses=statuses, include_kinds=include_kinds,
                    all_match=all_match,
                )
            ]
            if extra:
                # 复合排序键：created_at 只到秒，同秒多条时会打乱主查询的 (created_at, seq)
                # 次序，甚至被下面的截断裁掉。seq 为 0 的计划行排在同秒最前（它没有平台序号）。
                records = sorted(
                    records + extra,
                    key=lambda rec: (
                        str(rec.get("created_at") or ""),
                        int(rec.get("seq") or 0),
                    ),
                )
                if len(records) > wanted:
                    records = records[:wanted] if from_oldest else records[-wanted:]
    return records


def _plan_passes_filters(
    plan: dict,
    *,
    statuses: Optional[set[str]] = None,
    include_kinds: Optional[set[str]] = None,
    all_match: bool = False,
) -> bool:
    """计划成员兜底也要过白名单：否则调用方要"只要 text"，补回来的图/事件会混进上下文。"""
    if statuses is not None and str(plan.get("llm_status") or "") not in statuses:
        return False
    if include_kinds is not None:
        kinds = set(_kinds_of(plan.get("content_kind")))
        if all_match:
            if not include_kinds <= kinds:
                return False
        elif not (kinds & include_kinds):
            return False
    return True


def _collapse_rows(rows: list[Any], *, tz: Optional[ZoneInfo]) -> list[dict]:
    """把"同一轮的 assistant 行"合并成一条（顺序按每轮第一行的位置保持不变）。

    一轮的 assistant 侧在影子层是"计划行 + 各段实际形态"，V1 那边只有一行——消息级读
    路径也要守同一个不变量，否则切读后同一句回复会重复出现。没有 ``turn_id`` 的行
    （孤儿消息、别人的发言）原样返回。
    """
    groups: dict[str, list[Any]] = {}
    plan: list[tuple[str, Any]] = []
    for row in rows:
        turn = str(row[_C["turn_id"]] or "")
        if row[_C["role"]] == "assistant" and turn:
            if turn not in groups:
                groups[turn] = []
                plan.append(("turn", turn))
            groups[turn].append(row)
        else:
            plan.append(("row", row))
    out: list[dict] = []
    for kind, key in plan:
        if kind == "row":
            out.append(_record(key, tz))
            continue
        merged = _collapse_turn(groups[key], tz=tz)
        # 游标用组内最大序号：否则按 after_id 翻页时会把同一轮的各段又翻回来
        merged["record_id"] = max(int(r[_C["seq"]] or 0) for r in groups[key])
        out.append(merged)
    return out


def _plan_row(plan: dict) -> Any:
    """把一条"计划成员"（``chat_memory_reply_messages``，一轮一条）伪装成视图行。

    用 ``l:plan`` 当 msg_uid：``_collapse_turn`` 据此走"没有实际段才用计划全文"的分支，
    其余字段按 ``_COLS`` 的下标填进去，形状与真实行完全一致。
    """
    values: list[Any] = [None] * len(_COLS)
    mapping = {
        "msg_uid": f"{_PLAN_PREFIX}plan",
        "turn_id": plan.get("turn_id") or "",
        "content": plan.get("content") or "",
        "content_kind": plan.get("content_kind") or "[]",
        "relation_data": plan.get("relation_data"),
        "llm_status": plan.get("llm_status") or "",
        "send_status": plan.get("send_status") or "",
        "persona_id": plan.get("persona_id") or "",
        "created_at": plan.get("created_at"),
        "seq": 0,
    }
    for key, value in mapping.items():
        index = _C.get(key)
        if index is not None:
            values[index] = value
    return values


async def _fetch_plan_members(
    session: AsyncSession,
    *,
    cid: Optional[str] = None,
    turn_ids: list[str],
    umo: str = "",
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    persona_id: Optional[str] = None,
    filter_by_persona: bool = False,
) -> dict[str, dict]:
    """取这些轮次的计划成员行（``chat_memory_reply_messages``），一轮一条。

    ``turn_id`` 已经由调用方按范围条件选出，这里不再套 ``scope_cond``——范围条件里的
    ``user_id`` 是视图列，链接表上没有，套上去会直接报 no such column。但**会话要圈住**：
    同一条 ``turn_id`` 在别的 UMO 下也可能存在（链接表主键含 umo），不圈会拿错那条。

    表里**没有** ``relation_data`` 列（结构线 5 删掉了：计划成员行从来没人写它），
    所以这里不 SELECT 它，返回的 dict 里固定为 None——渲染层据此走
    ``strip_relation_tokens`` 分支，不能让裸 token 进上下文。
    """
    if not turn_ids:
        return {}
    conditions = ["deleted_at IS NULL", "turn_id IN :turn_ids"]
    params: dict[str, Any] = {"turn_ids": list(dict.fromkeys(turn_ids))}
    if umo:
        conditions.append("umo = :umo")
        params["umo"] = umo
    if cid:
        conditions.append("conversation_id = :cid")
        params["cid"] = cid
    # 时间窗与 persona 必须和 user 侧同一套：否则窗口之后才生成的计划全文、或别的
    # persona 的回复会被兜底带进上下文（V1 的时间窗是同时作用于两侧的）。
    if since is not None:
        conditions.append("created_at >= :since")
        params["since"] = since
    if until is not None:
        # 与主查询 `_filter_conditions` 的 `<=` 对齐：同一条消息在两侧的窗口判定必须一致。
        conditions.append("created_at <= :until")
        params["until"] = until
    if filter_by_persona:
        conditions.append("COALESCE(persona_id, '') = :persona")
        params["persona"] = str(persona_id or "")
    result = await session.execute(
        text(
            "SELECT turn_id, content, content_kind, llm_status, "
            "       send_status, persona_id, created_at "
            "  FROM chat_memory_reply_messages WHERE " + " AND ".join(conditions)
        ).bindparams(bindparam("turn_ids", expanding=True)),
        params,
    )
    out: dict[str, dict] = {}
    for row in result.fetchall():
        out[str(row[0] or "")] = {
            "turn_id": row[0], "content": row[1], "content_kind": row[2],
            "relation_data": None, "llm_status": row[3], "send_status": row[4],
            "persona_id": row[5], "created_at": row[6],
        }
    return {k: v for k, v in out.items() if k}


async def fetch_rounds(
    session: AsyncSession,
    *,
    scope_cond: str,
    scope_params: dict,
    tz: Optional[ZoneInfo] = None,
    cid: Optional[str] = None,
    limit_rounds: int = 10,
    statuses: Optional[set[str]] = None,
    include_kinds: Optional[set[str]] = None,
    all_match: bool = False,
    persona_id: Optional[str] = None,
    filter_by_persona: bool = False,
    skip_recalled: bool = False,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    from_oldest: bool = False,
    after_id: Optional[int] = None,
) -> list[list[dict]]:
    """配对查询：每轮 ``[user_dict, assistant_dict]``（assistant 侧是合并后的完整回复）。"""
    wanted = max(1, min(1000, int(limit_rounds)))
    # 先按 wanted 取、再按"有没有回复"过滤，会把返回轮数压到远小于 wanted（群里 bot 只回
    # 一部分消息）。改为先超取再裁：配对密度足够时轮数稳定等于 wanted，与 V1 的 LIMIT 对齐。
    user_rows = await fetch_messages(
        session, scope_cond=scope_cond, scope_params=scope_params, tz=tz, cid=cid,
        limit=min(1000, wanted * 6), statuses=statuses,
        include_kinds=include_kinds, all_match=all_match, role_filter="user",
        persona_id=persona_id, filter_by_persona=filter_by_persona,
        skip_recalled=skip_recalled, since=since, until=until,
        from_oldest=from_oldest, after_id=after_id,
    )
    turn_ids = [str(row.get("turn_id") or "") for row in user_rows]
    if not any(turn_ids):
        return []
    replies = await _fetch_turn_replies(
        session, scope_cond=scope_cond, scope_params=scope_params, tz=tz, cid=cid,
        turn_ids=[turn for turn in turn_ids if turn],
        since=since, until=until, persona_id=persona_id,
        filter_by_persona=filter_by_persona, skip_recalled=skip_recalled,
    )
    paired = [
        [row, replies[str(row.get("turn_id") or "")]]
        for row in user_rows
        if replies.get(str(row.get("turn_id") or "")) is not None
    ]
    # ``user_rows`` 出来是**升序**（fetch_messages 内部先 DESC 取窗口再 reverse），
    # 所以取"最近 wanted 轮"必须从尾部切：``[:wanted]`` 会把最新窗口里最旧的几轮留下、
    # 把用户刚聊的那几轮丢掉（V1 是 ORDER BY ... DESC LIMIT，语义相反）。
    if len(paired) > wanted:
        paired = paired[:wanted] if from_oldest else paired[-wanted:]
    return paired


async def _fetch_turn_replies(
    session: AsyncSession,
    *,
    scope_cond: str,
    scope_params: dict,
    tz: Optional[ZoneInfo] = None,
    cid: Optional[str] = None,
    turn_ids: list[str],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    persona_id: Optional[str] = None,
    filter_by_persona: bool = False,
    skip_recalled: bool = False,
) -> dict[str, dict]:
    """把给定的若干轮的 assistant 侧合并成一条（一轮一条逻辑回复）。"""
    if not turn_ids:
        return {}
    conditions = [f"({scope_cond})", "deleted_at IS NULL", "role = 'assistant'",
                  "turn_id IN :turn_ids"]
    params: dict[str, Any] = {**scope_params, "turn_ids": list(dict.fromkeys(turn_ids))}
    if cid:
        conditions.append("conversation_id = :cid")
        params["cid"] = cid
    if since is not None:
        conditions.append("created_at >= :since")
        params["since"] = since
    if until is not None:
        conditions.append("created_at < :until")
        params["until"] = until
    if filter_by_persona:
        conditions.append("COALESCE(persona_id, '') = :persona")
        params["persona"] = str(persona_id or "")
    if skip_recalled:
        conditions.append("recalled_at IS NULL")
    rows = await _select(
        session, conditions=conditions, params=params, expanding=("turn_ids",),
        order="ORDER BY created_at ASC, seq ASC",
    )
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row[_C["turn_id"]] or ""), []).append(row)
    # 只有计划、没有任何实际段的轮次：用计划成员行兜底（一轮一条）
    missing = [t for t in params["turn_ids"] if t and t not in grouped]
    if missing:
        # 这些轮次一个 assistant 行都没有（镜像里只有计划成员），拿不到"这轮属于哪个
        # umo"；turn_id 是发送侧生成的 uuid4（全局唯一），因此这里不按 umo 过滤，
        # 以免在跨会话查询里把别的 UMO 的计划成员整体漏掉。
        plans = await _fetch_plan_members(
            session, cid=cid, turn_ids=missing, since=since, until=until,
            persona_id=persona_id, filter_by_persona=filter_by_persona,
        )
        for turn, plan in plans.items():
            grouped[turn] = [_plan_row(plan)]
    return {turn: _collapse_turn(rows, tz=tz) for turn, rows in grouped.items() if turn}


def _collapse_turn(rows: list[Any], *, tz: Optional[ZoneInfo]) -> dict:
    """一轮的 assistant 侧 → 一条记录（段按平台号拼接；没有段才用计划全文）。"""
    # 计划成员是**合成行**（``_plan_row`` 用固定 uid ``l:plan``）；``messages`` 里真实的
    # ``l:`` 行是"没有平台共享 id 的真实消息"，必须当段参与拼接，不能当计划吞掉。
    plan_uid = f"{_PLAN_PREFIX}plan"
    plan_rows = [r for r in rows if str(r[_C["msg_uid"]]) == plan_uid]
    segment_rows = [r for r in rows if str(r[_C["msg_uid"]]) != plan_uid]
    visible = segment_rows or plan_rows or rows
    visible.sort(key=lambda r: (int(r[_C["platform_seq"]] or 0), int(r[_C["seq"]] or 0)))
    templates = [str(r[_C["content"]] or "") for r in visible]
    contents = [
        strip_relation_tokens(
            render_content_template(template, parse_relation_data(r[_C["relation_data"]]))
        )
        for template, r in zip(templates, visible)
    ]
    kinds: list[str] = []
    for row in visible:
        kinds.extend(kind for kind in _kinds_of(row[_C["content_kind"]]) if kind not in kinds)
    anchor = plan_rows[0] if plan_rows else visible[0]

    def _first(key: str) -> str:
        for row in (anchor, *visible):
            value = str(row[_C[key]] or "")
            if value:
                return value
        return ""

    record = _record(anchor, tz)
    # 只有计划成员、且它的 send_status 已经是 send_attempted：说明发送流程跑完了，
    # 但平台上没有这一条（禁言/拒收/被别的插件清空结果）——交付状态未知，别让模型
    # 以为自己已经说出口。正常轮次有实发段，这里恒为 False。
    # 加宽限期：发送流程刚结束时回显可能还没到，这段时间"没有实发段"不代表没发出去。
    delivery_unknown = False
    if (plan_rows and not segment_rows
            and str(anchor[_C["send_status"]] or "") == "send_attempted"):
        created = _parse_created_at(anchor[_C["created_at"]])
        if created is not None:
            age = (datetime.now(timezone.utc) - created).total_seconds()
            delivery_unknown = age >= _UNSENT_GRACE_SEC
    record.update(
        {
            "role": "assistant",
            "content": "\n".join(contents),
            "content_template": "\n".join(templates),
            "content_kind": kinds,
            "llm_status": _first("llm_status"),
            "send_status": str(
                (segment_rows[0] if segment_rows else anchor)[_C["send_status"]] or ""
            ),
            "persona_id": _first("persona_id"),
            "user_id": _first("target_id"),
            "recalled": any(str(r[_C["recalled_at"]] or "") for r in visible),
            "delivery_unknown": delivery_unknown,
            "segments": [str(r[_C["msg_uid"]]) for r in visible],
            "segment_count": len(visible),
            "platform_seq": visible[-1][_C["platform_seq"]],
            "msg_uid": str(visible[0][_C["msg_uid"]]),
        }
    )
    return record


async def fetch_turn_targets(
    session: AsyncSession,
    *,
    umo: str,
    turn_ids: list[str],
    tz: Optional[ZoneInfo] = None,
) -> dict[tuple[str, str], dict]:
    """V2 版 ``query_turn_targets``：``{(turn_id, role): record}``。

    与 V1 同形（``{(turn_id, role): 记录}``），但 assistant 侧是**这一轮合并后的一条
    逻辑回复**：被引用的那句回复本来就由多段组成，只取最早一段会让引用只剩半句。
    user 侧仍取最早一条（一轮只有一条用户消息）。
    """
    clean_ids = sorted({str(value) for value in turn_ids if value})
    if not umo or not clean_ids:
        return {}
    rows = await _select(
        session, conditions=["deleted_at IS NULL", "umo = :umo", "turn_id IN :turn_ids"],
        params={"umo": umo, "turn_ids": clean_ids}, expanding=("turn_ids",),
        order="ORDER BY created_at ASC, seq ASC",
    )
    targets: dict[tuple[str, str], dict] = {}
    assistant_rows: dict[str, list[Any]] = {}
    for row in rows:
        turn = str(row[_C["turn_id"]] or "")
        if turn and row[_C["role"]] == "assistant":
            assistant_rows.setdefault(turn, []).append(row)
            continue
        key = (turn, str(row[_C["role"]] or ""))
        targets.setdefault(key, _record(row, tz))
    for turn, group in assistant_rows.items():
        targets[(turn, "assistant")] = _collapse_turn(group, tz=tz)
    # 只有计划、没有实际段的轮次：照样能用计划成员当引用目标（与 V1 一致）
    missing = [turn for turn in clean_ids if turn not in assistant_rows]
    if missing:
        replies = await _fetch_turn_replies(
            session, scope_cond="umo = :umo", scope_params={"umo": umo},
            tz=tz, turn_ids=missing,
        )
        for turn, record in replies.items():
            targets.setdefault((turn, "assistant"), record)
    return targets


__all__ = ["fetch_messages", "fetch_rounds", "fetch_turn_targets"]
