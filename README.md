# ChatMemory

以 `UMO + conversation_id + user_id` 为维度的对话存档插件。所有进入 ProcessStage 的消息立即落库 SQLite，每条记录带两个独立维度的状态字段（`llm_status` + `content_kind`）。默认仅存档 + 暴露查询接口供其他插件调用；开启上下文接管后可让 CM 成为唯一上下文源。

## 特性

- **全量捕获**：所有进入 ProcessStage 的 user 消息立即落库（命令、闲聊、纯媒体、空消息都存）
- **配对存储**：assistant 带 `pair_id`（= 对应 user 的 `message_id`），便于重组成轮
- **双列状态**：`llm_status` 描述 LLM 路径，`content_kind` 描述内容形态，正交独立
- **细粒度内容分类**：组件白名单区分 `text` / `image` / `voice` / `forward` 等，`at` / `reply` 用独立字段表达
- **可选接管上下文**：默认纯旁路存档不改上下文链路；开启 `context_takeover` 后接管 LLM 的 contexts 注入
- **命令感知**：自动识别 `/reset`（清空 CID 存档）与 `/new`（保留旧 CID 记录）
- **自动清理**：`auto_cleanup_days > 0` 时启动周期清理任务（每 24h）

## 双列状态体系（核心）

每条记录带两个**独立维度**的状态：

- `llm_status`：LLM 配对状态（单值）
- `content_kind`：内容形态（JSON 数组，可多值）

### llm_status 取值

| 值 | 含义 | 触发场景 |
|---|---|---|
| `""`（空） | 默认，未走 LLM | 命令、插件 `set_result`、无回复、纯媒体 |
| `llm_pending` | LLM 触发但 assistant 未成功 | LLM 失败、超时、被拦截 |
| `llm_success` | LLM 路径且 assistant 成功回复 | 普通 LLM 对话（user 与 assistant 同步） |
| `proactive` | 主动消息（assistant 单边） | cron 触发、插件主动推送、无前置 user 事件 |
| `orphan` | user 漏存（DB 写入失败）但 assistant 来了 | DB 异常 |

### content_kind 取值

| 值 | 触发条件 |
|---|---|
| `text` | Plain 组件有非空文本 |
| `image` | Image 组件 |
| `video` | Video 组件 |
| `voice` | Record 组件（语音） |
| `file` | File 组件 |
| `face` | Face 组件（表情） |
| `forward` | Forward 组件（合并转发，不拆） |
| `system_event` | `MessageType.OTHER_MESSAGE`（poke / 加好友请求 / 通知等） |
| `[]` 空数组 | empty（如纯 @BOT 无文字、纯 Reply 无文字） |

> **at / reply 不入 content_kind**：用独立字段 `at_id` / `reply_id` 表达。如纯 @BOT 无文字的消息：`content_kind=[]` + `at_id=<bot_id>`。

### 状态流转

```
user 消息进入 ProcessStage
   ↓
   capture_user (priority=1000)
   ↓ 入口过滤：cron 平台跳过（assistant 走 proactive）
   ↓ 组件分类 → content_kind + at_id/reply_id/forward_id
   ↓ 落库：llm_status = ""
   ↓
   ├─ 走 LLM 路径 ──→ mark_llm_triggered
   │                   ↓ user.llm_status: "" → "llm_pending"
   │                   ↓ （平台无 mid 时跳过：无法用 message_id 定位）
   │                   ↓
   │                   LLM 调用 → result
   │                   ↓
   │                   capture_bot
   │                   ↓ assistant.llm_status = "llm_success" + pair_id=user.message_id
   │                   ↓ user.llm_status: "llm_pending" → "llm_success"
   │
   ├─ 命令 / 插件 set_result ──→ capture_bot
   │                              ↓ assistant.llm_status = "" + pair_id=user.message_id
   │
   └─ LLM 失败 ──→ capture_bot 不跑 / chain 空
                    ↓ user 保持 "llm_pending"（孤儿）

主动消息（capture_user 没跑）：
   assistant.llm_status = "proactive"（pair_id=None）

user DB 写入失败：
   assistant.llm_status = "orphan"（pair_id=None）
```

### 配对规则

caller 用 `pair_id` 关联 user-assistant。能配对的 assistant：
- `llm_status=''` / `llm_success`：`pair_id = user.message_id`（要求 user 有 mid）
- 平台无 mid（`message_id=NULL`）：`pair_id=NULL`，无法用 `query_rounds` 配对
- `proactive` / `orphan`：不配对

正交维度示例：用户发图（`content_kind=['image']`）+ LLM 看图回文本（`content_kind=['text']`）—— 这是一条合法配对，`llm_status` 都是 `llm_success`，`content_kind` 各自描述形态。

### 典型场景与查询示例

> 假设已通过 `_resolve_chat_memory(context)` 拿到 `cm` 实例。

```python
# 场景 1：取所有走 LLM 的对话（含媒体）
history = await cm.query_history(
    umo, cid, uid, limit=20,
    llm_status="llm_success",
)

# 场景 2：按完整轮次取 LLM 配对
rounds = await cm.query_rounds(
    umo, cid, uid, limit_rounds=10,
    llm_status="llm_success",
)
# rounds = [[user_dict, assistant_dict], ...]

# 场景 3：判 LLM 失败（孤儿 user，触发了 LLM 但没拿到回复）
failed = await cm.query_history(
    umo, cid, uid, limit=10,
    llm_status="llm_pending",
)

# 场景 4：取所有命令路径消息（llm_status='' 即非 LLM）
# 注意：空串过滤用 list 形式传 [""]
cmds = await cm.query_history(
    umo, cid, uid, limit=50,
    llm_status=[""],
)

# 场景 5：取所有含图片的消息（user 视角）
images = await cm.query_history(
    umo, cid, uid, limit=20,
    content_kind="image",
)

# 场景 6：取所有图文混合消息
mixed = await cm.query_history(
    umo, cid, uid, limit=20,
    content_kind=["text", "image"],  # ANY 语义：含 text 或 image 任一
)
# 注意：要 AND 语义（同时含 text 和 image）需 caller 侧二次过滤

# 场景 7：排查"为什么这条 bot 回复没配上 user"
proactives = await cm.query_history(
    umo, cid, llm_status="proactive", role_filter="assistant",
)
orphans = await cm.query_history(
    umo, cid, llm_status="orphan", role_filter="assistant",
)
# proactive：主动消息（设计内，含 cron）
# orphan：user 漏存（DB 写入失败，看日志排查）

# 场景 8：群聊全量历史（不按用户过滤）
group_all = await cm.query_history(umo, cid, limit=50)
```

**核心规律**：
- 想要"LLM 配对" → `llm_status="llm_success"`
- 想要"非 LLM" → `llm_status=[""]`（空串过滤用 list 包裹）
- 想要"按内容筛" → `content_kind="image"` / `"voice"` / `"forward"` 等
- 想要"诊断异常" → `llm_status` 用 `llm_pending` / `proactive` / `orphan`

## 工作机制

```
user 消息进入 ProcessStage
        ↓
capture_user (event_message_type.ALL, priority=1000)
        ↓ 入口设 chat_memory_capture_attempted=True
        ↓ 过滤：umo/user_id 空、BOT 自身、cron 平台 → 跳过（assistant 走 proactive）
        ↓ 取 cid（cid 不存在 → 跳过，首条非 LLM 命令漏存）
        ↓ 组件分类：Plain/Image/Video/Record/File/Face/Forward 入 content_kind
        ↓           At → at_id；Reply → reply_id；Forward → forward_id
        ↓           MessageType.OTHER_MESSAGE → content_kind=['system_event']
        ↓ 落库：llm_status=""，content 按文本/占位/空串
        ↓ INSERT 失败 → 不写 chat_memory_captured（让 capture_bot 走 orphan）

─── 走 LLM 路径 ───
        ↓
mark_llm_triggered (on_llm_request)
        ↓ 兜底：capture 未成功则重试（cid 此时一定存在）
        ↓ chat_memory_llm_triggered=True
        ↓ 平台无 mid → 跳过升级（无法用 message_id 定位）
        ↓ 否则：user.llm_status: "" → "llm_pending"

LLM 调用 → result
        ↓
capture_bot (on_decorating_result)
        ↓ 按 extras 判定 assistant.llm_status：
        ↓   - capture 未 attempted              → proactive（主动消息）
        ↓   - attempted 但 captured 未设         → orphan（user 漏存）
        ↓   - llm_triggered + is_llm_result     → llm_success + 升级 user
        ↓   - 其它                              → ""
        ↓ pair_id = user.message_id（无 mid 时 NULL）
        ↓ assistant.content_kind = ['text']

─── /reset / /new ───
        ↓
capture_bot 检测 _clean_group_context_session extra
        ↓ /reset：清空当前 CID 所有存档记录
        ↓ /new：新 CID 已创建，旧记录自然保留
```

## 配置

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_content_length` | int | 500 | 单条记录最大字符数，超出截断。**0 = 不限制** |
| `auto_cleanup_days` | int | 0 | 自动清理天数，**0 = 不清理**；>0 启动周期任务（24h 一次） |
| `log_config.log_with_bot_id` | bool | false | 日志前缀附加机器人 ID |
| `log_config.debug_to_info` | bool | false | debug 日志提级为 info 输出 |

## 数据存储

数据库：`data/plugin_data/chat_memory/chat_memory.db`

```sql
CREATE TABLE chat_memory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,        -- 'user' 或 'assistant'
    content         TEXT NOT NULL DEFAULT '',
    message_id      TEXT,                 -- 平台消息 id（NULL 表示平台未给）
    pair_id         TEXT,                 -- 仅 assistant：绑定的 user.message_id
    llm_status      TEXT NOT NULL DEFAULT '',     -- LLM 配对状态
    content_kind    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组：内容形态
    platform_id     TEXT,                 -- umo 拆分：平台实例 ID（platform_meta.id）
    platform_name   TEXT,                 -- 平台类型（platform_meta.name，如 aiocqhttp / lark）
    message_type    TEXT,                 -- umo 拆分：FriendMessage / GroupMessage / OtherMessage
    session_id      TEXT,                 -- umo 拆分：会话标识（QQ 号 / 群号）
    self_id         TEXT,                 -- 机器人自身 ID（多 bot 同群时隔离）
    group_id        TEXT,                 -- 群号（仅群聊，私聊为 NULL）
    sender_nickname TEXT,                 -- 发送者昵称（NULL 表示平台未给）
    raw_timestamp   INTEGER,              -- 消息原始时间戳（Unix 秒，来自 message_obj.timestamp）
    at_id           TEXT,                 -- At 组件目标 ID（仅 at 时存）
    reply_id        TEXT,                 -- Reply 组件引用的消息 ID
    forward_id      TEXT,                 -- Forward 组件的平台 ID（备查）
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

索引：`(umo, conversation_id, user_id)` / `(created_at)` / `(pair_id)` / `(platform_id, group_id, created_at)` / `(llm_status)`

> **platform_id vs platform_name**：自 AstrBot v4.0 起 umo 第一段是 `platform_meta.id`（**实例 ID**，多 aiocqhttp 实例时每个不同），不是类型。`platform_name` 才是类型（`aiocqhttp`）。
>
> **raw_timestamp vs created_at**：`raw_timestamp` 是消息到达 AstrBot 的时间（`message_obj.timestamp`），`created_at` 是记录落库时间。

**老库处理**：v2.3 不做兼容迁移。启动时 PRAGMA 检测到老 schema（缺 `llm_status` 列）直接 `DROP TABLE` + `CREATE TABLE`，老数据丢失。

## 供其他插件调用

> ⚠️ **不要直接 `from chat_memory.main import ...`**：AstrBot 的插件加载机制不保证模块级 import 可用。统一用 `get_registered_star`。

### 实例解析

```python
def _resolve_chat_memory(context):
    """定位 chat_memory 插件实例。"""
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

### API 一览

| 函数 | 参数 | 返回 |
|---|---|---|
| `query_history` | `umo, conversation_id, user_id=None, limit=20, llm_status=None, content_kind=None, role_filter=None` | `list[dict]`，按时间正序 |
| `query_rounds` | `umo, conversation_id, user_id=None, limit_rounds=10, llm_status=None, content_kind=None` | `list[list[dict]]`，每轮 `[user, assistant]` 两条 |

### 参数行为

- **`user_id`**：传值时按该用户过滤；为 `None` / 空串时**不按用户过滤**，返回该会话所有用户的混合记录（群聊场景）
- **`llm_status`**：支持 str 或 list[str]，按 LLM 状态过滤（list 走 SQL `IN`）
  - 空串 `""` 等价于"未走 LLM"，过滤时传 `llm_status=[""]`（list 形式，避免歧义）
  - `query_rounds` 仅过滤 user 侧（assistant 仍按配对字段返回）
- **`content_kind`**：支持 str 或 list[str]，**ANY 语义**——返回 `content_kind` JSON 数组中任一包含这些值的记录（用 `json_each` + `IN`）
- **`role_filter`**（仅 `query_history`）：传 `'user'` 或 `'assistant'` 时按 role 过滤

### 记录格式

`query_history` 返回的每条 dict：

```python
{
    "role": "user" | "assistant",
    "content": str,
    "user_id": str,
    "message_id": str | None,       # 平台消息 id
    "pair_id": str | None,          # 仅 assistant 有，绑定 user.message_id
    "llm_status": str,              # '' / 'llm_pending' / 'llm_success' / 'proactive' / 'orphan'
    "content_kind": list[str],      # ['text'] / ['image'] / ['text','image'] / [] / ['system_event'] / ...
    "platform_id": str | None,      # 平台实例 ID（platform_meta.id）
    "platform_name": str | None,    # 平台类型（aiocqhttp / lark / discord / ...）
    "message_type": str | None,     # FriendMessage / GroupMessage / OtherMessage
    "session_id": str | None,       # 会话标识（QQ 号 / 群号）
    "self_id": str | None,          # 机器人自身 ID
    "group_id": str | None,         # 群号（私聊为 None）
    "sender_nickname": str | None,  # 发送者昵称
    "raw_timestamp": int | None,    # 消息原始 Unix 时间戳（秒）
    "at_id": str | None,            # At 目标 ID（仅 at 时有）
    "reply_id": str | None,         # Reply 引用消息 ID（仅 reply 时有）
    "forward_id": str | None,       # Forward 平台 ID（仅 forward 时有）
    "created_at": str,              # 落库时间字符串
}
```

`query_rounds` 每轮：`[user_dict, assistant_dict]`，单 dict 格式同上。

### 调用示例

```python
# 取所有 LLM 配对（含媒体，纯文本或纯图或图文混合）
history = await cm.query_history(
    umo, cid, uid, limit=20,
    llm_status="llm_success",
)

# 取最近 10 轮 LLM 对话
rounds = await cm.query_rounds(
    umo, cid, uid, limit_rounds=10,
    llm_status="llm_success",
)

# 仅取 LLM 成功的 assistant（纯 assistant 上下文）
llm_asst = await cm.query_history(
    umo, cid, llm_status="llm_success", role_filter="assistant",
)

# 默认行为：全量记录
all_history = await cm.query_history(umo, cid, uid, limit=20)

# 群聊：不传 user_id 拿整群混合历史
group_history = await cm.query_history(umo, cid, limit=20)
```

更多场景见 [典型场景与查询示例](#典型场景与查询示例)。

## 已知限制

- **首条消息是非 LLM 命令时漏存**：用户在该 umo 的第一条消息如果是 `/help` 等命令（cid 尚未创建，且无 LLM 钩子兜底），整条对话不入库；后续消息正常
- **配对依赖平台提供 `message_id`**：平台不返 `message_id` 时，user 与 assistant 的 `pair_id` 都为 NULL，无法用 `query_rounds` 配对，只能用 `query_history` 按时间序读
- **主动消息不经过 ProcessStage 时 `proactive` 不会出现**：取决于宿主如何发起主动消息。`proactive` 作为兜底状态保留
- **Forward 不拆开**：用户发的合并转发只存 1 条记录（`content_kind=['forward']`），不调 OneBot API 拉取内部 chain
- **进程退出时清理任务自然消亡**：无 `on_unload` 显式取消（影响微小）

## 上下文接管

让 ChatMemory 成为唯一上下文源：每轮 LLM 请求时，用 CM 数据覆盖 `req.contexts`，并清空 native `conversation.history` 防累积。

### 工作机制

```
用户消息 → CM 落库 user
        ↓
LLM 请求触发
        ↓
CM 接管（最后执行，覆盖其他插件对 contexts 的修改）：
        ├─ 查 CM 历史（按 cross_session × full_group 组合 + 状态过滤）
        ├─ 规整：过滤纯媒体 → 加前缀 → 合并连发 → 丢无效头尾
        └─ 写入 req.contexts（每条标记不回写 native）
        ↓
LLM 看到 [system + CM 接管的历史 + 当前 user 消息]
```

**接管顺序**：CM 在所有 `on_llm_request` 钩子中最后执行，覆盖其他插件对 contexts 的修改（除非对方 priority 更低）。

**不污染 native**：每条注入的 dict 标记 `_no_save`，AstrBot 不会把它写回 native history。当前轮的 user/assistant 仍会进 native（agent runner 内部行为），所以默认每轮清空 native。

### 接管范围（两个独立 checkbox）

`cross_session` 和 `full_group` 是两个独立维度，可任意组合：

| `cross_session` | `full_group` | 数据范围 | 适用场景 |
|---|---|---|---|
| F | F | 同 CID 同用户 | 默认。与 native 行为等价但走 CM 数据源 |
| T | F | 跨 CID 同用户 | `/new` 或 `/reset` 后仍记得上一个会话内容 |
| F | T | 同 CID 全群（含其他用户） | 群聊让 LLM 看到所有发言者 |
| T | T | 跨 CID 全群 | 跨会话 + 整群聚合 |

> **full_group 仅群聊生效**：私聊自动降级为本用户。
> **隐私提示**：full_group 开启后，群内其他人的发言会注入 LLM，含昵称（可通过 `prefix_enhance=off/time` 关闭昵称前缀）。

### 状态过滤（llm_status_filter）

选哪些状态的消息进入上下文。各状态含义见 [双列状态体系](#llm_status-取值)。

| 状态 | 出现在 | 含义 |
|---|---|---|
| `llm_success` | user + assistant | LLM 成功配对（默认） |
| `llm_pending` | user | 触发 LLM 但失败（孤儿 user） |
| `no_llm` | user + assistant | 非 LLM 路径（命令、`set_result` 等） |
| `proactive` | assistant | 主动消息（cron 推送、插件主动） |
| `orphan` | assistant | user 漏存但 assistant 来了 |

开启 `proactive`/`orphan` 后，这些单边 assistant 会带 `[主动]`/`[未配对]` 前缀注入，让 LLM 知道这是 bot 单方面说的（没有前置 user）。

### 前缀增强（prefix_enhance）

每条消息按配置加前缀（合并连发时各条独立保留）：

| Mode | user 前缀 | 配对 assistant | 单边 assistant (proactive/orphan) |
|---|---|---|---|
| `off` | 无前缀 | 无前缀 | `[主动]` / `[未配对]` |
| `time` | `[MM/DD HH:MM:SS] content` | 无前缀 | `[MM/DD HH:MM:SS] [主动] content` |
| `sender` | `SenderName: content` | 无前缀 | `[主动] content`（昵称冗余） |
| `time_sender`（默认） | `[MM/DD HH:MM:SS] SenderName: content` | 无前缀 | `[MM/DD HH:MM:SS] [主动] content` |

- **配对 assistant 不加前缀**：角色即 bot 自身，前缀冗余
- **单边 assistant 加 `[主动]`/`[未配对]`**：让 LLM 区分 bot 单方面发言 vs 配对回复

**示例**（time_sender + full_group，注入的 contexts）：

```
[
  {"role":"user","content":"[07/09 14:30:25] Alice: 大家晚上吃什么\n\n[07/09 14:31:00] Bob: 我吃了面条"},
  {"role":"assistant","content":"你们聊"},
  {"role":"assistant","content":"[07/09 14:31:30] [主动] 提醒：14:00 开会"},
  {"role":"user","content":"[07/09 14:35:00] Alice: 那去吃火锅吧"}
]
```

合并的连续 user 用空行分隔；连续单边 assistant 同样合并。

### 自动行为（不可配置）

- **过滤纯媒体**：`content_kind` 全是媒体才丢；图文混合保留文本部分
- **合并连续同 role**：避免部分 LLM API 报错（不允许连续 user）
- **单边与配对 assistant 不合并**：语义不同，分开保留
- **丢尾部单边 assistant**：OpenAI 格式要求 messages 末尾不能是单边 assistant

### 配置项

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | false | 总开关 |
| `cross_session` | bool | false | 跨群会话开关（跨 CID） |
| `full_group` | bool | false | 整群消息开关（含其他用户，仅群聊） |
| `limit_rounds` | int | 30 | 注入最近 N 轮（≈ 2N 条） |
| `llm_status_filter` | chip | ["llm_success"] | 多选 LLM 状态过滤 |
| `prefix_enhance` | select | "time_sender" | off / time / sender / time_sender |
| `clear_native_history` | bool | true | 每轮清空 native history |



## 依赖

由 AstrBot 宿主环境提供：

- `sqlalchemy[asyncio]>=2.0.41`
- `aiosqlite>=0.21.0`
