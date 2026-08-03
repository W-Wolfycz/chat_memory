"""chat_memory 单元测试（重写版，行为级；不依赖 astrbot / sqlalchemy 完整安装）。

策略：
- ast.parse + exec 加载生产源码；sys.modules 注入 AstrBot / sqlalchemy mock
- 只断言行为（输出内容 / 边界 / 隐私），不锁实现细节与参数透传
- DB 生命周期 / 迁移 / send_status 依赖真实 sqlalchemy，由 test_storage_integration.py 覆盖

覆盖（对应生产代码可测面）：
- context_builder：时间戳 / 媒体 / 前缀 / 当前发言者 XML / 合并 / 首尾 / 字符裁剪
  / Reply 还原 / 跨会话来源 / assistant cm_ 标签裁剪
- relation_codec：token / 转义 / 截断 / 当前轮 XML 锚（含 ID 隐私）
- message_classifier：组件提取 / message_str 回退 / assistant 链
- storage 纯函数：_scope_filter 4 组合 / _normalize_dt / _row_to_dict
- main：指令幂等 / 接管策略（排除当前 turn / 公开 API / 字符预算 / think 剥离）
"""

import ast
import asyncio
import enum
import json
import sqlite3
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

# ── 注入 AstrBot mock ──────────────────────────────

for m in ["astrbot.api", "astrbot.api.star", "astrbot.api.event",
          "astrbot.api.provider", "astrbot.api.message_components",
          "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message"]:
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


class _TestTextPart:
    def __init__(self, text: str):
        self.text = text
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


sys.modules["astrbot.core.agent.message"].TextPart = _TestTextPart

# 注入 sqlalchemy mock，使 storage.py 可被 exec 且模块级纯函数可用
for _mod_name, _attrs in {
    "sqlalchemy": {"bindparam": object, "event": object, "text": object},
    "sqlalchemy.ext.asyncio": {
        "create_async_engine": object, "async_sessionmaker": object,
        "AsyncSession": object,
    },
}.items():
    _m = types.ModuleType(_mod_name)
    for _n, _v in _attrs.items():
        setattr(_m, _n, _v)
    sys.modules[_mod_name] = _m

# ── 加载生产模块（ast.parse + exec，避免 import 链）──

pkg = types.ModuleType("chat_memory")
sys.modules["chat_memory"] = pkg

models_mod = types.ModuleType("chat_memory.models")
sys.modules["chat_memory.models"] = models_mod
exec(compile((PLUGIN_DIR / "models.py").read_text(), "models.py", "exec"),
     models_mod.__dict__)
setattr(pkg, "models", models_mod)

relation_codec_mod = types.ModuleType("chat_memory.relation_codec")
sys.modules["chat_memory.relation_codec"] = relation_codec_mod
exec(compile((PLUGIN_DIR / "relation_codec.py").read_text(), "relation_codec.py", "exec"),
     relation_codec_mod.__dict__)
setattr(pkg, "relation_codec", relation_codec_mod)

classifier_mod = types.ModuleType("chat_memory.message_classifier")
sys.modules["chat_memory.message_classifier"] = classifier_mod
exec(compile((PLUGIN_DIR / "message_classifier.py").read_text(),
             "message_classifier.py", "exec"), classifier_mod.__dict__)
setattr(pkg, "message_classifier", classifier_mod)

context_builder_mod = types.ModuleType("chat_memory.context_builder")
sys.modules["chat_memory.context_builder"] = context_builder_mod
exec(compile((PLUGIN_DIR / "context_builder.py").read_text(),
             "context_builder.py", "exec"), context_builder_mod.__dict__)
setattr(pkg, "context_builder", context_builder_mod)

storage_mod = types.ModuleType("chat_memory.storage")
sys.modules["chat_memory.storage"] = storage_mod
exec(compile((PLUGIN_DIR / "storage.py").read_text(), "storage.py", "exec"),
     storage_mod.__dict__)
setattr(pkg, "storage", storage_mod)

_main_src = (PLUGIN_DIR / "main.py").read_text()
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
    p.ct_include_kinds = set()
    p.ct_include_all_match = False
    p.ct_filter_by_persona = False
    p.ct_fallback_to_native_on_empty = False
    p.ct_clear_native_history = True
    p.log_with_bot_id = False
    p.debug_to_info = False
    return p


_UMO_GROUP = "aiocqhttp:GroupMessage:g1"


def _rec(role="user", content="你好", user_id="u1", nickname="Alice",
         created_at="2026-07-09 10:00:00", status="llm_success",
         kinds=None, relation=None, **extra):
    rec = {
        "role": role, "content": content, "user_id": user_id,
        "sender_nickname": nickname, "created_at": created_at,
        "content_kind": kinds or (["text"] if content else []),
        "llm_status": status, "relation_data": relation,
    }
    rec.update(extra)
    return rec


# ── 测试用例：context_builder 纯函数 ────────────────


def test_extract_time_str():
    """时间戳 → [MM/DD HH:MM:SS]，空/非法返回空串。"""
    ets = context_builder_mod.extract_time_str
    assert ets("2026-07-09 10:00:00") == "07/09 10:00:00"
    assert ets("2026-07-09T10:00:00") == "07/09 10:00:00"
    assert ets("") == ""
    assert ets(None) == ""
    assert ets("2026-07-09") == "2026-07-09"  # 不足 19 位原样
    print("[T01] extract_time_str ✓")


def test_is_pure_media():
    """全媒体判定：全媒体 True，混合/空 False。"""
    f = context_builder_mod.is_pure_media
    assert f({"content_kind": ["image"]}, {"image"}) is True
    assert f({"content_kind": ["image", "video"]}, {"image", "video"}) is True
    assert f({"content_kind": ["image", "text"]}, {"image"}) is False
    assert f({"content_kind": []}, {"image"}) is False
    assert f({}, {"image"}) is False
    print("[T02] is_pure_media ✓")


def test_strip_reasoning_prefix():
    """剥离 AstrBot 错误序列化进 Plain 的 think 前缀。"""
    f = context_builder_mod.strip_reasoning_prefix
    src = "[{'type': 'think', 'content': '内部推理', 'encrypted': None}]实际回复"
    assert f(src) == "实际回复"
    assert f("普通文本") == "普通文本"
    assert f("[{'type': 'think'") == "[{'type': 'think'"  # 不完整前缀原样
    print("[T03] strip_reasoning_prefix ✓")


def test_strip_legacy_assistant_source_prefix():
    """清理旧版 CM 注入到 assistant 正文开头的 [群N]/[私N]/[会N]/[未知] 标签。"""
    f = context_builder_mod.strip_legacy_assistant_source_prefix
    assert f("[群1] 你好") == "你好"
    assert f("[私1][会2] 你好") == "你好"
    assert f("[未知] 你好") == "你好"
    assert f("[群1] 中间[群2] 保留") == "中间[群2] 保留"  # 只清开头
    assert f("无标签") == "无标签"
    print("[T04] strip_legacy_assistant_source_prefix ✓")


def test_strip_cm_xml_tags():
    """LLM 回复入库前移除 cm_ XML 标签，保留正文及非 CM 标签。"""
    f = context_builder_mod.strip_cm_xml_tags
    assert f('<cm_source n="1"/>正文') == "正文"
    assert f('<cm_speaker current="1">Alice</cm_speaker> 你好') == "Alice 你好"
    assert f('<cm_current>\n<cm_reply target="assistant"/>\n正文\n</cm_current>') == "正文"
    assert f('<CM_TIME>08/03 10:00:00</CM_TIME>回答') == "08/03 10:00:00回答"
    assert f('<code>&lt;cm_source/&gt;</code>') == '<code>&lt;cm_source/&gt;</code>'
    assert f("普通回复") == "普通回复"
    print("[T30] strip_cm_xml_tags ✓")


def test_user_prefix_basic():
    """非 full_group：时间 + 昵称前缀，无 speaker 标记。"""
    p = _make_plugin()
    out = p._takeover_normalize([_rec()], _UMO_GROUP)
    assert out[0]["content"] == "<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Alice</cm_nickname> 你好"
    # 无时间
    out2 = p._takeover_normalize([_rec(created_at="")], _UMO_GROUP)
    assert out2[0]["content"] == "<cm_nickname>Alice</cm_nickname> 你好"
    print("[T05] user_prefix_basic ✓")


def test_full_group_speaker_xml():
    """full_group：当前用户标 <cm_speaker current="1"/>，他人不标，中性 <cm_speaker/>。"""
    p = _make_plugin()
    records = [
        _rec(content="当前用户发言", user_id="u1", nickname="Alice"),
        _rec(content="其他用户发言", user_id="u2", nickname="Bob",
             created_at="2026-07-09 10:00:03"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP, current_user_id="u1",
                                full_group=True)
    assert len(out) == 2  # 拆分：每条独立
    joined = "\n".join(c["content"] for c in out)
    assert '<cm_speaker current="1"/> <cm_nickname>Alice</cm_nickname> 当前用户发言' in joined
    assert "<cm_nickname>Bob</cm_nickname> 其他用户发言" in joined
    assert "[其他发言者]" not in joined
    assert "[当前发言者]" not in joined

    # 中性：无 current_user_id
    neutral = p._takeover_normalize(records, _UMO_GROUP, full_group=True)
    neutral_joined = "\n".join(c["content"] for c in neutral)
    assert "<cm_speaker/> <cm_nickname>Alice</cm_nickname> 当前用户发言" in neutral_joined
    assert '<cm_speaker current="1"/>' not in neutral_joined
    print("[T06] full_group_speaker_xml ✓")


def test_solo_assistant_prefix():
    """solo assistant：<cm_solo active="1"/> / <cm_solo orphan="1"/> 标记；paired assistant 保持轮次相邻。"""
    p = _make_plugin()
    records = [
        _rec(content="用户"),
        _rec(role="assistant", content="推送", status="proactive",
             created_at="2026-07-09 10:00:30"),
        _rec(role="assistant", content="配对回复",
             created_at="2026-07-09 10:00:35"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    joined = "\n".join(c["content"] for c in out)
    assert '<cm_solo active="1"/> 推送' in joined
    assert "配对回复" in joined  # proactive 并入配对 assistant，不被 solo-pop
    assert '<cm_time>07/09 10:00:30</cm_time> <cm_solo active="1"/>' in joined  # proactive 统一带时间

    orphan_records = [
        _rec(content="用户2"),
        _rec(role="assistant", content="孤立", status="orphan",
             created_at="2026-07-09 10:00:40"),
        _rec(role="assistant", content="配对回复2",
             created_at="2026-07-09 10:00:45"),
    ]
    out2 = p._takeover_normalize(orphan_records, _UMO_GROUP)
    assert '<cm_solo orphan="1"/> 孤立' in \
        "\n".join(c["content"] for c in out2)
    print("[T07] solo_assistant_prefix ✓")


def test_split_no_merge():
    """拆分模式（默认）：相邻同 role 不合并，每条独立。"""
    p = _make_plugin()
    records = [
        _rec(content="第一段", created_at="2026-07-09 10:00:00"),
        _rec(content="第二段", nickname="Bob", created_at="2026-07-09 10:00:03"),
        _rec(role="assistant", content="回复", created_at="2026-07-09 10:00:05"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 3  # 拆分：每条独立
    assert "<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Alice</cm_nickname> 第一段" in out[0]["content"]
    assert "<cm_time>07/09 10:00:03</cm_time> <cm_nickname>Bob</cm_nickname> 第二段" in out[1]["content"]
    assert "回复" in out[2]["content"]
    print("[T08] split_no_merge ✓")


def test_head_tail_normalize():
    """拆分模式：不做任何 pop，头部/尾部均保留；proactive 独立带标记。"""
    p = _make_plugin()
    records = [
        _rec(role="assistant", content="头部孤儿", created_at="2026-07-09 09:00:00"),
        _rec(content="中间用户", created_at="2026-07-09 10:00:00"),
        _rec(role="assistant", content="配对回复", created_at="2026-07-09 10:00:05"),
        _rec(role="assistant", content="尾部主动", status="proactive",
             created_at="2026-07-09 10:01:00"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 4  # 拆分：全部保留，不做 pop
    joined = "\n".join(c["content"] for c in out)
    assert "头部孤儿" in joined
    assert "中间用户" in joined
    assert "配对回复" in joined
    assert "尾部主动" in joined
    assert '<cm_solo active="1"/>' in joined  # proactive 独立保留并带标记
    print("[T09] head_tail_normalize ✓")


def test_media_filter_mixed():
    """纯媒体过滤；图文混合保留文本。"""
    p = _make_plugin()
    records = [
        _rec(content="", kinds=["image"]),          # 纯图 → 丢
        _rec(content="带图文字", kinds=["image", "text"], nickname="Bob",
             created_at="2026-07-09 10:00:03"),    # 图文 → 保留
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert len(out) == 1
    assert "带图文字" in out[0]["content"]
    print("[T10] media_filter_mixed ✓")


def test_character_budget_keeps_latest_user():
    """字符预算裁剪：始终保留最新一条 user，避免以 assistant 开头。"""
    p = _make_plugin()
    records = [
        _rec(content="长" * 100, created_at="2026-07-09 09:00:00"),
        _rec(role="assistant", content="中" * 100, created_at="2026-07-09 09:01:00"),
        _rec(content="最新用户", nickname="Bob", created_at="2026-07-09 10:00:00"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP, max_chars=120)
    assert out[0]["role"] == "user"
    assert out[-1]["content"].startswith("<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Bob</cm_nickname> 最新用户")
    print("[T11] character_budget_keeps_latest_user ✓")


def test_apply_relation_reply():
    """Reply 还原三态：turn 命中 / snapshot / 降级。"""
    p = _make_plugin()
    target_map = {("turn_1", "user"): {
        "sender_nickname": "Wolfycz", "content": "被引用的原文"}}
    # turn 命中
    rec = _rec(content="回复内容", relation={"v": 1, "mentions": [], "reply": {
        "resolution": "turn", "target_turn_id": "turn_1", "target_role": "user"}})
    out = p._takeover_normalize([rec], _UMO_GROUP, target_map=target_map)
    assert '<cm_reply target="Wolfycz">被引用的原文</cm_reply>' in out[0]["content"]
    assert "回复内容" in out[0]["content"]
    # snapshot
    rec2 = _rec(content="x", relation={"v": 1, "mentions": [], "reply": {
        "resolution": "snapshot", "target_nickname": "虾仁粽子",
        "fallback_text": "快照原文"}})
    out2 = p._takeover_normalize([rec2], _UMO_GROUP)
    assert '<cm_reply target="虾仁粽子">快照原文</cm_reply>' in out2[0]["content"]
    # 全空降级
    rec3 = _rec(content="y", relation={"v": 1, "mentions": [], "reply": {}})
    out3 = p._takeover_normalize([rec3], _UMO_GROUP)
    assert "<cm_reply/>" in out3[0]["content"]
    print("[T12] apply_relation_reply ✓")


def test_cross_session_source_tags():
    """跨会话来源 <cm_source n="N"/> 编号、未知来源、当前会话零标记。"""
    p = _make_plugin()
    records = [
        _rec(content="当前群", user_id="u1", nickname="Alice",
             created_at="2026-07-09 10:00:00", group_id="g1"),
        _rec(content="外群一", user_id="u1", nickname="旧昵称",
             created_at="2026-07-09 10:01:00", platform_id="aiocqhttp",
             message_type="GroupMessage", session_id="g2", group_id="g2"),
        _rec(content="来源缺失", user_id="u1", nickname="孤行",
             created_at="2026-07-09 10:02:00"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP, current_user_id="u1",
                                full_group=True, cross_session=True)
    joined = "\n".join(c["content"] for c in out)
    assert '<cm_speaker current="1"/> <cm_nickname>Alice</cm_nickname> 当前群' in joined
    assert '<cm_source n="1"/> <cm_nickname>旧昵称</cm_nickname> 外群一' in joined
    assert '<cm_source n="?"/> <cm_nickname>孤行</cm_nickname> 来源缺失' in joined
    # 无 cross_session：完全无 <cm_source
    out2 = p._takeover_normalize(records, _UMO_GROUP, current_user_id="u1",
                                 full_group=True)
    assert '<cm_source n="' not in "\n".join(c["content"] for c in out2)
    print("[T13] cross_session_source_tags ✓")


def test_cross_session_reply_uses_record_umo():
    """跨会话历史 Reply 按原消息 UMO 回填，不拿当前群 UMO 查询。"""
    p = _make_plugin()
    p.ct_cross_session = True
    p.ct_full_group = False

    source_umo = "aiocqhttp:GroupMessage:g2"
    replied_user = _rec(
        content="群2里的回复",
        relation={"v": 1, "mentions": [], "reply": {
            "resolution": "turn",
            "target_turn_id": "turn_g2_target",
            "target_role": "user",
        }},
        platform_id="aiocqhttp",
        message_type="GroupMessage",
        session_id="g2",
        group_id="g2",
    )

    class _ReplyDB(_FakeDB):
        def __init__(self):
            super().__init__(rounds=[[replied_user]])
            self.target_calls = []

        async def query_turn_targets(self, umo, turn_ids):
            self.target_calls.append((umo, list(turn_ids)))
            if umo == source_umo:
                return {("turn_g2_target", "user"): _rec(
                    content="群2被引用原文", nickname="Bob",
                )}
            return {}

    db = _ReplyDB()
    p.db = db
    out = asyncio.run(p.build_takeover_contexts(
        _UMO_GROUP, "u1", "cid_demo",
    ))
    assert db.target_calls == [(source_umo, ["turn_g2_target"])]
    assert '<cm_reply target="Bob">群2被引用原文</cm_reply>' in out[0]["content"]
    print("[T31] cross_session_reply_uses_record_umo ✓")


# ── 测试用例：relation_codec ─────────────────────────


def test_render_content_template_at():
    """At token → <cm_mention target="昵称"/>；越界 → 未知成员；原样文本不渲染。"""
    r = relation_codec_mod
    rel = {"v": 1, "mentions": [{"user_id": "10001", "nickname": "虾仁粽子"}],
           "reply": None}
    assert r.render_content_template("⟦CM_AT:0⟧在干嘛", rel) == '<cm_mention target="虾仁粽子"/>在干嘛'
    assert r.render_content_template("⟦CM_AT:9⟧越界", rel) == '<cm_mention target="未知成员"/>越界'
    assert r.render_content_template("⟦CM_LITERAL_AT:0⟧原文", rel) == "⟦CM_AT:0⟧原文"
    assert r.render_content_template("无 token", None) == "无 token"
    print("[T14] render_content_template_at ✓")


def test_relation_codec_tokens():
    """token 编解码与截断：转义、截断不切半 token。"""
    r = relation_codec_mod
    assert r.escape_plain_text("⟦CM_AT:0⟧") == "⟦CM_LITERAL_AT:0⟧"
    assert r.at_token(0) == "⟦CM_AT:0⟧"
    assert r.truncate_reply_snapshot("短") == "短"
    long_t = "长" * 500
    assert len(r.truncate_reply_snapshot(long_t)) <= 300
    # 截断 content 不留下半个 At token
    tmpl = "前" + "⟦CM_AT:0⟧" + "后"
    cut = r.truncate_content_template(tmpl, 1)
    assert "⟦CM_AT:" not in cut or cut.endswith("⟧")
    print("[T15] relation_codec_tokens ✓")


def test_build_current_turn_xml():
    """当前轮 XML：speaker + reply/mention + 转义 + ID 隐私。"""
    r = relation_codec_mod
    # 无 relation 无 speaker
    assert r.build_current_turn_xml("早上好", None, "s1") == "<cm_current/>"
    # 无 relation 有 speaker
    assert r.build_current_turn_xml("早上好", None, "s1", "当前用户") \
        == '<cm_speaker current="1">当前用户</cm_speaker>'
    # reply 指向 assistant
    xml = r.build_current_turn_xml(
        "好", {"v": 1, "mentions": [], "reply": {"target_role": "assistant"}},
        "s1", "当前用户")
    assert xml.startswith('<cm_current>\n<cm_speaker current="1">当前用户</cm_speaker>\n')
    assert '<cm_reply target="assistant"/>' in xml
    # At 位置保留 + 转义 + 不暴露数字 ID
    rel = {"v": 1, "mentions": [
        {"user_id": "10002", "nickname": "Alice & <A>"},
        {"user_id": "bot", "nickname": "Bot"}, ], "reply": None}
    xml2 = r.build_current_turn_xml(
        "⟦CM_AT:0⟧看看⟦CM_AT:1⟧", rel, "bot")
    assert '<cm_mention target="Alice &amp; &lt;A&gt;"/>' in xml2
    assert '<cm_mention target="assistant"/>' in xml2  # bot 命中 self_id → assistant
    assert "10002" not in xml2 and "bot_demo" not in xml2
    print("[T16] build_current_turn_xml ✓")


# ── 测试用例：message_classifier ─────────────────────


def _mk_event(components, message_str="", message_type_value=None):
    class _E:
        def get_messages(self):
            return list(components)

    _E.message_str = message_str
    if message_type_value is not None:
        class _MT:
            value = message_type_value
        _E.get_message_type = lambda self: _MT()
    return _E()


def test_classify_content_extraction():
    """组件提取：纯图 / At+文本 / 纯 At / Reply / Forward / 混合 kind。"""
    components = sys.modules["astrbot.api.message_components"]
    p = _make_plugin()

    img = components.Image()
    kind, at, rep, fwd = p._classify_content(_mk_event([img]))
    assert kind == ["image"] and at is None and rep is None and fwd is None

    at_c = components.At(); at_c.qq = "123456"
    pl = components.Plain(); pl.text = "你好"
    kind, at_id, _, _ = p._classify_content(_mk_event([at_c, pl]))
    assert kind == ["text"] and at_id == "123456"

    rp = components.Reply(); rp.id = "msg_abc"
    pl2 = components.Plain(); pl2.text = "回复内容"
    kind, _, reply_id, _ = p._classify_content(_mk_event([rp, pl2]))
    assert kind == ["text"] and reply_id == "msg_abc"

    fw = components.Forward(); fw.id = "fwd_001"
    kind, _, _, forward_id = p._classify_content(_mk_event([fw]))
    assert kind == ["forward"] and forward_id == "fwd_001"

    img2 = components.Image()
    vc = components.Record()
    pl3 = components.Plain(); pl3.text = "看图听话"
    kind, _, _, _ = p._classify_content(_mk_event([img2, vc, pl3]))
    assert kind == ["image", "voice", "text"]
    print("[T17] classify_content_extraction ✓")


def test_classify_content_message_str_fallback():
    """chain 无文本时回退 event.message_str 补 text。"""
    p = _make_plugin()
    kind, _, _, _ = p._classify_content(_mk_event([], message_str="你好啊"))
    assert kind == ["text"]
    # chain 有 Plain 时不走回退
    components = sys.modules["astrbot.api.message_components"]
    pl = components.Plain(); pl.text = "hello"
    kind, _, _, _ = p._classify_content(_mk_event([pl], message_str=""))
    assert kind == ["text"]
    # 全空保持 []
    kind, _, _, _ = p._classify_content(_mk_event([], message_str=""))
    assert kind == []
    print("[T18] classify_content_message_str_fallback ✓")


def test_classify_assistant_chain():
    """assistant 组件链分类：纯文本 / 图片+文本。"""
    components = sys.modules["astrbot.api.message_components"]
    p = _make_plugin()

    pl = components.Plain(); pl.text = "回复正文"
    kind, text = p._classify_assistant_chain([pl])
    assert kind == ["text"] and text == "回复正文"

    img = components.Image()
    pl2 = components.Plain(); pl2.text = "带图"
    kind, text = p._classify_assistant_chain([img, pl2])
    assert kind == ["image", "text"] and text == "带图"

    kind, text = p._classify_assistant_chain([])
    assert kind == [] and text == ""
    print("[T19] classify_assistant_chain ✓")


# ── 测试用例：storage 纯函数 ─────────────────────────


def test_scope_filter_matrix():
    """_scope_filter 4 组合 + 空 user_id 边界。"""
    sf = storage_mod._scope_filter
    umo, uid = "aiocqhttp:GroupMessage:g1", "u1"

    cond, params = sf(umo, uid, False, False)
    assert cond == "umo = :scope_umo AND user_id = :scope_uid"
    assert params == {"scope_umo": umo, "scope_uid": uid}

    cond, params = sf(umo, uid, True, False)
    assert "platform_id = :scope_pid" in cond and "user_id = :scope_uid" in cond

    cond, params = sf(umo, uid, False, True)
    assert cond == "umo = :scope_umo" and params == {"scope_umo": umo}

    cond, _ = sf(umo, uid, True, True)
    assert "umo = :scope_umo" in cond and "platform_id = :scope_pid" in cond

    # 空 user_id：整群可降级，否则恒假
    assert sf(umo, "", True, False) == ("1 = 0", {})
    cond, params = sf(umo, "", False, True)
    assert cond == "umo = :scope_umo"
    print("[T20] scope_filter_matrix ✓")


def test_normalize_dt():
    """datetime 归一化：None 透传 / naive 原样 / aware 转 UTC naive。"""
    from datetime import datetime, timezone
    nd = storage_mod._normalize_dt
    assert nd(None) is None
    naive = datetime(2026, 7, 9, 2, 0, 0)
    assert nd(naive) == naive
    aware = datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc)
    assert nd(aware).tzinfo is None
    print("[T21] normalize_dt ✓")


def test_row_to_dict_mapping():
    """_row_to_dict：字段映射 + content 渲染 + created_at/UTC。"""
    from datetime import datetime, timezone
    r = (
        "user", "⟦CM_AT:0⟧在干嘛", "u1", "m1", "p1", "llm_success",
        '["text"]', "aiocqhttp", "cq", "GroupMessage", "g1", "s1",
        "g1", "Alice", 0, "at1", "rep1", "fwd1", "", "2026-07-09 02:00:00",
        "turn_1", "", None, 42,
    )
    rel = {"v": 1, "mentions": [{"user_id": "10001", "nickname": "虾仁粽子"}],
           "reply": None}
    r_with_rel = list(r)
    r_with_rel[1] = "⟦CM_AT:0⟧在干嘛"
    r_with_rel[22] = json.dumps(rel, ensure_ascii=False)
    d = storage_mod._row_to_dict(tuple(r_with_rel))
    assert d["role"] == "user"
    assert d["content"] == '<cm_mention target="虾仁粽子"/>在干嘛'   # relation 渲染
    assert d["content_template"] == "⟦CM_AT:0⟧在干嘛"
    assert d["user_id"] == "u1" and d["message_id"] == "m1"
    assert d["content_kind"] == ["text"]
    assert d["relation_data"]["v"] == 1
    assert d["record_id"] == 42
    # tz 输出
    tz = timezone.utc
    d2 = storage_mod._row_to_dict(tuple(r), tz)
    assert d2["created_at_utc"].endswith("Z")
    print("[T22] row_to_dict_mapping ✓")


# ── 测试用例：main 指令与接管策略 ────────────────────


def test_instruction_idempotent():
    """3 条 system 指令幂等追加：重复调用只加一次。"""
    p = _make_plugin()
    req = types.SimpleNamespace(system_prompt="原有")
    for _ in range(2):
        p._append_general_rules(req)
        p._append_full_group_instruction(req)
        p._append_cross_session_instruction(req)
    assert req.system_prompt.count("ChatMemory 通用规则") == 1
    assert req.system_prompt.count("ChatMemory 群聊历史解释规则") == 1
    assert req.system_prompt.count("ChatMemory 跨会话来源规则") == 1
    assert "群友视角" in req.system_prompt
    assert "按 ChatMemory 配置筛选的群聊历史片段" in req.system_prompt
    assert "必须按发送者" in req.system_prompt
    assert "无 <cm_speaker> 也无 <cm_source>" in req.system_prompt
    assert "续写角色" in req.system_prompt
    assert "每条默认回应其前最近" in req.system_prompt
    assert "可合并参考" in req.system_prompt
    assert "来源标记仅供你理解" in req.system_prompt
    assert "<cm_speaker" in req.system_prompt
    assert "闲聊触发" in req.system_prompt
    print("[T23] instruction_idempotent ✓")


def test_takeover_normalize_think():
    """_takeover_normalize 注入时剥离老库已存的 think 前缀。"""
    p = _make_plugin()
    records = [
        _rec(content="你好"),
        _rec(role="assistant",
             content="[{'type': 'think', 'content': '推理', 'encrypted': None}]实际回复",
             created_at="2026-07-09 10:00:05"),
    ]
    out = p._takeover_normalize(records, _UMO_GROUP)
    assert out[0]["content"] == "<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Alice</cm_nickname> 你好"
    assert out[1]["content"] == "<cm_time>07/09 10:00:05</cm_time> 实际回复"
    print("[T24] takeover_normalize_think ✓")


class _FakeDB:
    """最小可配 db 桩：只实现 _takeover_query 需要的查询。"""

    def __init__(self, rounds=None, messages=None):
        self.rounds = rounds or []       # list[list[dict]]，pair 模式
        self.messages = messages or []   # list[dict]，mixed 模式
        self.last_kwargs = {}

    async def query_rounds_raw(self, *a, **k):
        self.last_kwargs = dict(k)
        return self.rounds

    async def query_messages_raw(self, *a, **k):
        self.last_kwargs = dict(k)
        return self.messages

    async def query_turn_targets(self, *a, **k):
        return {}


def test_public_build_takeover_contexts_api():
    """公开 API：None / [] / list 语义与 user_id 边界。"""
    p = _make_plugin()

    p.ct_enable = False
    assert asyncio.run(p.build_takeover_contexts(_UMO_GROUP, "u1", "cid")) is None
    p.ct_enable = True
    assert asyncio.run(p.build_takeover_contexts("", "u1", "cid")) == []
    assert asyncio.run(p.build_takeover_contexts(_UMO_GROUP, "", "cid")) == []

    p.db = _FakeDB(rounds=[[_rec()]])
    out = asyncio.run(p.build_takeover_contexts(_UMO_GROUP, "u1", "cid_demo"))
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["content"].startswith("<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Alice</cm_nickname> 你好")
    assert out[0].get("_no_save") is True
    print("[T25] public_build_takeover_contexts_api ✓")


def test_current_turn_excluded_from_mixed():
    """混合模式排除当前 turn_id（透传给 query_messages_raw）。"""
    p = _make_plugin()
    p.ct_llm_status_filter = ["llm_success", ""]  # mixed
    p.db = _FakeDB(messages=[])
    asyncio.run(p.build_takeover_contexts(
        _UMO_GROUP, "u1", "cid_demo", exclude_turn_id="turn_x"))
    assert p.db.last_kwargs.get("exclude_turn_id") == "turn_x"
    print("[T26] current_turn_excluded_from_mixed ✓")


def test_takeover_character_budget_api():
    """接管字符预算：跨多轮裁剪，始终保留最新完整 user 起点。"""
    p = _make_plugin()
    p.ct_max_context_chars = 60
    p.db = _FakeDB(rounds=[
        [_rec(content="长" * 30, created_at="2026-07-09 08:00:00"),
         _rec(role="assistant", content="回1", created_at="2026-07-09 08:00:05")],
        [_rec(content="长" * 30, nickname="Bob", created_at="2026-07-09 09:00:00"),
         _rec(role="assistant", content="回2", created_at="2026-07-09 09:00:05")],
        [_rec(content="最新", created_at="2026-07-09 10:00:00")],
    ])
    out = asyncio.run(p.build_takeover_contexts(_UMO_GROUP, "u1", "cid_demo"))
    assert len(out) == 1
    assert out[0]["content"].startswith("<cm_time>07/09 10:00:00</cm_time> <cm_nickname>Alice</cm_nickname> 最新")
    print("[T27] takeover_character_budget_api ✓")


def test_public_contexts_no_history_tail():
    """公开 API 只返回历史 contexts，不含 cm_history_tail 标记。"""
    p = _make_plugin()
    p.db = _FakeDB(rounds=[[_rec(content="历史尾部")]])
    contexts = asyncio.run(p.build_takeover_contexts(_UMO_GROUP, "u1", "cid_demo"))
    assert isinstance(contexts, list) and contexts
    assert "历史尾部" in contexts[0]["content"]
    assert not any("cm_history_tail" in str(c.get("content", ""))
                   for c in contexts)
    print("[T28] public_contexts_no_history_tail ✓")


# ── 运行 ─────────────────────────────────────────────


def _run_all():
    tests = sorted(
        (name, obj) for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    )
    passed = 0
    failed = []
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, exc))
    print("\n" + "=" * 50)
    if failed:
        print(f"结果：{passed}/{passed + len(failed)} 通过")
        for name, exc in failed:
            print(f"  ✗ {name} FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"结果：{passed}/{passed} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
