# Changelog

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
