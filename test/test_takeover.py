"""chat_memory 单元测试（不依赖 astrbot / sqlalchemy 完整安装）。

策略：
- ast.parse + exec 字符串加载 main.py（避免 import 链）
- sys.modules 注入 mock 让 import 通过
- sqlite3 :memory: + 手写 SQL 验证 storage 层

覆盖：
- normalize 行为：时间戳/媒体/前缀/合并/丢头丢尾
- takeover 调用：4 scope 矩阵 / pair-vs-mixed 模式 / spawn_tasks 保活 / records 排序
- SQL 正确性：raw 白名单 ANY/ALL / cross_umo 混合 scope / _scope_filter 签名 / WAL pragma
- 组件分类：user 端 + assistant 端（CR2 #5） / message_str 回退
- 配对回归：assistant_map 用 pair_id 而非 message_id（P0-1）
- 上下文清洗：strip reasoning 前缀
- 生命周期：terminate 取消 task + dispose engine
"""

import ast
import asyncio
import enum
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

# ── 注入 AstrBot mock ──────────────────────────────

for m in ["astrbot.api", "astrbot.api.star", "astrbot.api.event",
          "astrbot.api.provider", "astrbot.api.message_components"]:
    sys.modules[m] = types.ModuleType(m)

sys.modules["astrbot.api"].logger = None
sys.modules["astrbot.api"].Star = object
sys.modules["astrbot.api"].Context = object
sys.modules["astrbot.api"].AstrBotConfig = dict
sys.modules["astrbot.api"].ProviderRequest = object

for name in ["Plain", "Image", "Video", "Record", "File", "Face",
             "At", "AtAll", "Reply", "Forward"]:
    setattr(sys.modules["astrbot.api.message_components"], name, type(name, (), {}))

sys.modules["astrbot.api.star"].Star = object
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
    get_data_dir=lambda plugin_name: PLUGIN_DIR / ".test_data" / plugin_name,
)


class _EventMessageType(enum.Enum):
    ALL = "all"


class _Filter:
    EventMessageType = _EventMessageType

    def event_message_type(self, *a, **k):
        def d(f):
            return f
        return d

    def on_llm_request(self, *a, **k):
        def d(f):
            return f
        return d

    def on_decorating_result(self, *a, **k):
        def d(f):
            return f
        return d

    def after_message_sent(self, *a, **k):
        def d(f):
            return f
        return d


sys.modules["astrbot.api.event"].filter = _Filter()
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.provider"].ProviderRequest = object

# storage 模块缺 sqlalchemy 时退化为空，由测试按需注入
pkg = types.ModuleType("chat_memory")
sys.modules["chat_memory"] = pkg
storage_mod = types.ModuleType("chat_memory.storage")
storage_mod.DBManager = object
sys.modules["chat_memory.storage"] = storage_mod
setattr(pkg, "storage", storage_mod)

# 领域常量模块（message_classifier/main 的共同依赖）。
models_mod = types.ModuleType("chat_memory.models")
models_mod.__file__ = str(PLUGIN_DIR / "models.py")
sys.modules["chat_memory.models"] = models_mod
exec(
    compile((PLUGIN_DIR / "models.py").read_text(), "models.py", "exec"),
    models_mod.__dict__,
)
setattr(pkg, "models", models_mod)

# main.py 的组件分类已拆到独立模块；在无 AstrBot 安装的本地测试中手动加载它。
classifier_mod = types.ModuleType("chat_memory.message_classifier")
classifier_mod.__file__ = str(PLUGIN_DIR / "message_classifier.py")
sys.modules["chat_memory.message_classifier"] = classifier_mod
exec(
    compile((PLUGIN_DIR / "message_classifier.py").read_text(), "message_classifier.py", "exec"),
    classifier_mod.__dict__,
)
setattr(pkg, "message_classifier", classifier_mod)

context_builder_mod = types.ModuleType("chat_memory.context_builder")
context_builder_mod.__file__ = str(PLUGIN_DIR / "context_builder.py")
sys.modules["chat_memory.context_builder"] = context_builder_mod
exec(
    compile((PLUGIN_DIR / "context_builder.py").read_text(), "context_builder.py", "exec"),
    context_builder_mod.__dict__,
)
setattr(pkg, "context_builder", context_builder_mod)


# ── 加载 main.py ────────────────────────────────────

_main_src = (PLUGIN_DIR / "main.py").read_text()
_storage_src = (PLUGIN_DIR / "storage.py").read_text()
_schema_src = (PLUGIN_DIR / "_conf_schema.json").read_text()

_mod_ns = {"__name__": "chat_memory.main"}
exec(compile(ast.parse(_main_src), "main.py", "exec"), _mod_ns)
_ChatMemoryPlugin = _mod_ns["ChatMemoryPlugin"]


class _PluginStub(_ChatMemoryPlugin):
    """绕过 __init__（依赖 db / context），手填测试用属性。"""

    def __init__(self):
        pass


def _make_plugin():
    p = _PluginStub()
    p.ct_enable = True
    p.ct_cross_session = False
    p.ct_full_group = False
    p.ct_limit_rounds = 5
    p.ct_max_context_chars = 0
    p.ct_llm_status_filter = ["llm_success"]
    p.ct_include_kinds = set()  # 默认不过滤
    p.ct_include_all_match = False
    p.ct_filter_by_persona = False  # v2.3.4 persona 过滤默认关
    p.ct_fallback_to_native_on_empty = False
    p.ct_clear_native_history = True
    p.log_with_bot_id = False  # _log_prefix 用
    p.debug_to_info = False    # _log 用
    return p


_UMO_GROUP = "aiocqhttp:GroupMessage:g1"
_UMO_DM = "aiocqhttp:FriendMessage:u1"


# ── 测试用例 ────────────────────────────────────────

def test_extract_time_str():
    p = _make_plugin()
    assert p._extract_time_str("2026-07-09 14:30:25") == "07/09 14:30:25"
    assert p._extract_time_str("2026-07-09T14:30:25") == "07/09 14:30:25"
    assert p._extract_time_str("") == ""
    assert p._extract_time_str(None) == ""
    print("[T1] 时间戳格式 [MM/DD HH:MM:SS] ✓")


def test_is_pure_media():
    p = _make_plugin()
    assert p._is_pure_media({"content_kind": ["image"]}) is True
    assert p._is_pure_media({"content_kind": ["voice"]}) is True
    assert p._is_pure_media({"content_kind": ["text", "image"]}) is False
    assert p._is_pure_media({"content_kind": ["text"]}) is False
    assert p._is_pure_media({"content_kind": []}) is False
    assert p._is_pure_media({"content_kind": None}) is False
    print("[T2] 纯媒体判定（图文混合保留） ✓")


def test_user_prefix():
    p = _make_plugin()
    records = [
        {"role": "user", "content": "你好", "user_id": "u1",
         "sender_nickname": "Alice", "created_at": "2026-07-09 14:30:25",
         "content_kind": ["text"], "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert out[0]["content"] == "[07/09 14:30:25] Alice: 你好"
    assert out[0]["_no_save"] is True
    print("[T3] user 前缀 ✓")


def test_solo_assistant_merge():
    """T4+T9: 连续 solo assistant 合并 — 多 status 前缀 + paired 不混入。"""
    p = _make_plugin()
    records = [
        {"role": "user", "content": "hi", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:00:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "hello", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:00:05", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "推送1", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:01:00", "content_kind": ["text"],
         "llm_status": "proactive"},
        {"role": "assistant", "content": "漏的", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:01:30", "content_kind": ["text"],
         "llm_status": "orphan"},
        {"role": "user", "content": "收到", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:02:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    # 4 条：user + paired asst + 合并的 solo asst + user
    assert len(out) == 4, f"应 4 条，实际 {len(out)}"
    # paired asst 不被合进 solo
    assert "hello" in out[1]["content"]
    assert "[主动]" not in out[1]["content"]
    # solo asst 合并了 proactive + orphan，前缀都带
    assert "[主动]" in out[2]["content"] and "[未配对]" in out[2]["content"]
    assert "推送1" in out[2]["content"] and "漏的" in out[2]["content"]
    print("[T4] solo assistant 合并（多 status 前缀 + paired 隔离）✓")


def test_head_drop():
    p = _make_plugin()
    records = [
        {"role": "assistant", "content": "开场推送", "sender_nickname": "bot",
         "created_at": "2026-07-09 08:00:00", "content_kind": ["text"],
         "llm_status": "proactive"},
        {"role": "user", "content": "早", "sender_nickname": "Alice",
         "created_at": "2026-07-09 08:05:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    print("[T5] 头部非 user 被丢 ✓")


def test_tail_solo_pop():
    p = _make_plugin()
    records = [
        {"role": "user", "content": "hi", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:00:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "hello", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:00:05", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "主动推送", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:01:00", "content_kind": ["text"],
         "llm_status": "proactive"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 2
    assert out[-1]["role"] == "assistant"
    assert "hello" in out[-1]["content"]
    assert "主动推送" not in out[-1]["content"]
    print("[T6] 末尾 solo assistant 被 pop ✓")


def test_tail_paired_assistant_kept():
    p = _make_plugin()
    records = [
        {"role": "user", "content": "hi", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:00:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "hello", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:00:05", "content_kind": ["text"],
         "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 2
    assert out[-1]["role"] == "assistant"
    print("[T7] 末尾 llm_success assistant 保留 ✓")


def test_image_text_mixed():
    p = _make_plugin()
    records = [
        {"role": "user", "content": "看这张图", "user_id": "u1",
         "sender_nickname": "Alice", "created_at": "2026-07-09 14:30:25",
         "content_kind": ["text", "image"], "llm_status": "llm_success"},
        {"role": "assistant", "content": "好的", "sender_nickname": "bot",
         "created_at": "2026-07-09 14:30:30", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "user", "content": "", "user_id": "u1",
         "sender_nickname": "Bob", "created_at": "2026-07-09 14:31:00",
         "content_kind": ["image"], "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    user_contents = [c["content"] for c in out if c["role"] == "user"]
    assert any("看这张图" in c for c in user_contents)
    assert not any("Bob" in c for c in user_contents)
    print("[T8] 图文混合保留文本，纯图丢弃 ✓")


def test_prefix_is_mandatory_and_solo_still_tagged():
    p = _make_plugin()
    records = [
        {"role": "user", "content": "hi", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:00:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant", "content": "推送", "sender_nickname": "bot",
         "created_at": "2026-07-09 10:00:30", "content_kind": ["text"],
         "llm_status": "proactive"},
        {"role": "user", "content": "收到", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:01:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert out[0]["content"] == "[07/09 10:00:00] Alice: hi"
    assert out[1]["content"] == "[07/09 10:00:30] [主动] 推送"
    assert out[2]["content"] == "[07/09 10:01:00] Alice: 收到"
    schema = json.loads(_schema_src)
    assert "prefix_enhance" not in schema["context_takeover"]["items"]
    assert "ct_prefix_enhance" not in _main_src
    print("[T10] 时间+发送者前缀强制启用，solo 保留时间与状态 tag ✓")


def test_full_group_speaker_identity_and_system_instruction():
    """T49: full-group 标记当前/其他发言者，system prompt 规则只追加一次。"""
    p = _make_plugin()
    records = [
        {
            "role": "user", "content": "当前用户发言", "user_id": "u1",
            "sender_nickname": "Alice", "created_at": "2026-07-09 10:00:00",
            "content_kind": ["text"], "llm_status": "llm_success",
        },
        {
            "role": "user", "content": "其他用户发言", "user_id": "u2",
            "sender_nickname": "Bob", "created_at": "2026-07-09 10:00:03",
            "content_kind": ["text"], "llm_status": "",
        },
        {
            "role": "assistant", "content": "历史回答", "user_id": "u1",
            "created_at": "2026-07-09 10:00:05", "content_kind": ["text"],
            "llm_status": "llm_success",
        },
    ]
    out = p._takeover_normalize(
        records,
        _UMO_GROUP,
        current_user_id="u1",
        full_group=True,
    )
    assert [item["role"] for item in out] == ["user", "assistant"]
    assert "[当前发言者] Alice: 当前用户发言" in out[0]["content"]
    assert "[其他发言者] Bob: 其他用户发言" in out[0]["content"]
    assert "合并群聊转录" not in out[0]["content"]

    # 公开 API 没有当前 user 时使用中性标记，不把任何人误标为“其他”。
    neutral = p._takeover_normalize(
        records,
        _UMO_GROUP,
        current_user_id="",
        full_group=True,
    )
    assert "[发言者] Alice: 当前用户发言" in neutral[0]["content"]
    assert "[当前发言者]" not in neutral[0]["content"]
    assert "[其他发言者]" not in neutral[0]["content"]

    req = types.SimpleNamespace(system_prompt="原有系统提示")
    p._append_full_group_instruction(req)
    p._append_full_group_instruction(req)
    assert req.system_prompt.startswith("原有系统提示\n\n")
    assert req.system_prompt.count("[ChatMemory 群聊历史解释规则]") == 1
    assert "当前用户的新请求位于历史 contexts 之后" in req.system_prompt

    async def _run_hook():
        async def _get_cid(umo):
            return "conversation_demo"

        async def _build(**kwargs):
            return [
                {"role": "user", "content": "历史问题", "_no_save": True},
                {"role": "assistant", "content": "历史回答", "_no_save": True},
            ]

        reset_calls = []

        async def _reset(umo, cid):
            reset_calls.append((umo, cid))

        class _Event:
            unified_msg_origin = _UMO_GROUP

            def get_sender_id(self):
                return "u1"

            def get_extra(self, key, default=None):
                return default

        p.ct_full_group = True
        p._get_curr_cid = _get_cid
        p.build_takeover_contexts = _build
        p._safe_reset_history = _reset
        p._log = lambda *args, **kwargs: None
        hook_req = types.SimpleNamespace(system_prompt="人格提示", contexts=[])
        await p.take_over_context(_Event(), hook_req)
        assert hook_req.contexts[-1]["role"] == "assistant"
        assert hook_req.system_prompt.count("[ChatMemory 群聊历史解释规则]") == 1
        assert reset_calls == [(_UMO_GROUP, "conversation_demo")]

    asyncio.run(_run_hook())
    print("[T49] full-group 发言者身份标记 + system prompt 幂等注入 ✓")


def test_no_llm_mapping():
    schema = json.loads(_schema_src)
    opts = schema["context_takeover"]["items"]["llm_status_filter"]["options"]
    assert opts == ["llm_success", "llm_pending", "no_llm", "proactive", "orphan"]

    # 模拟 main.py 加载逻辑
    def map_status(ct_status):
        ct_status_list = list(ct_status) if ct_status else ["llm_success"]
        return ["" if s == "no_llm" else s for s in ct_status_list]

    assert map_status(["llm_success"]) == ["llm_success"]
    assert map_status(["no_llm"]) == [""]
    assert map_status(["llm_success", "no_llm"]) == ["llm_success", ""]
    assert map_status([]) == ["llm_success"]
    print("[T11] no_llm → '' 映射 ✓")


def test_takeover_query_matrix():
    """T12: cross_session × full_group 矩阵 → 传给底层 API 的 cid / uid / cross_umo / full_group 正确。

    新语义（v2.3.4）：uid 始终保留（不再用 None 表达 full_group），
    full_group 通过独立参数透传到 storage；storage 层按 4 种组合构造 scope。
    """
    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None, warning=lambda *a, **kw: None, debug=lambda *a, **kw: None,
    )
    try:
        async def _run():
            p = _make_plugin()
            # 默认 is_pair_only=True（仅 llm_success），走 query_rounds_raw
            calls = []

            class _DB:
                async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match, cross_umo=False, full_group=False, **kw):
                    calls.append({
                        "umo": umo, "cid": cid, "uid": uid,
                        "cross_umo": cross_umo, "full_group": full_group,
                    })
                    return []
                async def query_messages_raw(self, *a, **kw):
                    calls.append({"method": "messages_raw"})
                    return []

            p.db = _DB()

            # 矩阵 1: standard (F, F) → cid=c1, uid=u1, cross=F, full=F
            p.ct_cross_session = False
            p.ct_full_group = False
            await p._takeover_query(_UMO_GROUP, "c1", "u1")
            assert calls[-1] == {"umo": _UMO_GROUP, "cid": "c1", "uid": "u1",
                                 "cross_umo": False, "full_group": False}

            # 矩阵 2: cross_session (T, F) → cid=None, uid=u1, cross=T, full=F
            p.ct_cross_session = True
            p.ct_full_group = False
            await p._takeover_query(_UMO_GROUP, "c1", "u1")
            assert calls[-1]["cid"] is None, "cross_session 应传 cid=None"
            assert calls[-1]["uid"] == "u1"
            assert calls[-1]["cross_umo"] is True
            assert calls[-1]["full_group"] is False

            # 矩阵 3: full_group + 群 (F, T) → cid=c1, uid=u1（保留）, cross=F, full=T
            p.ct_cross_session = False
            p.ct_full_group = True
            await p._takeover_query(_UMO_GROUP, "c1", "u1")
            assert calls[-1]["cid"] == "c1"
            assert calls[-1]["uid"] == "u1", "full_group 不再丢 uid（storage 自己处理 scope）"
            assert calls[-1]["cross_umo"] is False
            assert calls[-1]["full_group"] is True

            # 矩阵 4: cross + full + 群 → cid=None, uid=u1（保留）, cross=T, full=T
            p.ct_cross_session = True
            p.ct_full_group = True
            await p._takeover_query(_UMO_GROUP, "c1", "u1")
            assert calls[-1]["cid"] is None
            assert calls[-1]["uid"] == "u1", "T+T 仍保留 uid（其他 umo 限当前 user）"
            assert calls[-1]["cross_umo"] is True
            assert calls[-1]["full_group"] is True

            # 矩阵 5: full_group + 私聊 → 降级，cid=c1, uid=u1, cross=F, full=F
            p.ct_cross_session = False
            p.ct_full_group = True
            await p._takeover_query(_UMO_DM, "c1", "u1")
            assert calls[-1]["cid"] == "c1"
            assert calls[-1]["uid"] == "u1", "私聊应降级，不丢 user"
            assert calls[-1]["cross_umo"] is False
            assert calls[-1]["full_group"] is False, "私聊 full_group 应降级为 False"

            # 矩阵 6: cross + full + 空 uid 的公开 API 安全降级 → 当前 CID 整群
            p.ct_cross_session = True
            p.ct_full_group = True
            await p._takeover_query(
                _UMO_GROUP,
                "c1",
                "",
                force_current_session=True,
            )
            assert calls[-1]["cid"] == "c1"
            assert calls[-1]["uid"] == ""
            assert calls[-1]["cross_umo"] is False
            assert calls[-1]["full_group"] is True

        asyncio.run(_run())
    finally:
        _mod_ns["logger"] = _orig_logger
    print("[T12] _takeover_query 矩阵（cid + uid + cross_umo + full_group）✓")


def test_takeover_mode_selection():
    """T13: llm_status_filter 决定走配对模式还是混合模式；include_kinds/all_match 透传。"""
    async def _run():
        p = _make_plugin()
        calls = []

        class _DB:
            async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match, cross_umo=False, full_group=False, **kw):
                calls.append(("rounds_raw", kinds, all_match))
                return []
            async def query_messages_raw(self, umo, cid, uid, lim, statuses, kinds, all_match, cross_umo=False, full_group=False, **kw):
                calls.append((
                    "messages_raw", statuses, kinds, all_match,
                    kw.get("exclude_turn_id"),
                ))
                return []

        p.db = _DB()

        # 仅 llm_success → 配对模式；同时验证 include_kinds/all_match 透传
        p.ct_llm_status_filter = {"llm_success"}
        p.ct_include_kinds = {"text", "image"}
        p.ct_include_all_match = True
        await p._takeover_query(_UMO_GROUP, "c1", "u1")
        assert calls[-1][0] == "rounds_raw"
        assert calls[-1][1] == {"text", "image"}
        assert calls[-1][2] is True

        # 含 proactive → 混合模式
        p.ct_llm_status_filter = {"llm_success", "proactive"}
        p.ct_include_kinds = {"text"}
        p.ct_include_all_match = False
        await p._takeover_query(
            _UMO_GROUP, "c1", "u1", exclude_turn_id="turn-current",
        )
        assert calls[-1][0] == "messages_raw"
        assert calls[-1][1] == {"llm_success", "proactive"}
        assert calls[-1][2] == {"text"}
        assert calls[-1][3] is False
        assert calls[-1][4] == "turn-current"

        # 含 no_llm（空串）→ 混合模式
        p.ct_llm_status_filter = {"llm_success", ""}
        await p._takeover_query(_UMO_GROUP, "c1", "u1")
        assert calls[-1][0] == "messages_raw"

    asyncio.run(_run())
    print("[T13] 模式选择（pair vs mixed）+ include_kinds/all_match 透传 ✓")


def test_no_fire_and_forget_writes():
    """T14: 关键写入直接 await，不再依赖后台 task 保活与 terminate flush。"""
    assert "def _spawn_tasks" not in _main_src
    assert "_pending_tasks" not in _main_src
    assert "ok = await self._safe_insert(" in _main_src
    assert "update_user_llm_status=" in _main_src
    print("[T14] 关键写入直接 await，无 fire-and-forget 写入 ✓")


def test_initialize_lifecycle():
    """T46: initialize 启动期迁移；失败时 dispose 并继续抛出。"""
    assert 'StarTools.get_data_dir("chat_memory")' in _main_src
    assert "async def initialize(self) -> None:" in _main_src

    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
    )

    async def _run():
        calls = []

        class _Engine:
            async def dispose(self):
                calls.append("dispose")

        class _DB:
            engine = _Engine()

            async def init_db(self):
                calls.append("init")

        p = _make_plugin()
        p.db = _DB()
        p.auto_cleanup_days = 0
        p._cleanup_started = False
        p._cleanup_task = None
        await p.initialize()
        assert calls == ["init"]

        class _FailDB:
            engine = _Engine()

            async def init_db(self):
                calls.append("fail_init")
                raise RuntimeError("init failed")

        p.db = _FailDB()
        try:
            await p.initialize()
        except RuntimeError as exc:
            assert str(exc) == "init failed"
        else:
            raise AssertionError("initialize 失败必须向 AstrBot 继续抛出")
        assert calls[-2:] == ["fail_init", "dispose"]

    try:
        asyncio.run(_run())
    finally:
        _mod_ns["logger"] = _orig_logger
    print("[T46] initialize 启动期迁移 + 失败释放/抛出 ✓")


def test_current_turn_excluded_from_mixed_takeover():
    """T47: 混合模式排除当前 turn，避免 history 与 prompt 重复。"""
    assert "exclude_turn_id=exclude_turn_id or None" in _main_src
    assert "turn_id != :exclude_turn_id" in _storage_src

    async def _run():
        p = _make_plugin()
        p.ct_llm_status_filter = {"llm_success", "llm_pending"}
        received = {}

        class _DB:
            async def query_messages_raw(self, *args, **kwargs):
                received.update(kwargs)
                return []

        p.db = _DB()
        await p._takeover_query(
            _UMO_GROUP,
            "c1",
            "u1",
            exclude_turn_id="turn-current",
        )
        assert received["exclude_turn_id"] == "turn-current"

    asyncio.run(_run())
    print("[T47] takeover 混合模式排除当前 turn_id ✓")


def test_public_build_takeover_contexts_api():
    """T48: 公开只读 API 复用 takeover 配置，并守住空 user_id 范围边界。"""
    assert "async def build_takeover_contexts(" in _main_src
    assert "contexts = await self.build_takeover_contexts(" in _main_src

    async def _run():
        p = _make_plugin()
        calls = []

        async def _query(
            umo,
            cid,
            user_id,
            persona_id="",
            exclude_turn_id="",
            force_current_session=False,
        ):
            calls.append({
                "umo": umo,
                "cid": cid,
                "user_id": user_id,
                "persona_id": persona_id,
                "exclude_turn_id": exclude_turn_id,
                "force_current_session": force_current_session,
            })
            return [
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
            ]

        normalize_calls = []

        def _normalize(
            records,
            umo,
            max_records=None,
            max_chars=0,
            current_user_id="",
            full_group=False,
        ):
            normalize_calls.append({
                "records": records,
                "umo": umo,
                "max_records": max_records,
                "max_chars": max_chars,
                "current_user_id": current_user_id,
                "full_group": full_group,
            })
            return [
                {"role": "user", "content": "历史问题", "_no_save": True},
                {"role": "assistant", "content": "历史回答", "_no_save": True},
            ]

        async def _current_cid(umo):
            return "conversation_current"

        async def _must_not_reset(*args, **kwargs):
            raise AssertionError("只读 API 不得清理 native history")

        p._takeover_query = _query
        p._takeover_normalize = _normalize
        p._get_curr_cid = _current_cid
        p._safe_reset_history = _must_not_reset

        # 接管关闭用 None 与“启用但无数据”的 [] 区分。
        p.ct_enable = False
        assert await p.build_takeover_contexts(_UMO_GROUP, "u1") is None
        assert calls == []

        p.ct_enable = True
        p.ct_full_group = False
        assert await p.build_takeover_contexts(_UMO_GROUP, "") == []
        assert await p.build_takeover_contexts(_UMO_GROUP, "   ") == []
        assert await p.build_takeover_contexts("", "u1") == []
        assert calls == []

        # 私聊中的 full_group 必须降级，空 user_id 仍不能扩大范围。
        p.ct_full_group = True
        assert await p.build_takeover_contexts(_UMO_DM, "") == []
        assert calls == []

        # conversation_id=None 时读取当前 CID；混合模式复用消息数和字符预算配置。
        p.ct_full_group = False
        p.ct_llm_status_filter = {"llm_success", "proactive"}
        p.ct_limit_rounds = 7
        p.ct_max_context_chars = 321
        contexts = await p.build_takeover_contexts(
            _UMO_GROUP,
            "u1",
            persona_id="persona_demo",
            exclude_turn_id="turn_current",
        )
        assert contexts[0]["_no_save"] is True
        assert calls[-1] == {
            "umo": _UMO_GROUP,
            "cid": "conversation_current",
            "user_id": "u1",
            "persona_id": "persona_demo",
            "exclude_turn_id": "turn_current",
            "force_current_session": False,
        }
        assert normalize_calls[-1]["max_records"] == 7
        assert normalize_calls[-1]["max_chars"] == 321
        assert normalize_calls[-1]["current_user_id"] == "u1"
        assert normalize_calls[-1]["full_group"] is False

        # 纯配对模式由 query_rounds_raw 控制轮数，normalize 不再二次按消息数截断。
        p.ct_llm_status_filter = {"llm_success"}
        await p.build_takeover_contexts(_UMO_GROUP, "u1", "conversation_explicit")
        assert calls[-1]["cid"] == "conversation_explicit"
        assert normalize_calls[-1]["max_records"] is None

        # 群聊 full_group 明确开启时才允许空 user_id；cross_session 必须被强制关闭。
        p.ct_full_group = True
        p.ct_cross_session = True
        await p.build_takeover_contexts(_UMO_GROUP, "", "conversation_group")
        assert calls[-1]["user_id"] == ""
        assert calls[-1]["cid"] == "conversation_group"
        assert calls[-1]["force_current_session"] is True
        assert normalize_calls[-1]["current_user_id"] == ""
        assert normalize_calls[-1]["full_group"] is True

        async def _no_current_cid(umo):
            return ""

        p._get_curr_cid = _no_current_cid
        call_count = len(calls)
        assert await p.build_takeover_contexts(_UMO_GROUP, "u1") == []
        assert len(calls) == call_count

    asyncio.run(_run())
    print("[T48] build_takeover_contexts 返回语义、配置复用与空 user_id 边界 ✓")


def test_takeover_mixed_limit_and_empty_policy():
    """T40: 混合模式规整后不超过源记录 limit；空结果默认严格接管。"""
    p = _make_plugin()
    records = []
    for idx in range(1, 5):
        records.extend([
            {
                "role": "user", "content": f"u{idx}", "user_id": "u1",
                "sender_nickname": "Alice", "created_at": f"2026-07-09 10:0{idx}:00",
                "content_kind": ["text"], "llm_status": "llm_success",
            },
            {
                "role": "assistant", "content": f"a{idx}", "user_id": "u1",
                "sender_nickname": "bot", "created_at": f"2026-07-09 10:0{idx}:05",
                "content_kind": ["text"], "llm_status": "llm_success",
            },
        ])

    out = p._takeover_normalize(records, _UMO_GROUP, max_records=3)
    assert len(out) <= 3, f"规整后 contexts 不应超过 3，实际 {len(out)}"
    assert all("u1" not in item["content"] for item in out), "最老记录应被裁掉"

    async def _run_empty_policy():
        req = types.SimpleNamespace(contexts=[{"role": "user", "content": "native"}])
        reset_calls = []

        async def _reset(umo, cid):
            reset_calls.append((umo, cid))

        p._safe_reset_history = _reset
        p._log = lambda *a, **kw: None
        await p._handle_empty_takeover(
            types.SimpleNamespace(), req, _UMO_GROUP, "c1", "test-empty",
        )
        assert req.contexts == [], "严格接管应清空 native contexts"
        assert reset_calls == [(_UMO_GROUP, "c1")]

        p.ct_fallback_to_native_on_empty = True
        req.contexts = [{"role": "user", "content": "native"}]
        reset_calls.clear()
        await p._handle_empty_takeover(
            types.SimpleNamespace(), req, _UMO_GROUP, "c1", "test-fallback",
        )
        assert req.contexts[0]["content"] == "native"
        assert reset_calls == []

    asyncio.run(_run_empty_policy())
    print("[T40] takeover 混合 limit + 空结果严格/回退策略 ✓")


def test_takeover_character_budget():
    """T45: 字符预算按完整 user 起点裁掉旧上下文，不伪装精确 token。"""
    p = _make_plugin()
    records = []
    for idx in range(1, 4):
        records.extend([
            {
                "role": "user", "content": f"user-{idx}", "user_id": "u1",
                "content_kind": ["text"], "llm_status": "llm_success",
            },
            {
                "role": "assistant", "content": f"assistant-{idx}", "user_id": "u1",
                "content_kind": ["text"], "llm_status": "llm_success",
            },
        ])
    out = p._takeover_normalize(records, _UMO_GROUP, max_chars=20)
    assert out and out[0]["role"] == "user"
    assert "user-1" not in "\n".join(item["content"] for item in out)
    assert "user-3" in out[0]["content"]
    print("[T45] takeover 字符预算保留最新完整 user 起点 ✓")


def test_records_unconditional_sort():
    """T15: _takeover_query 末尾无条件排序（mock query_rounds_raw 返回倒序）。"""
    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None, warning=lambda *a, **kw: None, debug=lambda *a, **kw: None,
    )
    try:
        async def _run():
            p = _make_plugin()

            class _UnsortedDB:
                async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match, cross_umo=False, full_group=False, **kw):
                    # 故意返回倒序（later 在前）
                    return [
                        [{"role": "user", "content": "later", "created_at": "2026-07-09 11:00:00",
                          "content_kind": ["text"], "llm_status": "llm_success",
                          "sender_nickname": "B", "user_id": "u2", "message_id": "m2", "pair_id": None},
                         {"role": "assistant", "content": "a2", "created_at": "2026-07-09 11:00:05",
                          "content_kind": ["text"], "llm_status": "llm_success",
                          "sender_nickname": "bot", "user_id": "u2", "message_id": "a2", "pair_id": "m2"}],
                        [{"role": "user", "content": "earlier", "created_at": "2026-07-09 10:00:00",
                          "content_kind": ["text"], "llm_status": "llm_success",
                          "sender_nickname": "A", "user_id": "u1", "message_id": "m1", "pair_id": None},
                         {"role": "assistant", "content": "a1", "created_at": "2026-07-09 10:00:05",
                          "content_kind": ["text"], "llm_status": "llm_success",
                          "sender_nickname": "bot", "user_id": "u1", "message_id": "a1", "pair_id": "m1"}],
                    ]

            p.db = _UnsortedDB()

            recs = await p._takeover_query(_UMO_GROUP, "c1", "u1")
            assert recs[0]["content"] == "earlier", f"应按时间排序，实际首条: {recs[0]['content']}"

        asyncio.run(_run())
    finally:
        _mod_ns["logger"] = _orig_logger
    print("[T15] records 无条件 sort ✓")


def test_wal_pragma_registered():
    assert "PRAGMA journal_mode=WAL" in _storage_src
    assert "PRAGMA synchronous=NORMAL" in _storage_src
    assert "PRAGMA busy_timeout=5000" in _storage_src, "v2.3.6 应加 busy_timeout=5000"
    assert "@event.listens_for(self.engine.sync_engine, \"connect\")" in _storage_src
    print("[T16] WAL pragma 注册（含 busy_timeout） ✓")


def test_legacy_rename_releases_index_names():
    """T41: RENAME 后必须释放旧索引名称，否则新表 CREATE INDEX 会静默跳过。"""
    assert "_INDEX_DEFINITIONS" in _storage_src
    assert "DROP INDEX IF EXISTS" in _storage_src
    assert "for index_name in _INDEX_DEFINITIONS" in _storage_src

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chat_memory_records (id INTEGER, created_at DATETIME)")
    conn.execute("CREATE INDEX ix_cm_created ON chat_memory_records (created_at)")
    conn.execute("ALTER TABLE chat_memory_records RENAME TO chat_memory_records_backup_1")
    conn.execute("DROP INDEX IF EXISTS ix_cm_created")
    conn.execute("CREATE TABLE chat_memory_records (id INTEGER, created_at DATETIME)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cm_created ON chat_memory_records (created_at)")
    bound_table = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='ix_cm_created'"
    ).fetchone()[0]
    conn.close()
    assert bound_table == "chat_memory_records"
    print("[T41] 老库 RENAME 后索引名称释放并绑定新表 ✓")


def test_v236_hardening():
    """T37: 历史加固项静态验证 — limit 钳制 + 老库 rename + UTC + overfetch。"""
    schema = json.loads(_schema_src)
    assert schema["max_content_length"]["default"] == 0
    assert 'config.get("max_content_length", 0)' in _main_src
    assert 'self.ct_limit_rounds = max(1, int(ct_conf.get("limit_rounds", 30)))' in _main_src
    assert 'min(100, int(ct_conf.get("limit_rounds", 30)))' not in _main_src

    # limit 钳制：query_latest / query_rounds 入口都应有 [1, 1000] 钳制
    assert _storage_src.count("limit = max(1, min(1000, int(limit)))") == 1, \
        "query_latest 应钳 limit 到 [1, 1000]"
    assert _storage_src.count("limit_rounds = max(1, min(1000, int(limit_rounds)))") == 1, \
        "query_rounds 应钳 limit_rounds 到 [1, 1000]"

    # 老库 rename 备份（不再 DROP）
    assert "DROP TABLE chat_memory_records" not in _storage_src, \
        "v2.3.6 应改为 RENAME 备份，不再 DROP"
    assert "ALTER TABLE chat_memory_records RENAME TO" in _storage_src, \
        "v2.3.6 老库应改为 rename 备份"

    # count_by_conversation 审计方法
    assert "async def count_by_conversation" in _storage_src, \
        "v2.3.6 应新增 count_by_conversation 给 /reset 审计"

    # 存储统一 UTC naive（insert 的 :now 值）
    assert _storage_src.count("datetime.now(timezone.utc).replace(tzinfo=None)") >= 1, \
        "storage insert 应统一用 UTC naive"
    # created_at 参数已删除（存储走 UTC 默认，不再由 main 传入）
    assert "created_at: Optional[datetime]" not in _storage_src, \
        "insert 不应再有 created_at 参数"

    # 时区感知：存储 UTC + 输出转配置时区
    assert "def _row_to_dict(r, tz: Optional[ZoneInfo] = None)" in _storage_src, \
        "_row_to_dict 应接受 tz 参数"
    assert "dt.replace(tzinfo=timezone.utc)" in _storage_src, \
        "_row_to_dict 应把 UTC naive 标注为 aware"
    assert ".astimezone(tz)" in _storage_src, \
        "_row_to_dict 应转配置时区"
    assert "def __init__(self, data_dir: Path, tz: Optional[ZoneInfo] = None):" in _storage_src, \
        "DBManager 应接受 tz 参数"

    assert "ZoneInfo" in _main_src, "main.py 应 import ZoneInfo"
    assert 'context.get_config().get("timezone"' in _main_src, \
        "main.py 应从 AstrBot 全局配置读 timezone"
    assert "DBManager(data_dir, tz=self._tz)" in _main_src, \
        "main.py 应把 tz 传给 DBManager"
    assert "def _now(self)" not in _main_src, \
        "_now() 已删除（存储走 UTC，输出由 storage 转换）"
    assert "datetime.now(dt_timezone.utc).replace(tzinfo=None)" in _main_src, \
        "_cleanup_loop 应用 UTC naive 做清理窗口"
    # BUG-D: _safe_reset_history 改 await（消除竞态）
    assert "await self._safe_reset_history(umo, cid)" in _main_src, \
        "take_over_context 应 await _safe_reset_history 而非 fire-and-forget"

    # overfetch：混合模式 limit * 2
    assert "limit * 2" in _main_src, \
        "v2.3.6 混合模式应 overfetch 2x"

    # 关键 assistant 写入已改为直接 await，terminate 不再负责 flush 后台写入 task。
    assert "_TERMINATE_FLUSH_TIMEOUT" not in _main_src
    assert "_pending_tasks" not in _main_src

    # /reset 审计：删除前 count + warning
    assert "即将清除" in _main_src and "count_by_conversation" in _main_src, \
        "/reset 应删除前 SELECT count + warning"

    print("[T37] 历史加固项（limit 钳 + rename 备份 + UTC + overfetch + reset 审计）✓")


def test_persona_filter_pipeline():
    """T38: v2.3.4 persona 过滤 — schema 加列 + 透传 + 真实 sqlite 行为。

    覆盖：
    1. 静态：storage schema 有 persona_id 列 + ALTER 补列逻辑 + _SELECT_COLS/_row_to_dict 对齐
    2. 静态：main.py 入库填 persona_id + takeover 透传 filter_by_persona + warn 日志
    3. 透传：_takeover_query 把 persona_id + filter_by_persona 透传到 storage 层
    4. 行为：真实 sqlite 上 filter_by_persona=True 只返回当前 persona 记录；=False 全返回；
            persona_id="" 时 filter=True 严格过滤 IS NULL OR ''（不再跳过）
    """
    # ── 1. 静态：schema 加 persona_id 列 ──
    assert "persona_id      TEXT," in _storage_src, "schema 应有 persona_id 列"
    assert "ALTER TABLE chat_memory_records ADD COLUMN persona_id TEXT" in _storage_src, \
        "v2.3.3 老库（有 llm_status 缺 persona_id）应 ALTER 补列而非 RENAME"
    assert "ix_cm_persona" in _storage_src and "ON chat_memory_records (persona_id)" in _storage_src, \
        "persona_id 应有索引"
    # _SELECT_COLS 含 persona_id（在 forward_id 之后，created_at 之前）
    assert "forward_id, \"\n    \"persona_id, created_at" in _storage_src, \
        "_SELECT_COLS 应在 forward_id 后加 persona_id"
    # _row_to_dict 映射对齐（位置 18 = persona_id，19 = created_at）
    assert '"persona_id": r[18],' in _storage_src
    assert '"created_at": created_at_str,' in _storage_src, \
        "_row_to_dict 应把 created_at 经时区转换后输出"

    # insert 签名 + SQL 含 persona_id
    assert "persona_id: Optional[str] = None," in _storage_src, "insert 应加 persona_id 参数"
    assert "persona_id, created_at," in _storage_src, "INSERT SQL 应含 persona_id 列"
    assert ":per_id, :now," in _storage_src, "INSERT VALUES 应含 :per_id 占位"

    # ── 2. 静态：main.py 入库 + takeover 透传 ──
    assert "async def _get_curr_persona(self, umo: str" in _main_src, \
        "main.py 应有 _get_curr_persona helper"
    assert 'event.set_extra("chat_memory_persona_id", persona_id)' in _main_src, \
        "capture_user 应把 persona_id 缓存到 extras 供 capture_bot 复用"
    assert 'event.get_extra("chat_memory_persona_id")' in _main_src, \
        "capture_bot 应从 extras 取 persona_id（兜底重查）"
    assert "persona_id=persona_id or None," in _main_src, \
        "_safe_insert 入库时应填 persona_id"
    assert "self.ct_filter_by_persona = bool(ct_conf.get(\"filter_by_persona\", False))" in _main_src, \
        "main.py 应加载 ct_filter_by_persona 配置"
    # _get_effective_persona 通过 resolve_selected_persona 获取生效 persona
    assert "async def _get_effective_persona(self, umo: str, event: AstrMessageEvent," in _main_src, \
        "main.py 应有 _get_effective_persona helper"
    assert "resolve_selected_persona" in _main_src, \
        "_get_effective_persona 应调 persona_manager.resolve_selected_persona"
    # takeover + capture 都用 _get_effective_persona
    assert "persona_id = await self._get_effective_persona(umo, event, cid)" in _main_src, \
        "capture_user 和 takeover 应从 _get_effective_persona 取生效 persona"
    # _takeover_query 透传
    assert "persona_id=persona_id, filter_by_persona=filter_by_persona" in _main_src, \
        "_takeover_query 应透传 persona_id + filter_by_persona 到两个 raw 方法"
    # warn 日志：filter_by_persona=True 但 persona_id 为空
    assert "filter_by_persona=True 但当前生效 persona_id 为空" in _main_src, \
        "take_over_context 应在 persona_id 为空时打 warn 日志"
    # 严格过滤：空 persona_id 时 IS NULL OR ''
    assert "persona_id IS NULL OR persona_id = ''" in _storage_src, \
        "storage 层应在 persona_id 为空时严格过滤 IS NULL OR ''"
    # BUG-E: query_rounds_raw EXISTS 在 cid 非空时加 conversation_id（防跨 cid 误配对）
    assert "AND a.conversation_id = chat_memory_records.conversation_id" in _storage_src, \
        "query_rounds_raw EXISTS 应在非 cross_session 时加 cid 条件"
    # BUG-F: 公开 API 区分 None（不过滤）和 ""（严格过滤 IS NULL OR ''）
    assert _storage_src.count("if persona_id is not None:") >= 3, \
        "query_latest + query_rounds(user+asst) 三处都用 is not None 区分 None 和 ''"

    # ── 3. 透传：mock DB 验证 ──
    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None, warning=lambda *a, **kw: None, debug=lambda *a, **kw: None,
    )

    async def _run_passthrough():
        p = _make_plugin()
        calls = []

        class _DB:
            async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match,
                                       cross_umo=False, full_group=False,
                                       persona_id=None, filter_by_persona=False, **kw):
                calls.append({
                    "method": "rounds", "persona_id": persona_id,
                    "filter_by_persona": filter_by_persona,
                })
                return []

        p.db = _DB()

        # 关闭过滤：filter_by_persona=False，persona_id 应为空串
        p.ct_filter_by_persona = False
        await p._takeover_query(_UMO_GROUP, "c1", "u1", persona_id="A")
        assert calls[-1]["filter_by_persona"] is False
        # persona_id 仍透传（即使过滤关，值也传过去；storage 层因 filter=False 不用）

        # 开启过滤 + persona 非空：filter_by_persona=True，persona_id="A"
        p.ct_filter_by_persona = True
        await p._takeover_query(_UMO_GROUP, "c1", "u1", persona_id="A")
        assert calls[-1]["filter_by_persona"] is True
        assert calls[-1]["persona_id"] == "A"

        # 开启过滤 + persona 空：filter_by_persona=True，persona_id=""（storage 层严格过滤 IS NULL OR ''）
        p.ct_filter_by_persona = True
        await p._takeover_query(_UMO_GROUP, "c1", "u1", persona_id="")
        assert calls[-1]["filter_by_persona"] is True
        assert calls[-1]["persona_id"] == ""

    try:
        asyncio.run(_run_passthrough())
    finally:
        _mod_ns["logger"] = _orig_logger

    # ── 4. 行为：真实 sqlite 上 persona 过滤 ──
    schema = """CREATE TABLE chat_memory_records (
        id INTEGER PRIMARY KEY, umo TEXT, conversation_id TEXT, user_id TEXT,
        role TEXT, content TEXT, message_id TEXT, pair_id TEXT,
        llm_status TEXT, content_kind TEXT, persona_id TEXT, created_at DATETIME
    )"""
    conn = sqlite3.connect(":memory:")
    conn.execute(schema)
    # 4 条 user + 4 条 assistant 配对，分布在 persona_A / persona_B / NULL（老库兜底）
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, persona_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", "A问1", "m1", None, "llm_success", '["text"]', "A", "2026-07-09 10:00:00"),
            ("umo1", "c1", "u1", "assistant", "A答1", None, "m1", "llm_success", '["text"]', "A", "2026-07-09 10:00:05"),
            ("umo1", "c1", "u1", "user", "B问1", "m2", None, "llm_success", '["text"]', "B", "2026-07-09 11:00:00"),
            ("umo1", "c1", "u1", "assistant", "B答1", None, "m2", "llm_success", '["text"]', "B", "2026-07-09 11:00:05"),
            ("umo1", "c1", "u1", "user", "NULL问1", "m3", None, "llm_success", '["text"]', None, "2026-07-09 12:00:00"),
            ("umo1", "c1", "u1", "assistant", "NULL答1", None, "m3", "llm_success", '["text"]', None, "2026-07-09 12:00:05"),
        ],
    )
    conn.commit()

    # 模拟 query_messages_raw 的 persona 过滤 SQL（mixed 模式更直观）
    base_sql = (
        "SELECT id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? "
        "AND role IN ('user', 'assistant') "
    )
    # filter_by_persona=True + persona_id="A"：只返回 A 的 2 条
    sql_a = base_sql + "AND persona_id = ? ORDER BY id"
    rows = conn.execute(sql_a, ("umo1", "c1", "A")).fetchall()
    assert {r[0] for r in rows} == {1, 2}, f"persona=A 应只 2 条，实际：{rows}"

    # filter_by_persona=True + persona_id="B"：只返回 B 的 2 条
    rows = conn.execute(sql_a, ("umo1", "c1", "B")).fetchall()
    assert {r[0] for r in rows} == {3, 4}, f"persona=B 应只 2 条，实际：{rows}"

    # filter_by_persona=True + persona_id 为空：严格过滤 IS NULL OR ''（只返回 NULL persona 的 2 条）
    sql_empty = base_sql + "AND (persona_id IS NULL OR persona_id = '') ORDER BY id"
    rows = conn.execute(sql_empty, ("umo1", "c1")).fetchall()
    assert {r[0] for r in rows} == {5, 6}, f"persona 为空严格过滤应只 NULL 的 2 条，实际：{rows}"

    # filter_by_persona=False：全返回（无论 persona_id 值）
    rows = conn.execute(base_sql + "ORDER BY id", ("umo1", "c1")).fetchall()
    assert len(rows) == 6

    # 跨 cid + persona 过滤：模拟 cross_session=T 场景
    # 加 cid=c2 的 persona_A 数据，验证 persona 过滤跨 cid 生效
    conn.execute(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, persona_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("umo1", "c2", "u1", "user", "A问2", "m4", None, "llm_success", '["text"]', "A", "2026-07-09 13:00:00"),
    )
    conn.commit()
    # 跨 cid（cid 条件去掉）+ persona=A：应返回 c1 的 A 2条 + c2 的 A 1条 = 3 条
    sql_cross = (
        "SELECT id FROM chat_memory_records "
        "WHERE umo = ? AND role IN ('user', 'assistant') "
        "AND persona_id = ? ORDER BY id"
    )
    rows = conn.execute(sql_cross, ("umo1", "A")).fetchall()
    assert {r[0] for r in rows} == {1, 2, 7}, f"跨 cid + persona=A 应 3 条，实际：{rows}"
    conn.close()

    print("[T38] v2.3.4 persona 过滤（schema + 透传 + 严格过滤 + warn 日志 + 跨 cid）✓")


def test_public_api_new_params():
    """T39: 对外 API query_history / query_rounds 加 persona_id / since / until 参数。

    覆盖：
    1. 静态：storage / main 签名含三参数 + _normalize_dt 辅助函数
    2. 行为：sqlite3 :memory: 验证 query_latest 的 persona_id + since/until 过滤
    3. 行为：sqlite3 :memory: 验证 query_rounds 配对模式下 persona + 时间窗口一致性
    4. 行为：EXISTS 子查复用 persona/since/until，保证严格完整配对
    """
    # ── 1. 静态：签名 + _normalize_dt ──
    assert "def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:" in _storage_src, \
        "storage 应有 _normalize_dt 辅助函数"
    assert "dt.astimezone(timezone.utc).replace(tzinfo=None)" in _storage_src, \
        "_normalize_dt 应把 tz-aware 转 UTC naive"
    assert "if dt.tzinfo is not None:" in _storage_src

    # query_latest 签名含 persona_id/since/until（返回 list[dict]）
    assert (
        "persona_id: Optional[str] = None,\n        since: Optional[datetime] = None,\n"
        "        until: Optional[datetime] = None,\n    ) -> list[dict]:"
    ) in _storage_src, "query_latest 签名应含 persona_id/since/until"
    # query_rounds 签名含 persona_id/since/until（返回 list[list[dict]]）
    assert (
        "persona_id: Optional[str] = None,\n        since: Optional[datetime] = None,\n"
        "        until: Optional[datetime] = None,\n    ) -> list[list[dict]]:"
    ) in _storage_src, "query_rounds 签名应含 persona_id/since/until"
    # main.py 透传
    assert "persona_id=persona_id, since=since, until=until," in _main_src, \
        "main.py 应把三参数透传到 db 层"

    # ── 2. 行为：query_latest 的 persona + 时间范围 ──
    schema = """CREATE TABLE chat_memory_records (
        id INTEGER PRIMARY KEY, umo TEXT, conversation_id TEXT, user_id TEXT,
        role TEXT, content TEXT, message_id TEXT, pair_id TEXT,
        llm_status TEXT, content_kind TEXT, persona_id TEXT, created_at DATETIME
    )"""
    conn = sqlite3.connect(":memory:")
    conn.execute(schema)
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, persona_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            # persona A: 10:00 + 11:00 各一轮
            ("umo1", "c1", "u1", "user", "A问1", "m1", None, "llm_success", '["text"]', "A", "2026-07-09 10:00:00"),
            ("umo1", "c1", "u1", "assistant", "A答1", None, "m1", "llm_success", '["text"]', "A", "2026-07-09 10:00:05"),
            ("umo1", "c1", "u1", "user", "A问2", "m2", None, "llm_success", '["text"]', "A", "2026-07-09 11:00:00"),
            ("umo1", "c1", "u1", "assistant", "A答2", None, "m2", "llm_success", '["text"]', "A", "2026-07-09 11:00:05"),
            # persona B: 12:00 一轮
            ("umo1", "c1", "u1", "user", "B问1", "m3", None, "llm_success", '["text"]', "B", "2026-07-09 12:00:00"),
            ("umo1", "c1", "u1", "assistant", "B答1", None, "m3", "llm_success", '["text"]', "B", "2026-07-09 12:00:05"),
        ],
    )
    conn.commit()

    base_sql = "SELECT id FROM chat_memory_records WHERE umo = ? AND conversation_id = ?"

    # persona=A：4 条
    rows = conn.execute(base_sql + " AND persona_id = ? ORDER BY id", ("umo1", "c1", "A")).fetchall()
    assert {r[0] for r in rows} == {1, 2, 3, 4}, f"persona=A 应 4 条，实际：{rows}"

    # persona=A + since=11:00：2 条（m2 + A答2）
    rows = conn.execute(
        base_sql + " AND persona_id = ? AND created_at >= ? ORDER BY id",
        ("umo1", "c1", "A", "2026-07-09 11:00:00"),
    ).fetchall()
    assert {r[0] for r in rows} == {3, 4}, f"persona=A + since=11:00 应 2 条，实际：{rows}"

    # persona=A + until=10:30：2 条（m1 + A答1）
    rows = conn.execute(
        base_sql + " AND persona_id = ? AND created_at <= ? ORDER BY id",
        ("umo1", "c1", "A", "2026-07-09 10:30:00"),
    ).fetchall()
    assert {r[0] for r in rows} == {1, 2}, f"persona=A + until=10:30 应 2 条，实际：{rows}"

    # persona=A + since=10:30 + until=11:30：2 条（m2 + A答2）
    rows = conn.execute(
        base_sql + " AND persona_id = ? AND created_at >= ? AND created_at <= ? ORDER BY id",
        ("umo1", "c1", "A", "2026-07-09 10:30:00", "2026-07-09 11:30:00"),
    ).fetchall()
    assert {r[0] for r in rows} == {3, 4}, f"时间窗口应 2 条，实际：{rows}"

    # ── 3. 行为：query_rounds 配对模式下 persona + 时间窗口 ──
    # user 查询：persona=A + since=11:00 → 只 m2
    user_sql = (
        "SELECT message_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'user' "
        "AND persona_id = ? AND created_at >= ? "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.conversation_id = chat_memory_records.conversation_id "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id) "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    rows = conn.execute(user_sql, ("umo1", "c1", "A", "2026-07-09 11:00:00", 10)).fetchall()
    assert {r[0] for r in rows} == {"m2"}, f"persona=A + since=11:00 user 应只 m2，实际：{rows}"

    # assistant 查询：persona=A + since=11:00 + pair_id IN (m2) → 只 A答2
    asst_sql = (
        "SELECT pair_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'assistant' "
        "AND persona_id = ? AND created_at >= ? AND pair_id IN (?) "
        "ORDER BY created_at ASC, id ASC"
    )
    rows = conn.execute(asst_sql, ("umo1", "c1", "A", "2026-07-09 11:00:00", "m2")).fetchall()
    assert {r[0] for r in rows} == {"m2"}, f"assistant persona=A + since=11:00 应只 m2 配对，实际：{rows}"

    # ── 4. EXISTS 子查复用 persona，严格排除不一致的配对 ──
    # 加一条 user persona=A，配对 assistant persona=B（异常但验证语义）
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, persona_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", "A问3", "m4", None, "llm_success", '["text"]', "A", "2026-07-09 13:00:00"),
            ("umo1", "c1", "u1", "assistant", "B答3", None, "m4", "llm_success", '["text"]', "B", "2026-07-09 13:00:05"),
        ],
    )
    conn.commit()

    # user persona=A 查询：m4 被排除（assistant 是 persona=B，不是严格配对）
    user_cross = (
        "SELECT message_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'user' "
        "AND persona_id = ? "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.conversation_id = chat_memory_records.conversation_id "
        "           AND a.role = 'assistant' "
        "           AND a.persona_id = chat_memory_records.persona_id "
        "           AND a.pair_id = chat_memory_records.message_id) "
        "ORDER BY id"
    )
    rows = conn.execute(user_cross, ("umo1", "c1", "A")).fetchall()
    assert "m4" not in {r[0] for r in rows}, "user persona=A 不应匹配 persona=B assistant"

    # assistant persona=A 查询 pair_id=m4：不返回 B答3
    asst_cross = (
        "SELECT pair_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'assistant' "
        "AND persona_id = ? AND pair_id IN (?) "
        "ORDER BY id"
    )
    rows = conn.execute(asst_cross, ("umo1", "c1", "A", "m4")).fetchall()
    assert len(rows) == 0, f"assistant persona=A 不应返回 B答3，实际：{rows}"

    conn.close()
    print("[T39] 对外 API persona_id/since/until（静态 + 真实 sqlite + EXISTS 语义）✓")


def test_cron_not_marked_attempted():
    """#1 cron 平台走 _capture_user_internal 不应标 capture_attempted。

    构造 mock event，platform_name='cron' 时 attempted 应保持未设置（让
    capture_bot 走 proactive 而非 orphan）。
    """
    p = _make_plugin()

    class _Ev:
        unified_msg_origin = "aiocqhttp:GroupMessage:g1"
        _extras = {}
        def get_extra(self, k, default=None): return self._extras.get(k, default)
        def set_extra(self, k, v): self._extras[k] = v
        def get_sender_id(self): return "cron_user"  # 非空，避免被空 user_id 跳过
        def get_self_id(self): return "bot1"

    ev = _Ev()

    # _log 调用 logger.debug，需把 None 替换为 mock
    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
    )

    import asyncio
    async def _run():
        # mock _get_platform_name 返回 'cron'
        orig = p._get_platform_name
        p._get_platform_name = lambda e: "cron"
        try:
            await p._capture_user_internal(ev)
        finally:
            p._get_platform_name = orig
    try:
        asyncio.run(_run())
    finally:
        _mod_ns["logger"] = _orig_logger

    # 核心：cron 路径不应标 attempted
    assert ev.get_extra("chat_memory_capture_attempted") in (None, False), \
        f"cron 不该标 attempted（实际：{ev.get_extra('chat_memory_capture_attempted')}）"
    print("[T19] cron 平台不标 capture_attempted ✓")


def test_raw_sql_whitelist():
    """T25+T26: query_rounds_raw / query_messages_raw 的白名单 SQL 在真实 sqlite 上行为正确。

    pair 和 mixed 数据集需求不同（pair 需要每条 user 有配对 assistant，mixed 不需要），
    所以分两个独立 conn 验证。
    """
    schema = """CREATE TABLE chat_memory_records (
        id INTEGER PRIMARY KEY, umo TEXT, conversation_id TEXT, user_id TEXT,
        role TEXT, content TEXT, message_id TEXT, pair_id TEXT,
        llm_status TEXT, content_kind TEXT, created_at DATETIME
    )"""

    # ── pair 模式（query_rounds_raw）──────────────
    # 注意：生产中 assistant.message_id 恒为 None（main.py:842），只有 user 有 message_id
    conn = sqlite3.connect(":memory:")
    conn.execute(schema)
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", "正常问1", "m1", None, "llm_success", '["text"]', "2026-07-09 10:00:00"),
            ("umo1", "c1", "u1", "assistant", "答1", None, "m1", "llm_success", '["text"]', "2026-07-09 10:00:05"),
            ("umo1", "c1", "u1", "user", "[通知]", "m2", None, "llm_success", '["system_event"]', "2026-07-09 10:30:00"),
            ("umo1", "c1", "u1", "assistant", "答2", None, "m2", "llm_success", '["text"]', "2026-07-09 10:30:05"),
            ("umo1", "c1", "u1", "user", "图+文", "m3", None, "llm_success", '["text","image"]', "2026-07-09 11:00:00"),
            ("umo1", "c1", "u1", "assistant", "答3", None, "m3", "llm_success", '["text"]', "2026-07-09 11:00:05"),
        ],
    )
    conn.commit()

    sql_pair_any = (
        "SELECT message_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'user' "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.conversation_id = chat_memory_records.conversation_id "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id) "
        "AND EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value IN ('text')) "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    mids = {r[0] for r in conn.execute(sql_pair_any, ("umo1", "c1", 10)).fetchall()}
    assert mids == {"m1", "m3"}, f"pair ANY 应保留 m1/m3，实际：{mids}"

    sql_pair_all = sql_pair_any.replace(
        "AND EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value IN ('text')) ",
        "AND EXISTS (SELECT 1 FROM json_each(content_kind)) "
        "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value NOT IN ('text')) ",
    )
    mids = {r[0] for r in conn.execute(sql_pair_all, ("umo1", "c1", 10)).fetchall()}
    assert mids == {"m1"}, f"pair ALL 应只 m1，实际：{mids}"
    conn.close()

    # ── mixed 模式（query_messages_raw）──────────────
    # assistant.message_id=None（生产真实情况）；SELECT id 区分（mixed 模式含 user+assistant）
    conn = sqlite3.connect(":memory:")
    conn.execute(schema)
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, llm_status, content_kind, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", "问1", "m1", None, "llm_success", '["text"]', "2026-07-09 10:00:00"),
            ("umo1", "c1", "u1", "assistant", "答1", None, "m1", "llm_success", '["text"]', "2026-07-09 10:00:05"),
            ("umo1", "c1", "u1", "assistant", "推送1", None, None, "proactive", '["text"]', "2026-07-09 10:30:00"),
            ("umo1", "c1", "u1", "user", "[poke]", "m2", None, "llm_success", '["system_event"]', "2026-07-09 10:45:00"),
            ("umo1", "c1", "u1", "user", "图+文", "m3", None, "llm_success", '["text","image"]', "2026-07-09 11:00:00"),
        ],
    )
    conn.commit()

    # SELECT id 用于稳定区分记录（mixed 模式有 assistant 行，其 message_id 恒为 None）
    sql_mixed_any = (
        "SELECT id, role FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? "
        "AND role IN ('user', 'assistant') "
        "AND llm_status IN ('llm_success', 'proactive') "
        "AND EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value IN ('text')) "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    rows = conn.execute(sql_mixed_any, ("umo1", "c1", 10)).fetchall()
    # id 1-3 + 5（4 是 system_event 被滤掉）= 4 条
    assert {r[0] for r in rows} == {1, 2, 3, 5}, f"mixed ANY 应 4 条（id 1/2/3/5），实际：{rows}"

    sql_mixed_all = sql_mixed_any.replace(
        "AND EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value IN ('text')) ",
        "AND EXISTS (SELECT 1 FROM json_each(content_kind)) "
        "AND NOT EXISTS (SELECT 1 FROM json_each(content_kind) WHERE value NOT IN ('text')) ",
    )
    rows = conn.execute(sql_mixed_all, ("umo1", "c1", 10)).fetchall()
    # id 5 是 ['text','image'] 被 ALL 滤掉
    assert {r[0] for r in rows} == {1, 2, 3}, f"mixed ALL 应 3 条（id 1/2/3），实际：{rows}"
    conn.close()

    print("[T25] raw SQL 白名单 ANY/ALL（pair + mixed）✓")


def test_cross_umo_full_group_mixed_sql():
    """T34: cross_umo + full_group 混合 scope — 当前 umo 整群 + 其他 umo 仅当前 user。

    场景：
    - 群 G（当前 umo）：u1 配对 + u2 配对（u2 是同群另一人）
    - 私聊 F（其他 umo）：u1 配对 + u3 配对（u3 是另一个用户的私聊）
    - 跨群 G2（其他 umo）：u1 配对（u1 在另一群的发言）

    开 cross_umo + full_group 时，从 G 发起 takeover：
    - 应拿到：G/u1, G/u2, F/u1, G2/u1（4 个 user）
    - 不应拿到：F/u3（其他 umo 内非当前 user）
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE chat_memory_records (
        id INTEGER PRIMARY KEY, umo TEXT, platform_id TEXT,
        conversation_id TEXT, user_id TEXT,
        role TEXT, content TEXT, message_id TEXT, pair_id TEXT,
        llm_status TEXT, content_kind TEXT, created_at DATETIME
    )""")
    G = "aiocqhttp:GroupMessage:111"
    F = "aiocqhttp:FriendMessage:222"
    G2 = "aiocqhttp:GroupMessage:333"

    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, platform_id, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            # 当前群 G：u1 + u2 各一轮
            (G, "aiocqhttp", "c_g1", "u1", "user", "G u1 问", "mg1", None, "llm_success", '["text"]', "2026-07-14 10:00:00"),
            (G, "aiocqhttp", "c_g1", "u1", "assistant", "G u1 答", "ag1", "mg1", "llm_success", '["text"]', "2026-07-14 10:00:05"),
            (G, "aiocqhttp", "c_g1", "u2", "user", "G u2 问", "mg2", None, "llm_success", '["text"]', "2026-07-14 10:30:00"),
            (G, "aiocqhttp", "c_g1", "u2", "assistant", "G u2 答", "ag2", "mg2", "llm_success", '["text"]', "2026-07-14 10:30:05"),
            # 私聊 F：u1 + u3（u3 是别人的私聊）
            (F, "aiocqhttp", "c_f1", "u1", "user", "F u1 问", "mf1", None, "llm_success", '["text"]', "2026-07-14 11:00:00"),
            (F, "aiocqhttp", "c_f1", "u1", "assistant", "F u1 答", "af1", "mf1", "llm_success", '["text"]', "2026-07-14 11:00:05"),
            (F, "aiocqhttp", "c_f3", "u3", "user", "F u3 问", "mf3", None, "llm_success", '["text"]', "2026-07-14 12:00:00"),
            (F, "aiocqhttp", "c_f3", "u3", "assistant", "F u3 答", "af3", "mf3", "llm_success", '["text"]', "2026-07-14 12:00:05"),
            # 跨群 G2：u1 一轮
            (G2, "aiocqhttp", "c_g2", "u1", "user", "G2 u1 问", "mg2_1", None, "llm_success", '["text"]', "2026-07-14 13:00:00"),
            (G2, "aiocqhttp", "c_g2", "u1", "assistant", "G2 u1 答", "ag2_1", "mg2_1", "llm_success", '["text"]', "2026-07-14 13:00:05"),
        ],
    )
    conn.commit()

    # 模拟 _scope_filter(cross_umo=T, full_group=T) 的 OR 条件
    sql_mixed = (
        "SELECT message_id, user_id, umo FROM chat_memory_records "
        "WHERE role = 'user' "
        "AND ((umo = ?) "
        "     OR (platform_id = ? AND umo != ? AND user_id = ?)) "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id)"
    )
    # 从群 G 发起 takeover，当前 user = u1
    rows = conn.execute(sql_mixed, (G, "aiocqhttp", G, "u1")).fetchall()
    msg_ids = {r[0] for r in rows}
    user_ids = {r[1] for r in rows}
    umos = {r[2] for r in rows}

    # 应拿到 4 条 user：
    # - G/u1 (当前群当前 user)
    # - G/u2 (当前群其他人 — full_group 覆盖)
    # - F/u1 (其他 umo 当前 user)
    # - G2/u1 (其他 umo 当前 user)
    # 不应拿到 F/u3（其他 umo 非当前 user）
    assert msg_ids == {"mg1", "mg2", "mf1", "mg2_1"}, (
        f"混合 scope 应取 G 全员 + 其他 umo 仅 u1，实际：{msg_ids}"
    )
    assert "u3" not in user_ids, f"u3 不应出现（其他 umo 非当前 user），实际：{user_ids}"
    assert umos == {G, F, G2}, f"应跨 3 个 umo，实际：{umos}"

    # 对照：仅 full_group（cross_umo=F），只取当前 umo 整群
    sql_fg_only = (
        "SELECT message_id FROM chat_memory_records "
        "WHERE role = 'user' AND umo = ? "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id)"
    )
    rows = conn.execute(sql_fg_only, (G,)).fetchall()
    assert {r[0] for r in rows} == {"mg1", "mg2"}, "仅 full_group 应只在当前群全员"

    # 对照：仅 cross_umo（full_group=F），只取当前 user 跨 umo
    sql_co_only = (
        "SELECT message_id FROM chat_memory_records "
        "WHERE role = 'user' AND platform_id = ? AND user_id = ? "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id)"
    )
    rows = conn.execute(sql_co_only, ("aiocqhttp", "u1")).fetchall()
    assert {r[0] for r in rows} == {"mg1", "mf1", "mg2_1"}, "仅 cross_umo 应只取 u1 跨 umo"

    conn.close()
    print("[T34] cross_umo + full_group 混合 scope（当前 umo 整群 + 其他 umo 当前 user）✓")


def test_scope_filter_signature():
    """T33: _scope_filter 函数签名 + 4 种 scope 组合的字符串特征。

    用 storage.py 源码文本断言关键字符串，避免触发 sqlalchemy 依赖。
    """
    # _scope_filter 函数定义存在（4 个参数）
    assert "def _scope_filter(\n    umo: str,\n    user_id: Optional[str],\n    cross_umo: bool,\n    full_group: bool," in _storage_src, \
        "_scope_filter 应定义在 storage.py 顶层，4 参数"
    # 默认分支（F/F）：umo + user_id
    assert '"umo = :scope_umo AND user_id = :scope_uid"' in _storage_src, \
        "默认 scope 应为 umo + user_id"
    # cross_umo 分支（T/F）：platform_id + user_id
    assert '"platform_id = :scope_pid AND user_id = :scope_uid"' in _storage_src, \
        "cross_umo 分支应为 platform_id + user_id"
    # full_group 分支（F/T）：仅 umo
    assert '"umo = :scope_umo"' in _storage_src, "full_group 分支应仅 umo"
    # 混合分支（T/T）：OR 条件
    assert "OR (platform_id = :scope_pid AND umo != :scope_umo" in _storage_src, \
        "T+T 混合分支应有 OR 条件（当前 umo 整群 + 其他 umo 当前 user）"
    # 两个 raw 方法签名都加了 cross_umo + full_group 参数
    assert _storage_src.count("cross_umo: bool = False,\n        full_group: bool = False") >= 2, \
        "query_rounds_raw 和 query_messages_raw 都应有 cross_umo + full_group 参数"
    # main.py takeover 调用时透传 cross_umo + full_group
    assert "cross_umo=cross_umo, full_group=effective_full_group" in _main_src, \
        "_takeover_query 应把 cross_umo + full_group 透传到 storage 层"

    # 空 user_id 防御：跨 UMO 不得退化为 platform_id 全量查询。
    scope_node = next(
        node for node in ast.parse(_storage_src).body
        if isinstance(node, ast.FunctionDef) and node.name == "_scope_filter"
    )
    scope_ns = {"Optional": __import__("typing").Optional}
    exec(compile(ast.Module(body=[scope_node], type_ignores=[]), "scope_filter", "exec"), scope_ns)
    scope_filter = scope_ns["_scope_filter"]
    assert scope_filter(_UMO_GROUP, "", True, True) == (
        "umo = :scope_umo",
        {"scope_umo": _UMO_GROUP},
    )
    assert scope_filter(_UMO_GROUP, "", True, False) == ("1 = 0", {})
    assert scope_filter(_UMO_GROUP, "", False, False) == ("1 = 0", {})
    assert scope_filter(_UMO_GROUP, "   ", True, True) == (
        "umo = :scope_umo",
        {"scope_umo": _UMO_GROUP},
    )
    assert "platform_id" not in scope_filter(_UMO_GROUP, "", True, True)[0]
    print("[T33] _scope_filter 4 种组合 + 空 user_id 禁止跨平台扩张 ✓")


def test_takeover_modes_and_whitelist():
    """T23+T24+T27: pair/mixed 模式 × ANY/ALL 白名单参数透传 + records 扁平化。"""
    import asyncio

    async def _run():
        # pair 模式 + ANY：query_rounds_raw 返回 [[u,a],[u,a]] → 扁平 4 条
        p = _make_plugin()
        p.ct_llm_status_filter = {"llm_success"}
        p.ct_include_kinds = {"text"}
        p.ct_include_all_match = False

        class _PairDB:
            async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match, **kw):
                assert kinds == {"text"} and all_match is False
                return [
                    [{"role": "user", "content_kind": ["text"], "message_id": "u1"},
                     {"role": "assistant", "content_kind": ["text"]}],
                    [{"role": "user", "content_kind": ["text"], "message_id": "u3"},
                     {"role": "assistant", "content_kind": ["text"]}],
                ]
        p.db = _PairDB()
        out = await p._takeover_query("umo1", "c1", "u1")
        assert len(out) == 4 and out[0]["message_id"] == "u1" and out[2]["message_id"] == "u3"

        # mixed 模式 + ANY：含 proactive → query_messages_raw
        p = _make_plugin()
        p.ct_llm_status_filter = {"llm_success", "proactive"}
        p.ct_include_kinds = {"text"}
        p.ct_include_all_match = False

        class _MixedDB:
            async def query_messages_raw(self, umo, cid, uid, lim, statuses, kinds, all_match, **kw):
                assert kinds == {"text"} and all_match is False
                return [
                    {"role": "user", "content_kind": ["text"], "message_id": "m1"},
                    {"role": "assistant", "content_kind": ["text"], "message_id": "a1"},
                ]
        p.db = _MixedDB()
        out = await p._takeover_query("umo1", "c1", "u1")
        assert len(out) == 2

        # pair 模式 + ALL：all_match=True 透传
        p = _make_plugin()
        p.ct_llm_status_filter = {"llm_success"}
        p.ct_include_kinds = {"text", "image"}
        p.ct_include_all_match = True

        class _AllDB:
            async def query_rounds_raw(self, umo, cid, uid, lim, kinds, all_match, **kw):
                assert all_match is True
                return [[{"role": "user", "content_kind": ["text"], "message_id": "u1"},
                         {"role": "assistant", "content_kind": ["text"]}]]
        p.db = _AllDB()
        out = await p._takeover_query("umo1", "c1", "u1")
        assert len(out) == 2

    asyncio.run(_run())
    print("[T23] pair/mixed 模式 × ANY/ALL 白名单透传 + 扁平化 ✓")


def test_strip_reasoning_prefix():
    """T28: 剥离 AstrBot 错误序列化的 reasoning parts 前缀。"""
    p = _make_plugin()
    strip = p._strip_reasoning_prefix

    # 1. 无前缀：原样返回
    assert strip("普通文本") == "普通文本"
    assert strip("") == ""

    # 2. 真实 think 前缀（来自 DB 的样本）
    raw = (
        "[{'type': 'think', 'content': '推理内容。D指挥官在亲脸', "
        "'encrypted': None}]（耳尖猛地一颤）……拉菲说了狡猾……"
    )
    out = strip(raw)
    assert out == "（耳尖猛地一颤）……拉菲说了狡猾……", f"实际：{out!r}"

    # 3. 纯 think 列表（无后续）→ 空串
    pure_think = "[{'type': 'think', 'content': '只思考不答', 'encrypted': None}]"
    assert strip(pure_think) == ""

    # 4. content 里含 ]、引号、转义（验证字符级平衡）
    tricky = (
        "[{'type': 'think', 'content': '里面有\\'转义引号\\'和]方括号', "
        "'encrypted': None}]实际回复"
    )
    out = strip(tricky)
    assert out == "实际回复", f"含转义的实际：{out!r}"

    # 5. 用户消息含中文 [ 但不是 think 前缀：原样返回
    user_msg = "[系统]你好的回事"
    assert strip(user_msg) == user_msg

    print("[T28] _strip_reasoning_prefix（剥离 think 前缀）✓")


def test_takeover_normalize_strips_think():
    """T29: _takeover_normalize 注入时剥离老库已存的 think 前缀。"""
    p = _make_plugin()
    records = [
        {"role": "user", "content": "你好", "sender_nickname": "Alice",
         "created_at": "2026-07-09 10:00:00", "content_kind": ["text"],
         "llm_status": "llm_success"},
        {"role": "assistant",
         "content": "[{'type': 'think', 'content': '内部推理', 'encrypted': None}]实际回复",
         "sender_nickname": "bot",
         "created_at": "2026-07-09 10:00:05", "content_kind": ["text"],
         "llm_status": "llm_success"},
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 2
    assert out[0]["content"] == "[07/09 10:00:00] Alice: 你好"
    assert out[1]["content"] == "实际回复", f"实际：{out[1]['content']!r}"
    print("[T29] _takeover_normalize 剥离 think 前缀 ✓")


def test_classify_content_message_str_fallback():
    """T30: get_messages() 无 Plain 时回退到 event.message_str 补 text。

    复现老库 bug：AstrBot 把 user 文本放 message_str，组件链为空，
    导致 100% user 错标 content_kind=[]，takeover 全滤掉。
    """
    p = _make_plugin()

    class _Event:
        def get_messages(self): return []
        message_str = "你好啊"

    kind, _, _, _ = p._classify_content(_Event())
    assert kind == ["text"], f"应回退补 text，实际：{kind}"

    # chain 有 Plain 时不走回退
    Plain_cls = _mod_ns["Plain"]
    chain_with_plain = [Plain_cls()]
    chain_with_plain[0].text = "hello"

    class _EventWithPlain:
        def get_messages(self): return chain_with_plain
        message_str = ""

    kind, _, _, _ = p._classify_content(_EventWithPlain())
    assert kind == ["text"]

    # chain 空且 message_str 也空：保持 []
    class _EventEmpty:
        def get_messages(self): return []
        message_str = ""

    kind, _, _, _ = p._classify_content(_EventEmpty())
    assert kind == [], f"全空应保持 []，实际：{kind}"

    print("[T30] _classify_content message_str 回退 ✓")


def test_classify_content_components_extraction():
    """T31: 组件链提取 — 纯图片 / At+文本 / 纯 At / Reply / Forward / 多 kind 混合。

    复现 v2.3.5 之前 bug：``event.message_chain`` 不存在，``getattr`` 永远拿 None，
    所有非文本组件（Image/At/Reply/Forward 等）全部漏抽，纯图片被错跳过。
    改用 ``event.get_messages()`` 后修复。
    """
    p = _make_plugin()
    components = sys.modules["astrbot.api.message_components"]
    Plain = components.Plain
    Image = components.Image
    At = components.At
    Reply = components.Reply
    Forward = components.Forward
    Record = components.Record

    def _mk(components, message_str="", message_type_value=None):
        class _E:
            def get_messages(self): return list(components)
        _E.message_str = message_str
        if message_type_value is not None:
            class _MT:
                value = message_type_value
            _E.get_message_type = lambda self: _MT()
        return _E()

    # 1. 纯图片：v2.3.4 时被错跳过不入库；现在 kind=['image']
    img = Image()
    kind, at, rep, fwd = p._classify_content(_mk([img]))
    assert kind == ["image"], f"纯图片 kind 应为 ['image']，实际：{kind}"
    assert at is None and rep is None and fwd is None

    # 2. At + 文本：kind=['text']，at_id 提取
    at = At(); at.qq = "123456"
    pl = Plain(); pl.text = "你好"
    kind, at_id, _, _ = p._classify_content(_mk([at, pl]))
    assert kind == ["text"], f"At+文本 kind 应为 ['text']，实际：{kind}"
    assert at_id == "123456", f"at_id 应为 '123456'，实际：{at_id}"

    # 3. 纯 At（无文本）：kind=[]，但 at_id 仍提取（不应被跳过）
    at2 = At(); at2.qq = "999"
    kind, at_id, _, _ = p._classify_content(_mk([at2]))
    assert kind == [], f"纯 At kind 应为 []，实际：{kind}"
    assert at_id == "999", f"at_id 应为 '999'，实际：{at_id}"

    # 4. Reply：reply_id 提取，不入 kind
    rp = Reply(); rp.id = "msg_abc"
    pl2 = Plain(); pl2.text = "回复内容"
    kind, _, reply_id, _ = p._classify_content(_mk([rp, pl2]))
    assert kind == ["text"]
    assert reply_id == "msg_abc"

    # 5. Forward：kind=['forward']，forward_id 提取
    fw = Forward(); fw.id = "fwd_001"
    kind, _, _, forward_id = p._classify_content(_mk([fw]))
    assert kind == ["forward"], f"Forward kind 应为 ['forward']，实际：{kind}"
    assert forward_id == "fwd_001"

    # 6. 多 kind 混合：图片 + 语音 + 文本（去重保持顺序）
    img2 = Image()
    vc = Record()
    pl3 = Plain(); pl3.text = "看图听话"
    kind, _, _, _ = p._classify_content(_mk([img2, vc, pl3]))
    assert kind == ["image", "voice", "text"], f"混合 kind 应保持顺序去重，实际：{kind}"

    # 7. 图片 + 文本：不应触发 message_str 回退（chain 已有 kind）
    img3 = Image()
    pl4 = Plain(); pl4.text = "  "  # 空白 Plain 不入 kind
    kind, _, _, _ = p._classify_content(_mk([img3, pl4], message_str="fallback text"))
    assert kind == ["image"], f"已有 image 不应再补 text，实际：{kind}"

    print("[T31] _classify_content 组件提取（含 v2.3.5 message_chain 修复回归）✓")


def test_assistant_chain_classification():
    """T35: _classify_assistant_chain — BOT 回复组件链分类（CR2 #5 修复）。

    复现 v2.3.5 之前限制：assistant 端只提取 Plain，纯图/视频/语音不入库（content_kind 写死 ['text']）。
    修复后 content_kind 反映真实组件，纯媒体回复也入库（content 用占位符）。
    """
    p = _make_plugin()
    components = sys.modules["astrbot.api.message_components"]
    Plain = components.Plain
    Image = components.Image
    Video = components.Video
    Record = components.Record
    File = components.File
    Face = components.Face
    Forward = components.Forward
    At = components.At

    # 1. 纯文本：kind=['text']，text 完整
    pl = Plain(); pl.text = "你好"
    kind, text = p._classify_assistant_chain([pl])
    assert kind == ["text"] and text == "你好"

    # 2. 纯图片：v2.3.5 之前 if not bot_text: return 跳过；现在 kind=['image']
    kind, text = p._classify_assistant_chain([Image()])
    assert kind == ["image"], f"纯图 assistant kind 应为 ['image']，实际：{kind}"
    assert text == ""

    # 3. 文本 + 图片：kind=['text','image']（混合保留文本）
    pl2 = Plain(); pl2.text = "看图"
    kind, text = p._classify_assistant_chain([pl2, Image()])
    assert kind == ["text", "image"]
    assert text == "看图"

    # 4. 纯语音：kind=['voice']
    kind, _ = p._classify_assistant_chain([Record()])
    assert kind == ["voice"]

    # 5. 多种媒体：保持顺序去重
    kind, _ = p._classify_assistant_chain([Image(), Video(), Record(), File(), Face(), Forward()])
    assert kind == ["image", "video", "voice", "file", "face", "forward"]

    # 6. 空 chain：返回 ([], "")
    kind, text = p._classify_assistant_chain([])
    assert kind == [] and text == ""

    # 7. 空白 Plain：不入 kind（与 _classify_content 一致）
    pl3 = Plain(); pl3.text = "   "
    kind, text = p._classify_assistant_chain([pl3])
    assert kind == [] and text == ""

    # 8. assistant 端 At 被忽略（不入 kind）
    at = At(); at.qq = "123"
    pl4 = Plain(); pl4.text = "hi"
    kind, _ = p._classify_assistant_chain([at, pl4])
    assert kind == ["text"], f"At 在 assistant 端应忽略，实际 kind：{kind}"

    # 9. 占位符：_content_placeholder 用首个 kind
    assert p._content_placeholder(["image"]) == "[image]"
    assert p._content_placeholder(["voice", "text"]) == "[voice]"
    assert p._content_placeholder([]) == ""

    print("[T35] _classify_assistant_chain 组件分类（CR2 #5 修复）✓")


def test_assistant_pairing_regression():
    """T36: P0-1 回归 — assistant_map 必须用 pair_id (r[4]) 做 key，用 message_id (r[3]) 会全部 miss。

    生产中 assistant.message_id 恒为 None（main.py:842），用 r[3] 做 map key 会全部塞到 {None: [...]}，
    查询时用 user.message_id（非 None）查找永远空 → query_rounds* 返回的轮次只有 user 没有 assistant。

    用真实 sqlite + 复刻 Python 配对逻辑验证修复正确。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE chat_memory_records (
        id INTEGER PRIMARY KEY, umo TEXT, conversation_id TEXT, user_id TEXT,
        role TEXT, content TEXT, message_id TEXT, pair_id TEXT,
        llm_status TEXT, content_kind TEXT, created_at DATETIME
    )""")
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", "问1", "m1", None, "llm_success", '["text"]', "2026-07-14 10:00:00"),
            # assistant.message_id=None（生产真实），pair_id 指向 user.message_id
            ("umo1", "c1", "u1", "assistant", "答1", None, "m1", "llm_success", '["text"]', "2026-07-14 10:00:05"),
            ("umo1", "c1", "u1", "user", "问2", "m2", None, "llm_success", '["text"]', "2026-07-14 11:00:00"),
            ("umo1", "c1", "u1", "assistant", "答2", None, "m2", "llm_success", '["text"]', "2026-07-14 11:00:05"),
        ],
    )
    conn.commit()

    # 复刻 storage.py:query_rounds_raw 的查询逻辑
    user_rows = conn.execute(
        "SELECT role, content, user_id, message_id, pair_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'user' "
        "AND EXISTS (SELECT 1 FROM chat_memory_records a "
        "           WHERE a.umo = chat_memory_records.umo "
        "           AND a.conversation_id = chat_memory_records.conversation_id "
        "           AND a.role = 'assistant' "
        "           AND a.pair_id = chat_memory_records.message_id) "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        ("umo1", "c1", 10),
    ).fetchall()
    user_msg_ids = [r[3] for r in user_rows]

    asst_rows = conn.execute(
        "SELECT role, content, user_id, message_id, pair_id FROM chat_memory_records "
        "WHERE umo = ? AND conversation_id = ? AND role = 'assistant' "
        "AND pair_id IN ({})".format(",".join("?" * len(user_msg_ids))),
        ["umo1", "c1"] + user_msg_ids,
    ).fetchall()

    # 修复后：用 r[4]（pair_id）做 key
    amap_fixed: dict = {}
    for r in asst_rows:
        amap_fixed.setdefault(r[4], []).append(r)
    # 修复前（bug）：用 r[3]（message_id=None）做 key
    amap_buggy: dict = {}
    for r in asst_rows:
        amap_buggy.setdefault(r[3], []).append(r)

    # 关键回归：每个 user.message_id 在修复版都能配对；bug 版全部 miss
    assert all(amap_fixed.get(uid) for uid in user_msg_ids), \
        f"修复版应配对成功，map keys: {list(amap_fixed.keys())}"
    assert all(not amap_buggy.get(uid) for uid in user_msg_ids), \
        f"bug 版应全部 miss（key=None），实际: {[(uid, amap_buggy.get(uid)) for uid in user_msg_ids]}"
    # bug 版所有 assistant 堆在 None key 下
    assert list(amap_buggy.keys()) == [None], \
        f"bug 版所有 assistant 应堆在 None key，实际 keys: {list(amap_buggy.keys())}"

    conn.close()
    print("[T36] assistant_map 配对 key 回归（r[4] pair_id 而非 r[3] message_id）✓")


def test_turn_id_pairing_and_send_status():
    """T42: 新记录无平台 message_id 时依靠 turn_id 配对，并保留发送流程状态。"""
    assert "turn_id         TEXT" in _storage_src
    assert "send_status     TEXT NOT NULL DEFAULT ''" in _storage_src
    assert "ux_cm_turn_role" in _storage_src
    assert "_SCHEMA_VERSION = 2" in _storage_src
    assert "PRAGMA user_version" in _storage_src
    assert "update_llm_status_by_turn" in _storage_src
    assert "update_send_status" in _storage_src
    assert "VALID_LLM_STATUSES" in _storage_src
    assert "VALID_SEND_STATUSES" in _storage_src
    assert "send_status=_SEND_PREPARED" in _main_src
    assert "@filter.after_message_sent()" in _main_src
    assert "_SEND_ATTEMPTED" in _main_src

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE chat_memory_records ("
        "id INTEGER PRIMARY KEY, umo TEXT, conversation_id TEXT, user_id TEXT, "
        "role TEXT, message_id TEXT, pair_id TEXT, turn_id TEXT, send_status TEXT)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX ux_cm_turn_role ON chat_memory_records(turn_id, role)"
    )
    conn.executemany(
        "INSERT INTO chat_memory_records "
        "(umo, conversation_id, user_id, role, message_id, pair_id, turn_id, send_status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("umo1", "c1", "u1", "user", None, None, "turn1", ""),
            ("umo1", "c1", "u1", "assistant", None, None, "turn1", "prepared"),
        ],
    )
    conn.execute(
        "UPDATE chat_memory_records SET send_status='send_attempted' "
        "WHERE turn_id='turn1' AND role='assistant'"
    )
    row = conn.execute(
        "SELECT role, message_id, pair_id, turn_id, send_status "
        "FROM chat_memory_records WHERE turn_id='turn1' "
        "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END"
    ).fetchall()
    assert len(row) == 2 and row[0][3] == row[1][3] == "turn1"
    assert row[0][4] == "" and row[1][4] == "send_attempted"
    conn.close()
    print("[T42] turn_id 无 mid 配对 + send_status 状态流转 ✓")


def test_schema_v2_and_atomic_finalize_sql():
    """T43: 直接执行当前 schema/index，并验证 assistant finalize 事务与幂等。"""
    assert "assistant finalize requires turn_id" in _storage_src
    assert "async def update_llm_status(" not in _storage_src
    assert "_safe_update_llm_status(" not in _main_src
    tree = ast.parse(_storage_src)
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"_CREATE_TABLE_SQL", "_INDEX_DEFINITIONS", "_SCHEMA_VERSION", "_SELECT_COLS"}:
                assignments[node.targets[0].id] = ast.literal_eval(node.value)

    assert assignments["_SCHEMA_VERSION"] == 2
    conn = sqlite3.connect(":memory:")
    conn.execute(assignments["_CREATE_TABLE_SQL"])
    for sql in assignments["_INDEX_DEFINITIONS"].values():
        conn.execute(sql)
    conn.execute("PRAGMA user_version = 2")
    selected = conn.execute(
        assignments["_SELECT_COLS"] + " FROM chat_memory_records LIMIT 1"
    ).description
    assert len(selected) == 22, "_SELECT_COLS 应与 _row_to_dict 的 22 个位置一致"

    columns = {r[1] for r in conn.execute("PRAGMA table_info(chat_memory_records)")}
    assert {"turn_id", "send_status"}.issubset(columns)
    index_tables = {
        name: table for name, table in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
        )
    }
    assert all(index_tables.get(name) == "chat_memory_records" for name in assignments["_INDEX_DEFINITIONS"])

    base_values = (
        "umo1", "c1", "u1", "user", "问", None, None,
        "llm_pending", '["text"]', None, None, None, None, None,
        None, None, None, None, None, None, "turn_atomic", "",
    )
    columns_sql = (
        "umo, conversation_id, user_id, role, content, message_id, pair_id, "
        "llm_status, content_kind, platform_id, platform_name, message_type, "
        "session_id, self_id, group_id, sender_nickname, raw_timestamp, at_id, "
        "reply_id, forward_id, turn_id, send_status"
    )
    conn.execute(
        f"INSERT INTO chat_memory_records ({columns_sql}) VALUES ({','.join('?' for _ in base_values)})",
        base_values,
    )
    conn.commit()
    conn.execute("BEGIN")
    conn.execute(
        "UPDATE chat_memory_records SET llm_status='llm_success' "
        "WHERE turn_id='turn_atomic' AND role='user'"
    )
    assistant_values = list(base_values)
    assistant_values[3] = "assistant"
    assistant_values[4] = "答"
    assistant_values[7] = "llm_success"
    assistant_values[21] = "prepared"
    conn.execute(
        f"INSERT OR IGNORE INTO chat_memory_records ({columns_sql}) "
        f"VALUES ({','.join('?' for _ in assistant_values)})",
        assistant_values,
    )
    conn.commit()
    # 重放 assistant 插入被 ux_cm_turn_role 幂等忽略。
    conn.execute(
        f"INSERT OR IGNORE INTO chat_memory_records ({columns_sql}) "
        f"VALUES ({','.join('?' for _ in assistant_values)})",
        assistant_values,
    )
    rows = conn.execute(
        "SELECT role, llm_status, send_status FROM chat_memory_records "
        "WHERE turn_id='turn_atomic' ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END"
    ).fetchall()
    conn.close()
    assert rows == [
        ("user", "llm_success", ""),
        ("assistant", "llm_success", "prepared"),
    ]
    print("[T43] schema v2 + user升级/assistant写入原子事务 + 幂等 ✓")


def test_after_message_sent_marks_attempted():
    """T44: after_message_sent 只更新 send_attempted，不伪称 delivered。"""
    p = _make_plugin()
    calls = []

    class _Event:
        unified_msg_origin = _UMO_GROUP

        def get_extra(self, key, default=None):
            return {
                "chat_memory_assistant_turn_id": "turn-send",
                "chat_memory_cid": "c1",
            }.get(key, default)

    async def _update(umo, cid, turn_id, status):
        calls.append((umo, cid, turn_id, status))
        return 1

    p._safe_update_send_status = _update
    import asyncio
    asyncio.run(p.mark_send_attempted(_Event()))
    assert calls == [(_UMO_GROUP, "c1", "turn-send", "send_attempted")]
    assert "delivered" not in _main_src.split("def mark_send_attempted", 1)[1].split("# ── reset", 1)[0]
    print("[T44] after_message_sent → send_attempted（非 delivered）✓")


def test_terminate_lifecycle():
    """T22: terminate 停止清理循环并释放 DB engine。"""
    import asyncio
    p = _make_plugin()

    # 终止时 logger.info 被调用，需把 None 替换为 mock
    _orig_logger = _mod_ns["logger"]
    _mod_ns["logger"] = types.SimpleNamespace(
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
    )
    disposed = {"v": False}

    class _Eng:
        async def dispose(self): disposed["v"] = True

    async def _run():
        p._cleanup_task = None
        p.db = types.SimpleNamespace()
        p.db.engine = _Eng()

        await p.terminate()
        assert disposed["v"], "engine.dispose 没被调用"

    try:
        asyncio.run(_run())
    finally:
        _mod_ns["logger"] = _orig_logger
    print("[T22] terminate 释放 engine（关键写入已直接 await）✓")


# ── 入口 ────────────────────────────────────────────

def _run_all():
    tests = [
        test_extract_time_str,
        test_is_pure_media,
        test_user_prefix,
        test_solo_assistant_merge,
        test_head_drop,
        test_tail_solo_pop,
        test_tail_paired_assistant_kept,
        test_image_text_mixed,
        test_prefix_is_mandatory_and_solo_still_tagged,
        test_full_group_speaker_identity_and_system_instruction,
        test_no_llm_mapping,
        test_takeover_query_matrix,
        test_takeover_mode_selection,
        test_no_fire_and_forget_writes,
        test_initialize_lifecycle,
        test_current_turn_excluded_from_mixed_takeover,
        test_public_build_takeover_contexts_api,
        test_takeover_mixed_limit_and_empty_policy,
        test_takeover_character_budget,
        test_records_unconditional_sort,
        test_wal_pragma_registered,
        test_legacy_rename_releases_index_names,
        test_v236_hardening,
        test_persona_filter_pipeline,
        test_public_api_new_params,
        test_cron_not_marked_attempted,
        test_terminate_lifecycle,
        test_takeover_modes_and_whitelist,
        test_strip_reasoning_prefix,
        test_takeover_normalize_strips_think,
        test_classify_content_message_str_fallback,
        test_classify_content_components_extraction,
        test_assistant_chain_classification,
        test_assistant_pairing_regression,
        test_turn_id_pairing_and_send_status,
        test_schema_v2_and_atomic_finalize_sql,
        test_after_message_sent_marks_attempted,
        test_raw_sql_whitelist,
        test_cross_umo_full_group_mixed_sql,
        test_scope_filter_signature,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  ✗ {t.__name__} FAILED: {e}")
    print(f"\n{'=' * 50}")
    print(f"结果：{len(tests) - failed}/{len(tests)} 通过")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
