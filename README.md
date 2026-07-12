# ChatMemory

以 `UMO + conversation_id + user_id` 为维度的对话存档插件。所有进入 ProcessStage 的消息立即落库 SQLite，每条记录带两个独立维度的状态字段（`llm_status` + `content_kind`）。默认纯旁路存档；开启上下文接管后可让 CM 成为唯一上下文源。

## 特性

- **全量捕获**：所有进入 ProcessStage 的 user 消息立即落库（命令、闲聊、纯媒体、空消息都存）
- **配对存储**：assistant 带 `pair_id`（= 对应 user 的 `message_id`），便于重组成轮
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

`at` / `reply` 不入 `content_kind`，用独立字段 `at_id` / `reply_id` 表达。

**配对规则**：caller 用 `pair_id` 关联 user-assistant。`proactive` / `orphan` 不配对（`pair_id=NULL`）。平台无 `message_id` 时无法配对。

## 配置

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_content_length` | int | 500 | 单条记录最大字符数。**0 = 不限制** |
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
    message_id      TEXT,                           -- 平台消息 id（NULL 表示平台未给）
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
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

> **platform_id vs platform_name**：自 AstrBot v4.0 起 umo 第一段是实例 ID（多 aiocqhttp 实例时每个不同），`platform_name` 才是类型。
>
> **raw_timestamp vs created_at**：`raw_timestamp` 是消息到达 AstrBot 的时间，`created_at` 是落库时间。

**老库处理**：v2.3 不做兼容迁移。启动时检测到老 schema 直接 DROP 重建，老数据丢失。

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
| `query_history` | `umo, conversation_id, user_id=None, limit=20, llm_status=None, content_kind=None, role_filter=None` | `list[dict]`，按时间正序 |
| `query_rounds` | `umo, conversation_id, user_id=None, limit_rounds=10, llm_status=None, content_kind=None` | `list[list[dict]]`，每轮 `[user, assistant]` 两条 |

**参数行为**

- `user_id` 为空时不按用户过滤，返回该会话所有用户混合记录（群聊场景）
- `llm_status` 支持 str 或 list[str]（list 走 SQL `IN`）。空串过滤用 `[""]`
- `content_kind` ANY 语义：返回数组中任一包含指定值的记录
- `query_rounds` 仅过滤 user 侧；assistant 仍按配对字段返回

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
```

## 上下文接管

让 ChatMemory 成为唯一上下文源：每轮 LLM 请求时用 CM 数据覆盖 `req.contexts`，并清空 native `conversation.history` 防累积。

CM 在所有 `on_llm_request` 钩子中最后执行（覆盖其他插件对 contexts 的修改）；注入内容标 `_no_save`，不回写 native。

### 接管范围

`cross_session` 与 `full_group` 是两个独立 checkbox：

| `cross_session` | `full_group` | 数据范围 | 适用场景 |
|---|---|---|---|
| F | F | 同 CID 同用户 | 默认，与 native 等价但走 CM 数据源 |
| T | F | 跨 CID 同用户 | `/new` 或 `/reset` 后仍记得上一个会话 |
| F | T | 同 CID 全群 | 群聊让 LLM 看到所有发言者 |
| T | T | 跨 CID 全群 | 跨会话 + 整群聚合 |

> **full_group 仅群聊生效**：私聊自动降级为本用户。
> **隐私**：full_group 开启后，群内其他人发言（含昵称）会注入 LLM。可用 `prefix_enhance=off/time` 关闭昵称前缀。
> **cross_session 限制**：跨 CID 配对依赖平台 `message_id` 全局唯一，多数平台天然满足。

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

### 轮数 vs 消息数（limit_rounds 含义）

`limit_rounds` 的语义随 `llm_status_filter` 选择动态变化：

| `llm_status_filter` | limit_rounds 含义 | SQL 策略 |
|---|---|---|
| 仅 `llm_success` | **轮数**（user-assistant 一对为一轮） | 配对查询，按配对切 N 轮 |
| 含其他状态（`proactive`/`no_llm`/...） | **消息数**（单条记录） | 全量查询，按条数切 N 条 |

**例**：`limit_rounds=30`
- 只选 `llm_success` → 30 轮配对（≈60 条记录）
- 选了 `llm_success + proactive` → 30 条消息（可能是 14 user + 14 assistant + 2 proactive）

**轮数精确**：内容白名单下沉到 SQL 层，被过滤的记录不占用 limit 名额。配 30 轮就是 30 轮，不会因为过滤变少。

### 前缀增强

每条消息按 `prefix_enhance` 配置加前缀：

| Mode | user 前缀 | 配对 assistant | 单边 assistant |
|---|---|---|---|
| `off` | 无 | 无 | `[主动]` / `[未配对]` |
| `time` | `[MM/DD HH:MM:SS]` | 无 | `[MM/DD HH:MM:SS] [主动]` |
| `sender` | `SenderName:` | 无 | `[主动]`（昵称冗余） |
| `time_sender`（默认） | `[MM/DD HH:MM:SS] SenderName:` | 无 | `[MM/DD HH:MM:SS] [主动]` |

配对 assistant 不加前缀（角色即 bot 自身）。

**示例**（time_sender + full_group，注入的 contexts）：

```
[
  {"role":"user","content":"[07/09 14:30:25] Alice: 大家晚上吃什么\n\n[07/09 14:31:00] Bob: 我吃了面条"},
  {"role":"assistant","content":"你们聊"},
  {"role":"assistant","content":"[07/09 14:31:30] [主动] 提醒：14:00 开会"},
  {"role":"user","content":"[07/09 14:35:00] Alice: 那去吃火锅吧"}
]
```

### 配置项

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | false | 总开关 |
| `cross_session` | bool | false | 跨 CID |
| `full_group` | bool | false | 整群消息（仅群聊） |
| `limit_rounds` | int | 30 | 注入轮数或消息数（含义随状态过滤变化，见下） |
| `llm_status_filter` | chip | `["llm_success"]` | 状态多选 |
| `include_content_kinds` | chip | `["text"]` | 内容白名单（清空=不过滤） |
| `include_all_match` | bool | false | ALL 模式开关（默认 ANY） |
| `prefix_enhance` | select | `"time_sender"` | off / time / sender / time_sender |
| `clear_native_history` | bool | true | 每轮清空 native history |

### 内容白名单（include_content_kinds）

**只影响 takeover**，capture 照常入库。

`include_content_kinds` 是白名单：选中=需要进入上下文的 kind。空集合 = 不过滤（全部进入）。
配合 `include_all_match` 切换 ANY / ALL 两种语义：

| `include_all_match` | 语义 | 保留条件 | 例子（白名单 `["text"]`） |
|---|---|---|---|
| false（默认） | ANY | content_kind 与白名单**任一交集** | `["text"]` ✓ / `["text","image"]` ✓ / `["image"]` ✗ / `[]` ✗ |
| true | ALL | content_kind **全部**属于白名单（且非空） | `["text"]` ✓ / `["text","image"]` ✗ / `["image"]` ✗ / `[]` ✗ |

场景：
- **默认 ANY + `["text"]`**：含文本即进（纯文 ✓ / 文+图 ✓ / 纯图 ✗ / poke 通知 ✗）
- **启用 ALL + `["text"]`**：仅纯文本进（文+图也滤掉，因为 image 不在白名单）
- **ALL + 多选 `["text","image"]`**：精确限定为这两种 kind（纯文 ✓ / 纯图 ✓ / 文+图 ✓ / 文+语音 ✗）
- **清空**：不过滤，全量进入

## 已知限制

- **首条消息是非 LLM 命令时漏存**：cid 尚未创建，且无 LLM 钩子兜底
- **配对依赖 `message_id`**：平台不返时无法用 `query_rounds` 配对，只能 `query_history`
- **Forward 不拆开**：合并转发只存 1 条记录
- **/reset 与 /new 区分靠文本匹配**：AstrBot 未提供官方区分 API；若 bot 回复碰巧含 "reset" 字样会误判清库
- **与其他接管类插件冲突**：LivingMemory 等会互相覆盖，建议二选一

## 依赖

由 AstrBot 宿主环境提供：`sqlalchemy[asyncio]>=2.0.41`、`aiosqlite>=0.21.0`
