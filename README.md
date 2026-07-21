# ChatMemory

以 `UMO + conversation_id + user_id` 为维度的对话存档插件。所有进入 ProcessStage 的消息立即落库 SQLite，每条记录带两个独立维度的状态字段（`llm_status` + `content_kind`）。默认纯旁路存档；开启上下文接管后可让 CM 成为唯一上下文源。

当前发布版本：`1.1.0`。此前版本统一视为内部 `0.x` 测试版；插件版本与数据库 schema 版本相互独立，当前数据库 `PRAGMA user_version=3`。

## 特性

- **全量捕获**：所有进入 ProcessStage 的 user 消息 + BOT 回复立即落库（命令、闲聊、纯媒体、混合消息都存）
- **配对存储**：新记录用 `turn_id` 配对，旧记录兼容 assistant `pair_id`（= 对应 user 的 `message_id`）
- **双列状态**：`llm_status` 描述 LLM 路径，`content_kind` 描述内容形态，正交独立
- **可选接管上下文**：开启 `context_takeover` 后接管 LLM 的 contexts 注入
- **命令感知**：自动识别 `/reset`（清空 CID 存档）与 `/new`（保留旧 CID 记录）
- **自动清理**：`auto_cleanup_days > 0` 时启动周期清理任务

## 双列状态体系

| 字段 | 取值 | 含义 |
|---|---|---|
| `llm_status` | `""` | 默认，未走 LLM（命令、`set_result`、无回复、纯媒体） |
| | `llm_pending` | LLM 触发但 assistant 未成功 |
| | `llm_success` | LLM 路径且 assistant 成功回复 |
| | `proactive` | 主动消息（assistant 单边，含 cron） |
| | `orphan` | user 漏存（DB 写入失败） |
| `content_kind` | `text` / `image` / `video` / `voice` / `file` / `face` / `forward` / `system_event` | JSON 数组，可多值 |
| | `[]` 空数组 | empty（如纯 @BOT 无文字、纯 Reply 无文字） |

`at` / `reply` 不入 `content_kind`。`reply_id` 保留平台引用 ID；`at_id` 已弃用但在
1.x 继续双写第一个普通 At，以兼容旧调用方。新消息通过 `relation_data` 保存完整有序 At
参数与 Reply 关系，正文在库内使用 `⟦CM_AT:n⟧` 模板，查询返回的 `content` 会渲染为
`[提及:昵称]`，并额外提供原始 `content_template`。

**配对规则**：新记录用内部 `turn_id` 关联 user-assistant；旧记录继续用 `pair_id` 关联 user `message_id`。`proactive` / `orphan` 仍由 `llm_status` 表达单边语义。平台无 `message_id` 时新记录也能配对。

## 配置

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_content_length` | int | 0 | 单条记录最大字符数。**0 = 不限制** |
| `auto_cleanup_days` | int | 0 | 自动清理天数，**0 = 不清理**；>0 启动周期任务（24h 一次） |
| `log_config.log_with_bot_id` | bool | false | 日志前缀附加机器人 ID |
| `log_config.debug_to_info` | bool | false | debug 日志提级为 info |
| `context_takeover` | object | — | 上下文接管配置，详见 [上下文接管](#上下文接管) |

## 数据存储

数据库：`data/plugin_data/chat_memory/chat_memory.db`

```sql
CREATE TABLE chat_memory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,                  -- 'user' 或 'assistant'
    content         TEXT NOT NULL DEFAULT '',
    message_id      TEXT,                           -- 平台消息 id（NULL 表示平台未给；assistant 恒为 NULL）
    pair_id         TEXT,                           -- 仅 assistant：绑定的 user.message_id
    llm_status      TEXT NOT NULL DEFAULT '',
    content_kind    TEXT NOT NULL DEFAULT '[]',     -- JSON 数组：内容形态
    platform_id     TEXT,                           -- 平台实例 ID
    platform_name   TEXT,                           -- 平台类型（aiocqhttp / lark / ...）
    message_type    TEXT,                           -- FriendMessage / GroupMessage / OtherMessage
    session_id      TEXT,                           -- 会话标识（QQ 号 / 群号）
    self_id         TEXT,                           -- 机器人自身 ID
    group_id        TEXT,                           -- 群号（仅群聊）
    sender_nickname TEXT,
    raw_timestamp   INTEGER,                        -- 消息原始 Unix 时间戳（秒）
    at_id           TEXT,                           -- At 目标 ID
    reply_id        TEXT,                           -- Reply 引用消息 ID
    forward_id      TEXT,                           -- Forward 平台 ID
    persona_id      TEXT,                           -- 生效 persona，用于可选隔离
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    turn_id         TEXT,                           -- ChatMemory 内部轮次 ID，不依赖平台 message_id
    send_status     TEXT NOT NULL DEFAULT '',       -- assistant: prepared / send_attempted
    relation_data   TEXT                             -- v1 At 模板参数 + Reply turn/snapshot
);
```

> **platform_id vs platform_name**：自 AstrBot v4.0 起 umo 第一段是实例 ID（多 aiocqhttp 实例时每个不同），`platform_name` 才是类型。
>
> **raw_timestamp vs created_at**：`raw_timestamp` 是消息到达 AstrBot 的时间（Unix 秒，平台给出，本地时区），`created_at` 是落库时间（**UTC naive 存储**，与 schema default `CURRENT_TIMESTAMP` 对齐；**查询返回时按 AstrBot `timezone` 配置转成配置时区 naive 字符串**）。
>
> **测试版数据库升级**：启动时使用 `PRAGMA user_version` + 实际列检查。已有 `llm_status` 的数据库会增量补 `persona_id` / `turn_id` / `send_status` / `relation_data`；更早、缺少 `llm_status` 的表不会猜测字段语义，而是先 RENAME 为 `chat_memory_records_backup_<ts>` 再建立当前主表。迁移后会校验索引绑定，并将数据库 schema version 写为 3。

> **turn_id 与发送状态**：1.0.0 的实时写入和状态升级统一使用内部 `turn_id`；历史记录查询仍保留 `message_id` / `pair_id` 回退，确保现有数据库中的旧轮次可读。`send_status=prepared` 表示 assistant 已写入、准备发送；`send_attempted` 仅表示 AstrBot 发送流程结束/已尝试，不等价于平台送达回执。

### 升级与备份

- 已有 schema v2 数据库可直接启动 1.1.0；迁移仅增加 nullable `relation_data`，旧行内容不重写。
- 数据库启用 WAL。AstrBot 运行时不要只复制 `chat_memory.db` 主文件，否则可能漏掉 `.db-wal` 中尚未 checkpoint 的记录；应先停止 AstrBot，或使用 SQLite `backup()` API。
- 升级前仍建议保留一次独立备份。插件不会自动删除历史备份表。

## 供其他插件调用

> ⚠️ **不要直接 `from chat_memory.main import ...`**：AstrBot 的插件加载机制不保证模块级 import 可用。统一用 `get_registered_star`。

```python
def _resolve_chat_memory(context):
    try:
        star = context.get_registered_star("chat_memory")
        if star is not None:
            for candidate in (star, getattr(star, "star", None), getattr(star, "star_cls", None)):
                if candidate is not None and hasattr(candidate, "query_history"):
                    return candidate
    except Exception:
        pass
    return None


cm = _resolve_chat_memory(context)
```

### API

| 函数 | 参数 | 返回 |
|---|---|---|
| `query_history` | `umo, conversation_id, user_id=None, limit=20, llm_status=None, content_kind=None, role_filter=None, persona_id=None, since=None, until=None, from_oldest=False` | `list[dict]`，按时间正序 |
| `query_rounds` | `umo, conversation_id, user_id=None, limit_rounds=10, llm_status=None, content_kind=None, persona_id=None, since=None, until=None, from_oldest=False` | `list[list[dict]]`，按 `turn_id` 配对；旧数据回退 `pair_id` |
| `build_takeover_contexts` | `umo, user_id, conversation_id=None, persona_id="", exclude_turn_id=""` | `list[dict] \| None`，按当前接管配置构建可直接传给 LLM 的 contexts |

**参数行为**

- `user_id` 为空时不按用户过滤，返回该会话所有用户混合记录（群聊场景）
- `llm_status` 支持 str 或 list[str]（list 走 SQL `IN`）。空串过滤用 `[""]`
- `content_kind` ANY 语义：返回数组中任一包含指定值的记录
- `query_rounds` 的 `llm_status` / `content_kind` 只过滤 user 侧；persona 和时间条件同时作用于 user、assistant 及配对检查
- `query_rounds` 只返回严格完整的 `[user, assistant]`；旧数据存在重复 assistant 时按最早 `created_at + id` 取一条
- `persona_id`：`None` 不过滤；非空按值过滤；空串 `""` 严格过滤 `IS NULL OR ''`
- `since` / `until` 给定时按 `created_at` 过滤时间窗口（含端点）；`datetime` 为 tz-aware 时自动转 UTC naive，naive 假定已是 UTC（与落库 `CURRENT_TIMESTAMP` 对齐）
- 返回记录同时提供 `created_at`（配置时区 naive 字符串）和明确的 `created_at_utc`（UTC ISO 8601 字符串）。新调用方应使用 `created_at_utc` 作为 UTC 游标。
- `from_oldest=false` 保持原行为：选最新 N 条/轮后按时间正序返回；设为 `true` 时从最旧记录开始选 N 条/轮，返回顺序仍为正序。
- 新关系记录的 `content` 是渲染后的可读文本，`content_template` 是库内原始模板，`relation_data` 是结构化 At/Reply 数据；旧记录的 `relation_data` 为 `None`，不会推测性回填。
- `query_rounds` 的配对检查会复用 persona / 时间条件，避免过滤后返回孤立 user

### 群聊 At / Reply 关系

- At 按 MessageChain 原始位置写成 `⟦CM_AT:0⟧` 等带索引模板；`relation_data.mentions` 按出现顺序保存参数，重复 At 不去重，AtAll 使用 `{"all": true}`。
- 普通成员 Reply 只用同一平台实例、同一 UMO 下唯一的 `reply_id → user.message_id` 精确解析为 `target_turn_id`；零命中或多命中都不猜测。
- 引用 Bot/assistant 时不做时间或内容启发式匹配，直接保存 AstrBot Reply 组件提供的昵称与最多 300 字符文本快照；其他无法精确解析的 Reply 同样降级为快照。
- takeover 会把关系渲染为 `[回复 → 昵称 | 原文: ...]` 与正文中的 `[提及:昵称]`。旧记录没有 `relation_data` 时完全保持旧格式。

**`build_takeover_contexts` 返回语义**

- `context_takeover.enable=false` 时返回 `None`
- 接管已启用但 CID、用户范围、查询记录或规整结果为空时返回 `[]`
- 完整复用 CM 当前接管查询与规整逻辑，自动遵循 scope、状态、内容白名单、Persona、固定身份前缀、条数和字符预算
- 只读构建 contexts：不清理 AstrBot native history、不修改数据库，也不修改调用方请求对象
- `conversation_id=None` 时读取该 UMO 的当前 CID；没有当前 CID 时返回 `[]`
- `user_id=""` 仅在 `full_group=true` 且 UMO 确为群聊时允许；此时忽略 `cross_session`，严格限制为当前 UMO + 当前 CID 的整群记录。其他情况返回 `[]`

### 调用示例

```python
# LLM 配对历史
history = await cm.query_history(umo, cid, uid, limit=20, llm_status="llm_success")

# 按完整轮次取 LLM 配对
rounds = await cm.query_rounds(umo, cid, uid, limit_rounds=10, llm_status="llm_success")

# 非 LLM 路径（命令、set_result 等）
cmds = await cm.query_history(umo, cid, uid, limit=50, llm_status=[""])

# 群聊全量历史（不按用户过滤）
group_all = await cm.query_history(umo, cid, limit=50)

# 按 persona 过滤（只看 default persona 下的对话）
history = await cm.query_history(umo, cid, uid, persona_id="default")

# 按时间窗口查最近 24 小时
from datetime import datetime, timedelta, timezone
since = datetime.now(timezone.utc) - timedelta(days=1)
recent = await cm.query_history(umo, cid, uid, since=since, limit=100)

# persona + 时间窗口组合
rounds = await cm.query_rounds(
    umo, cid, uid, persona_id="default", since=since, limit_rounds=20,
)

# 为主动消息等独立 LLM 调用构建与 CM 接管完全一致的只读 contexts
contexts = await cm.build_takeover_contexts(
    umo=umo,
    user_id=uid,
    conversation_id=cid,
    persona_id="default",
)
```

## 上下文接管

让 ChatMemory 成为唯一上下文源：每轮 LLM 请求时用 CM 数据覆盖 `req.contexts`，并清空 native `conversation.history` 防累积。

默认采用**严格接管**：即使 CM 查询无数据或过滤后为空，也会显式把 `req.contexts` 置空，不会静默回退到 native history。若确实需要兼容回退，可开启 `fallback_to_native_on_empty`。

CM 在所有 `on_llm_request` 钩子中最后执行（priority=-100）；注入内容标 `_no_save`，不回写 native。

### 接管范围

`cross_session` 与 `full_group` 是两个独立 checkbox：

| `cross_session` | `full_group` | 数据范围 | 适用场景 |
|---|---|---|---|
| F | F | 当前 umo 当前 user | 默认，与 native 等价但走 CM 数据源 |
| T | F | 跨 umo 当前 user | **群私聊互通**：同一用户在所有群 + 私聊的对话都进入上下文 |
| F | T | 当前 umo 整群全员 | 群聊让 LLM 看到所有发言者 |
| T | T | 当前 umo 整群 + 其他 umo 当前 user | 群私聊互通 + 整群 |

> **full_group 仅群聊生效**：私聊自动降级为本用户。
> **隐私**：full_group 开启后，群内其他人发言及昵称会注入 LLM。时间、发送者和身份前缀属于正确归因所需的协议字段，不提供关闭选项。
> **scope 实现**：storage 层 `_scope_filter(umo, user_id, cross_umo, full_group)` 按 4 种组合构造 WHERE — F/F=`umo+user_id`、T/F=`platform_id+user_id`、F/T=`umo`、T/T=前两者 OR。跨 umo 时 EXISTS 子查的 `a.umo = chat_memory_records.umo` 保证 user/assistant 在同一 umo 内配对。空 `user_id` 不允许进入跨 UMO scope：仅 `full_group` 可降级到当前 UMO，公开 API 会进一步固定当前 CID。

### 状态过滤

选哪些状态的消息进入上下文，多选。各状态含义见 [双列状态体系](#双列状态体系)。

| UI 选项 | DB 值 | 含义 |
|---|---|---|
| `llm_success` | `llm_success` | LLM 成功配对（默认） |
| `llm_pending` | `llm_pending` | 触发 LLM 但失败（孤儿 user） |
| `no_llm` | `""` | 非 LLM 路径（UI 占位符，DB 是空串） |
| `proactive` | `proactive` | 主动消息（cron、插件主动） |
| `orphan` | `orphan` | user 漏存但 assistant 来了 |

开启 `proactive`/`orphan` 后，单边 assistant 会带 `[主动]`/`[未配对]` 前缀注入，让 LLM 知道这是 bot 单方面说的。

混合状态模式会排除当前正在请求 LLM 的 `turn_id`，避免本轮 user 消息同时出现在历史 contexts 和当前 prompt 中。

### 轮数 vs 消息数（limit_rounds 含义）

`limit_rounds` 的语义随 `llm_status_filter` 选择动态变化：

| `llm_status_filter` | limit_rounds 含义 | SQL 策略 |
|---|---|---|
| 仅 `llm_success` | **轮数**（user-assistant 一对为一轮） | 配对查询，按配对切 N 轮 |
| 含其他状态（`proactive`/`no_llm`/...） | **消息数**（单条记录） | 全量查询，按条数切 N 条 |

**例**：`limit_rounds=30`
- 只选 `llm_success` → 30 轮配对（≈60 条记录）
- 选了 `llm_success + proactive` → 30 条消息（可能是 14 user + 14 assistant + 2 proactive）

**轮数精确**：内容白名单下沉到 SQL 层，被过滤的记录不占用 limit 名额。

### 固定前缀与群聊身份

user 历史始终带时间和发送者前缀，不再提供关闭配置：

```text
[MM/DD HH:MM:SS] SenderName: content
```

`full_group` 在群聊中生效时，同一个 `role=user` 可能是多个连续群成员发言合并后的转录容器。CM 保持 OpenAI 兼容的 U/A 交替结构，并给每段增加身份标记：

```text
[MM/DD HH:MM:SS] [当前发言者] SenderName: content
[MM/DD HH:MM:SS] [其他发言者] SenderName: content
```

如果公开 API 调用方未提供 `user_id`，使用中性的 `[发言者]`。ChatMemory 自身接管还会向 `system_prompt` 追加固定的群聊解释规则，要求模型按标记分别归因；当前用户的新 `prompt` 仍由 AstrBot 在历史 contexts 之后追加。

配对 assistant 不加前缀（角色已明确是 bot 自身）；单边 assistant 使用 `[MM/DD HH:MM:SS] [主动]` / `[未配对]`。

### 配置项

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | false | 总开关 |
| `cross_session` | bool | false | 跨 umo（群私聊互通） |
| `full_group` | bool | false | 整群消息（仅群聊） |
| `limit_rounds` | int | 30 | 注入轮数或消息数；最小按 1 处理，不限制上限（含义随状态过滤变化，见上） |
| `max_context_chars` | int | 0 | 规整后字符预算，超出从最旧完整 user 起点裁剪；0=关闭 |
| `llm_status_filter` | chip | `["llm_success"]` | 状态多选 |
| `include_content_kinds` | chip | `["text"]` | 内容白名单（清空=不过滤） |
| `include_all_match` | bool | false | ALL 模式开关（默认 ANY） |
| `clear_native_history` | bool | true | 每轮清空 native history |
| `fallback_to_native_on_empty` | bool | false | CM 空结果时是否保留 AstrBot 原生 contexts；默认 false=严格接管 |

### 内容白名单（include_content_kinds）

**只影响 takeover**，capture 照常入库。

`include_content_kinds` 是白名单：选中=需要进入上下文的 kind。空集合 = 不过滤（全部进入）。配合 `include_all_match` 切换 ANY / ALL 两种语义：

| `include_all_match` | 语义 | 保留条件 | 例子（白名单 `["text"]`） |
|---|---|---|---|
| false（默认） | ANY | content_kind 与白名单**任一交集** | `["text"]` ✓ / `["text","image"]` ✓ / `["image"]` ✗ / `[]` ✗ |
| true | ALL | content_kind **全部**属于白名单（且非空） | `["text"]` ✓ / `["text","image"]` ✗ / `["image"]` ✗ / `[]` ✗ |

**典型场景**：
- **默认 ANY + `["text"]`**：含文本即进（纯文 ✓ / 文+图 ✓ / 纯图 ✗ / poke 通知 ✗）
- **ALL + `["text","image"]`**：精确限定为这两种 kind
- **清空**：不过滤，全量进入

### persona 隔离（filter_by_persona）

**只影响 takeover 查询**，capture 照常全量入库（每条记录带 `persona_id` 列）。

`filter_by_persona` 开启后，takeover 查询严格按当前 persona 过滤——切换 persona 时旧 persona 时期的对话自动隔离，切回后自然回归。`persona_id` 为空时过滤 `IS NULL OR ''`（匹配老库旧行），并打 warn 日志提醒。

**与 cross_session 的协同**：

| `filter_by_persona` | `cross_session` | 切 persona + /new + 切回旧行为 |
|---|---|---|
| F | F | 现行行为，看到所有数据 |
| T | F | persona 隔离，但 /new 后切不回旧 persona 数据（cid 卡死） |
| F | T | 跨 cid 聚合，persona 不卡 |
| **T** | **T** | **persona 严格过滤 + 跨 cid 聚合**——完整隔离体验 |

**设计说明**：
- cid **不会自动**随 persona 切换——同一 cid 下可能累积多个 persona 的数据，按 `persona_id` 列区分
- 旧测试版数据库启动时自动 `ALTER TABLE ADD COLUMN persona_id TEXT` 补列，旧行 `persona_id` 为 NULL
- 入库时 `persona_id` 取自 `_get_effective_persona`（走 `persona_manager.resolve_selected_persona`，与 LLM 实际生效 persona 同源）

## 已知限制

- **首条消息是非 LLM 命令时漏存**：cid 尚未创建，且无 LLM 钩子兜底
- **/reset 与 /new 区分靠文本匹配**：AstrBot 未提供官方区分 API；若 bot 回复碰巧含 "reset" 字样会误判清库

## 依赖

由 AstrBot 宿主环境提供：`sqlalchemy[asyncio]>=2.0.41`、`aiosqlite>=0.21.0`

版本历史见 [CHANGELOG.md](CHANGELOG.md)。
