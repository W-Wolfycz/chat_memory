# ChatMemory

独立对话记录存档插件。以 `UMO + conversation_id + user_id` 为维度，将每轮对话的文本存入本地 SQLite 数据库，不干预 AstrBot 自带的上下文管理，仅存档 + 暴露查询接口供其他插件调用。

## 特性

- **零干预**：不修改 AstrBot 的上下文传递链路，纯旁路存档
- **成对写入**：用户消息与 BOT 回复在 `on_decorating_result` 阶段成对落库，避免单边记录（BOT 回复中断时丢弃整轮）
- **命令感知**：自动识别 `/reset`（清空当前 CID 存档）与 `/new`（保留旧 CID 记录）
- **双接口**：既支持模块级 `import`，也支持 `context.get_registered_star()` 实例方法调用

## 工作机制

```
用户消息 → on_llm_request        → 提取文本，暂存到 event extras（不写库）
                ↓
BOT 回复  → on_decorating_result → 确认 LLM 结果有效后，成对写入 user + assistant
                ↓
/reset 命令 → on_decorating_result → 检测 _clean_group_context_session → 清空当前 CID 存档
/new   命令 → on_decorating_result → 新 CID，旧记录自然保留
```

### 写入时机

用户消息**不会**在收到时就写入，而是延迟到 BOT 回复成功后，与 assistant 消息一起成对写入。这确保了：

- BOT 回复中断（LLM 报错、超时、空响应）→ 什么都不存
- BOT 回复成功 → user + assistant 一起落库

## 配置

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_content_length` | int | 500 | 单条记录最大字符数，超出截断。**0 = 不限制（存储全文）** |
| `auto_cleanup_days` | int | 0 | 自动清理天数，0 = 不清理 |
| `log_config.log_with_bot_id` | bool | false | 日志前缀附加机器人 ID，启用后显示 `[ChatMemory:机器人ID]` |
| `log_config.debug_to_info` | bool | false | debug 日志提级为 info 输出 |

## 数据存储

数据库文件：`data/plugin_data/chat_memory/chat_memory.db`

表结构：

```sql
CREATE TABLE chat_memory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,   -- 'user' 或 'assistant'
    content         TEXT NOT NULL DEFAULT '',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

索引：`(umo, conversation_id, user_id)`、`(created_at)`

## 供其他插件调用

### 方式一：模块级 import

```python
from chat_memory.main import query_history, query_latest, count_records

history = await query_history(umo, conversation_id, user_id, limit=20)
# 返回 [{"role": "user", "content": "...", "created_at": "..."}, ...]
```

### 方式二：实例方法（通过 `get_registered_star`）

```python
star = context.get_registered_star("chat_memory")
if star:
    plugin = star.star_cls
    history = await plugin.query_history(umo, conversation_id, user_id, limit=20)
    count = await plugin.count_records(umo, conversation_id, user_id)
```

### API 一览

| 函数 | 参数 | 返回 |
|---|---|---|
| `query_history` | `umo, conversation_id, user_id, limit=20` | `list[dict]`，按时间正序 |
| `query_latest` | `umo, conversation_id, user_id, limit=10` | `list[dict]`，最近 N 条 |
| `count_records` | `umo, conversation_id, user_id` | `int` |

每条记录格式：`{"role": "user"|"assistant", "content": str, "created_at": str}`

## 依赖

由 AstrBot 宿主环境提供，无需单独安装：

- `sqlalchemy[asyncio]>=2.0.41`
- `aiosqlite>=0.21.0`
