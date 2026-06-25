# Changelog

## 1.1.0 — 2026-06-26

### 新增

- **群聊场景：按会话维度查询**。`query_history` / `query_latest` / `count_records` 的 `user_id` 参数改为可选（默认 `None`），为空时返回该会话下**所有用户**的混合记录（不再按用户过滤）。群聊场景下整群共享一个 `(umo, conversation_id)`，便于插件拉取整个群的对话上下文。
- 返回 dict 新增 `user_id` 字段（始终存在），便于调用方区分发言人。

### 变更

- **README 调整推荐调用方式**：移除模块级 `from chat_memory.main import ...` 推荐路径。AstrBot 的插件加载机制不保证模块级 import 可用（实测多数环境下失败），统一推荐 `context.get_registered_star("chat_memory")` + `sys.modules` fallback 的稳健解析方式。

### 兼容性

- **纯增字段、向后兼容**，可平滑升级：
  - 老调用方按位置或关键字传 `user_id` 仍正常工作，行为完全不变（仍按用户过滤）
  - 返回 dict 只多了 `user_id` 字段，老代码用 `record.get("role")` / `record.get("content")` / `record.get("created_at")` 完全不受影响

### 内部

- `query_latest` / `query` 在 `user_id` 为空时跳过 `WHERE user_id = ?` 条件，复用同一索引前缀 `(umo, conversation_id)`
- `count` 在 `user_id` 为空时直接转调既有的 `count_conversation`，避免重复 SQL

## 1.0.0 — 2026-06-13

### 新增

- 以 `UMO + conversation_id + user_id` 为维度的异步 SQLite 存档
- `on_llm_request` 钩子提取用户消息文本，暂存 event extras
- `on_decorating_result` 钩子成对写入 user + assistant 消息
- 自动识别 `/reset`（清空当前 CID 存档）与 `/new`（保留旧记录）
- 双调用接口：模块级函数 + 实例方法
- 日志配置组 `log_config`：
  - `log_with_bot_id`：日志前缀附加机器人实例 ID
  - `debug_to_info`：debug 日志提级为 info 输出
- `max_content_length` 支持 `0 = 不限制`（存储全文）

### 设计决策

- 用户消息延迟到 BOT 回复成功后写入，避免单边记录
- 不修改 AstrBot 上下文传递链路，纯旁路存档
- 通过 `_clean_group_context_session` extra 检测内置命令，再以响应文本区分 `/reset` 与 `/new`
- 数据目录使用 `data/plugin_data/chat_memory/`，与 AstrBot 插件数据规范一致
- 日志配置与 emotion_favour 插件结构对齐，便于多插件统一管理
