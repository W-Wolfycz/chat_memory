# ChatMemory

以 `UMO + conversation_id + user_id` 为维度的对话存档插件。所有进入 ProcessStage 的消息立即落库 SQLite，通过 `tag` 字段标识消息的成因/形态，不干预 AstrBot 自带上下文管理，仅存档 + 暴露查询接口供其他插件调用。

## 特性

- **全量捕获**：所有进入 ProcessStage 的 user 消息立即落库（命令、闲聊、纯媒体都存）
- **配对存储**：assistant 带 `pair_id`（= 对应 user 的 `message_id`），便于重组成轮
- **细粒度 tag**：7 个 tag 表达不同成因/形态，caller 按 tag 精准过滤
- **零干预**：纯旁路存档，不改上下文链路
- **命令感知**：自动识别 `/reset`（清空 CID 存档）与 `/new`（保留旧 CID 记录）
- **自动清理**：`auto_cleanup_days > 0` 时启动周期清理任务（每 24h）

## Tag 体系（核心）

每条记录的 `tag` 字段描述其**成因/形态**。tag 不强求 user/assistant 对称——`user.tag` 回答"用户发了什么"，`assistant.tag` 回答"回复怎么生成的"。

### 7 个 tag

| Tag | 视角 | 含义 |
|---|---|---|
| `non_llm` | user / assistant | 文本消息，未走 LLM（命令、插件 `set_result`、无回复等） |
| `llm_pending` | user | LLM 触发但 assistant 未成功（孤儿，LLM 失败/无回复/被拦截） |
| `llm_success` | user / assistant | LLM 路径且 assistant 成功回复（**普通文本** user/assistant 双侧同步） |
| `media_only` | user | 纯媒体消息（无文本，仅图片/语音/文件等）。**终态**：走 LLM 仍保持 |
| `no_message_id` | user / assistant | 平台未给 `message_id`，user 在库但无法配对 |
| `proactive` | assistant | 主动消息（无前置 user 事件） |
| `orphan` | assistant | user 漏存（DB 写入失败）但 bot 来了 |

### Tag 流转

```
user 消息进入
   ↓
   ├─ 普通文本 ──→ non_llm ──┐
   ├─ 纯媒体    ──→ media_only（终态，不走 LLM 时停在此）
   └─ 平台无 mid ─→ no_message_id（终态）
                            │
                            ↓ 走 LLM 路径
                            │
   non_llm ──→ llm_pending ──┐
   media_only（保持）         │
   no_message_id（保持）      │
                            ↓
                ┌─────────── LLM 成功 ────┐
                ↓                         ↓
   user: llm_success（仅 non_llm 系升级）
   user: media_only（保持）
   user: no_message_id（保持）
                ↓
   assistant: llm_success（普通文本 LLM 成功）
   assistant: llm_success（媒体 LLM 成功，与 user.media_only 配对）
   assistant: no_message_id（与 user.no_message_id 同回合，但不配对）

LLM 未触发 / 非 LLM 路径：
   user: non_llm
   assistant: non_llm（与 user 配对）

主动消息（capture_user 未跑）：
   assistant: proactive（不配对）

user 写入失败（DB 异常）：
   assistant: orphan（不配对）
```

### 配对规则

caller 用 `pair_id` 字段关联 user-assistant。能配对的 user tag 只有三种：`non_llm` / `llm_success` / `media_only`。

- `llm_pending`：定义就是没配对，孤儿
- `no_message_id`：`message_id=NULL`，SQL `NULL = NULL` 不成立，EXISTS 永远 miss
- `proactive` / `orphan`：assistant 侧专用，不配对

`media_only + llm_success` 是合法配对：用户发了纯图片，LLM 看图回文本。**user.tag 描述形态（media_only），assistant.tag 描述路径（llm_success），是正交维度**。

### 典型场景与查询示例

> 假设已通过 `_resolve_chat_memory(context)` 拿到 `cm` 实例。

```python
# 场景 1：取所有走 LLM 的对话（含纯媒体）
# 普通文本：user.llm_success + assistant.llm_success
# 纯媒体：user.media_only + assistant.llm_success
# 无 mid：user.no_message_id + assistant.no_message_id（无法配对，只能按时间序）
history = await cm.query_history(
    umo, cid, uid, limit=20,
    tag_filter=["llm_success", "media_only", "no_message_id"],
)

# 场景 2：仅普通文本 LLM 配对（最常见）
history = await cm.query_history(
    umo, cid, uid, limit=20, tag_filter="llm_success",
)

# 场景 3：按完整轮次取 LLM 配对（含媒体，不含无 mid 因配不上）
rounds = await cm.query_rounds(
    umo, cid, uid, limit_rounds=10,
    tag_filter=["llm_success", "media_only"],
)
# rounds = [[user_dict, assistant_dict], ...]

# 场景 4：判 LLM 失败（孤儿 user，触发了 LLM 但没拿到回复）
failed = await cm.query_history(
    umo, cid, uid, limit=10, tag_filter="llm_pending",
)

# 场景 5：取所有命令路径消息（如统计 /help 调用次数）
cmds = await cm.query_history(
    umo, cid, uid, limit=50, tag_filter="non_llm",
)

# 场景 6：排查"为什么这条 bot 回复没配上 user"
proactives = await cm.query_history(
    umo, cid, tag_filter="proactive", role_filter="assistant",
)
orphans = await cm.query_history(
    umo, cid, tag_filter="orphan", role_filter="assistant",
)
# proactive：主动消息（设计内）
# orphan：user 漏存（DB 写入失败，看日志排查）

# 场景 7：群聊全量历史（不按用户过滤）
group_all = await cm.query_history(umo, cid, limit=50)
```

**核心规律**：
- 想要"LLM 配对" → `tag_filter` 用 `llm_success` 单值（普通文本）或 `["llm_success", "media_only"]`（含媒体）
- 想要"非 LLM" → `tag_filter="non_llm"`
- 想要"无 mid 受平台限制" → `tag_filter="no_message_id"`
- 想要"诊断异常" → `proactive` / `orphan` / `llm_pending`

## 工作机制

```
user 消息进入 ProcessStage
        ↓
capture_user (event_message_type.ALL, priority=1000)
        ↓ 入口设 chat_memory_capture_attempted=True
        ↓ 按形态/平台判定 user.tag：
        ↓   - 无 mid              → no_message_id
        ↓   - 纯媒体（无文本）     → media_only，content 用占位 [Image]/[Voice]/...
        ↓   - 普通文本             → non_llm
        ↓ INSERT 失败 → 不写 chat_memory_captured（让 capture_bot 走 orphan）
        ↓ 成功 → 写 chat_memory_captured / user_msg_id / cid / no_mid / is_media

─── 走 LLM 路径 ───
        ↓
mark_llm_triggered (on_llm_request)
        ↓ 兜底：capture 未成功则重试（cid 此时一定存在）
        ↓ chat_memory_llm_triggered=True
        ↓ 终态 tag（media_only / no_message_id）保持不变
        ↓ 普通文本 user.tag → llm_pending

LLM 调用 → result
        ↓
capture_bot (on_decorating_result)
        ↓ 按 extras 判定 assistant.tag：
        ↓   - capture 未 attempted              → proactive（主动消息）
        ↓   - attempted 但 captured 未设         → orphan（user 漏存）
        ↓   - no_mid                            → no_message_id
        ↓   - llm_triggered + is_llm_result     → llm_success（仅普通文本 user 升级）
        ↓   - 其它                              → non_llm
        ↓ 落库 assistant 记录（llm_success/non_llm 配对 user，no_message_id/proactive/orphan 不配对）

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
    tag             TEXT NOT NULL DEFAULT 'non_llm',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

索引：`(umo, conversation_id, user_id)` / `(created_at)` / `(pair_id)`

**老库迁移**：启动时 `PRAGMA table_info` 检测列，缺则 `ALTER TABLE ADD COLUMN` 自动补。老数据 `tag='non_llm'`、`message_id` / `pair_id` 为 NULL。`query_rounds` 基于配对字段，老数据无法配对（设计如此），如需查老数据用 `query_history`。

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
| `query_history` | `umo, conversation_id, user_id=None, limit=20, tag_filter=None, role_filter=None` | `list[dict]`，按时间正序 |
| `query_rounds` | `umo, conversation_id, user_id=None, limit_rounds=10, tag_filter=None` | `list[list[dict]]`，每轮 `[user, assistant]` 两条 |

### 参数行为

- **`user_id`**：传值时按该用户过滤；为 `None` / 空串时**不按用户过滤**，返回该会话所有用户的混合记录（群聊场景）
- **`tag_filter`**：支持 str 或 list[str]，按 tag 过滤（list 走 SQL `IN`）
  - `query_history`：过滤所有记录
  - `query_rounds`：仅过滤 user 侧 tag（assistant 仍按配对字段返回，配不上的 user 不进结果）
- **`role_filter`**（仅 `query_history`）：传 `'user'` 或 `'assistant'` 时按 role 过滤

### 记录格式

`query_history` 返回的每条 dict：

```python
{
    "role": "user" | "assistant",
    "content": str,
    "user_id": str,
    "message_id": str | None,    # 平台消息 id
    "pair_id": str | None,       # 仅 assistant 有，绑定 user.message_id
    "tag": str,                  # 见 Tag 体系章节
    "created_at": str,           # 时间字符串
}
```

`query_rounds` 每轮：`[user_dict, assistant_dict]`，单 dict 格式同上。

### 调用示例

```python
# 取所有 LLM 配对（含媒体）
history = await cm.query_history(
    umo, cid, uid, limit=20,
    tag_filter=["llm_success", "media_only"],
)

# 取最近 10 轮 LLM 对话（含媒体）
rounds = await cm.query_rounds(
    umo, cid, uid, limit_rounds=10,
    tag_filter=["llm_success", "media_only"],
)

# 仅取 LLM 成功的 assistant（纯 assistant 上下文）
llm_asst = await cm.query_history(
    umo, cid, tag_filter="llm_success", role_filter="assistant",
)

# 默认行为：全量记录（含 non_llm / proactive / orphan / 孤儿 user 等）
all_history = await cm.query_history(umo, cid, uid, limit=20)

# 群聊：不传 user_id 拿整群混合历史
group_history = await cm.query_history(umo, cid, limit=20)
```

更多场景见 [Tag 体系 - 典型场景与查询示例](#典型场景与查询示例)。

## 已知限制

- **首条消息是非 LLM 命令时漏存**：用户在该 umo 的第一条消息如果是 `/help` 等命令（cid 尚未创建，且无 LLM 钩子兜底），整条对话不入库；后续消息正常
- **配对依赖平台提供 `message_id`**：平台不返 `message_id` 时，user 与 assistant 都标 `no_message_id`，无法用 `query_rounds` 配对，只能用 `query_history` 按时间序读
- **主动消息不经过 ProcessStage 时 `proactive` 不会出现**：取决于宿主如何发起主动消息。`proactive` 作为兜底 tag 保留
- **进程退出时清理任务自然消亡**：无 `on_unload` 显式取消（影响微小）

## 依赖

由 AstrBot 宿主环境提供：

- `sqlalchemy[asyncio]>=2.0.41`
- `aiosqlite>=0.21.0`
