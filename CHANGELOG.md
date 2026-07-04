# Changelog

## 2.0.0 — 2026-07-03

### 重大变更：从"仅 LLM 触发存档"升级为"全量捕获 + 7-tag 配对存储"

v1.x 仅在 `on_llm_request` + `on_decorating_result` 双钩子下捕获 LLM 路径对话，命令/失败/无回复的消息全部漏存。v2.0 改为**所有进入 ProcessStage 的消息都立即落库**，并通过 7 个细粒度 `tag` 标识消息的成因/形态。

### 新增

- **全量消息捕获**：新增 `@filter.event_message_type(EventMessageType.ALL, priority=1000)` 钩子，所有进入 ProcessStage 的 user 消息（含命令、唤醒词未命中、闲聊、纯媒体）都立即落库
- **三字段 schema 扩展**（自动迁移老库）：
  - `message_id TEXT` —— 平台消息 id（NULL 表示平台未给）
  - `pair_id TEXT` —— 仅 assistant 使用，值 = 对应 user 的 message_id
  - `tag TEXT NOT NULL DEFAULT 'non_llm'` —— 消息成因/形态标识
- **7 个 tag 体系**（user.tag 描述"用户发了什么"，assistant.tag 描述"回复怎么生成"，正交）：
  - `non_llm` —— 文本消息，未走 LLM（命令、`set_result`、无回复等）
  - `llm_pending` —— LLM 触发但 assistant 未成功（孤儿，仅 user 侧出现）
  - `llm_success` —— LLM 路径且成功回复。普通文本 user/assistant 双侧同步
  - `media_only` —— 纯媒体消息（无文本，仅图片/语音/文件等）。**终态 tag**：走 LLM 仍保持，与 `assistant.llm_success` 配对
  - `no_message_id` —— 平台未给 `message_id`。user/assistant 双侧同步，无法配对
  - `proactive` —— 主动消息（无前置 user 事件）
  - `orphan` —— user 漏存（DB 写入失败）但 bot 来了
- **`tag` 终态规则**：`media_only` 与 `no_message_id` 不参与 `llm_pending → llm_success` 流转，`mark_llm_triggered` 与 `capture_bot` 检测到这两个 tag 时跳过升级
- **纯媒体消息存档**：`_extract_text` 返回空时，检测 `message_chain` 是否含非 Plain 组件。是 → 用 `[Image]` / `[Voice]` / `[File]` 等占位 content 落库，`tag=media_only`
- **`on_llm_request` 兜底机制**：当 capture_user 在 priority=1000 跑时 cid 还没创建（首条消息），LLM 调用钩子触发时 cid 已存在，重新尝试捕获
- **`query_rounds` 新接口**：按轮次返回 user-assistant 配对，每轮保证 `[user, assistant]` 两条。用 `EXISTS` 子查询过滤单边 user，`limit_rounds=N` ⇒ `2N` 条记录
- **`query_history` 加 `tag_filter` / `role_filter` 参数**：
  - `tag_filter` 支持 str 或 list[str]（list 走 SQL `IN`）
  - `role_filter` 按 role 过滤（`'user'` / `'assistant'`）
- **`query_rounds` 的 `tag_filter`**：仅过滤 user 侧 tag（assistant 仍按配对字段返回）
- **`auto_cleanup_days` 真正实现**：v1.x 读了配置但未实现，v2.0 启动周期清理任务（每 24h 跑一次 `delete_old`）
- **新索引**：`(pair_id)` 加速配对查询

### 修复

- **修复 user 漏存的语义歧义**：`_safe_insert` 改为返回 bool；user INSERT 真失败时 `_capture_user_internal` **不写** `chat_memory_captured` extras，让 `capture_bot` 走 `orphan` 分支正确标记（v1.x 行为是：失败仍写 extras → assistant 误标 `llm_success` 但 user 不在库）
- **区分"主动消息"与"漏存"**：`_capture_user_internal` 入口处先 `set_extra("chat_memory_capture_attempted", True)`，`capture_bot` 据此区分：
  - `attempted=False` → `proactive`（无前置 user 事件）
  - `attempted=True` + `captured=False` → `orphan`（DB 写入失败）
- **修复 v1.x fire-and-forget 竞态**：`mark_llm_triggered` 的 `UPDATE tag` 找不到 user 行——capture_user 改用 `await self._safe_insert` 保证 user 行已落库
- **LLM 成功时仅升级普通文本 user**：`media_only` user 走 LLM 成功后，assistant 标 `llm_success` 但 user 保持 `media_only`（不升级），保留形态信息

### 变更

- **API 重构**：仅保留实例方法入口（`query_history` / `query_rounds`），删除模块级 API（`from chat_memory.main import ...`）和实例 `query_latest` / `count_records`。CHANGELOG 1.1.0 已明示模块级 import 不可靠，v2.0 彻底清理
- **写入策略改写**：
  - user 消息**立即落库**（v1.x 延迟到 BOT 回复成功后成对写入）
  - 不再保证 user/assistant 事务原子性，改用配对字段在 caller 侧重组
- **schema 迁移**：启动时 `PRAGMA table_info` 检测列，缺则 `ALTER TABLE ADD COLUMN`，老库原地扩展无需重建
- **`proactive` 范围收窄**：从"不配对的 assistant（混合主动消息与漏存）"改为"主动消息"。漏存场景独立为 `orphan` tag

### 删除

- 实例方法 `query_latest`、`count_records`（外部无调用方）
- 模块级 `query_history` / `query_latest` / `count_records` / `_db` / `_max_len`
- storage 层 `query()`（带 role/before/after 过滤的通用版，无人调用）、`count_conversation`（合并进 `count`）、`delete_user`（无人调用）、`count`（无人调用）

### 兼容性

#### ⚠️ 破坏性变更：`query_history` 默认返回范围变了

v1.x 因钩子机制限制，`query_history` 仅返回**走 LLM 路径的 user + 成功的 assistant** 配对记录。v2.0 改为全量捕获后，同样的调用会多出非 LLM 消息（命令、命令回复、孤儿 user、主动消息等）。**保持 v1.x 语义的最小修**：传 `tag_filter="llm_success"`（user 与 assistant 双侧都已同步为该 tag，单值即可）。

#### 字段层面：纯增，向后兼容

返回 dict 在原 `role` / `content` / `user_id` / `created_at` 基础上**新增** `message_id` / `pair_id` / `tag` 三字段，老代码读旧字段不受影响。

#### 老库自动迁移

v1.x 数据自动补列，老记录 `tag='non_llm'`、`message_id` / `pair_id` 为 NULL。

#### 老数据查 rounds 会返回空

`query_rounds` 用 EXISTS 配对，老数据无 `message_id` 永远配不上 assistant。这是预期行为（老数据本就没有配对语义），如需查老数据请用 `query_history`。

### 已知限制

- 用户在该 umo 的**第一条消息是非 LLM 命令**时漏存（cid 尚未创建，且无 LLM 钩子兜底）
- 主动消息不经过 ProcessStage 时 `proactive` 不会出现（取决于宿主如何发起）。`proactive` 作为兜底 tag 保留
- 进程退出时 `_cleanup_task` 自然消亡，无 `on_unload` 显式取消

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
- 日志配置与其他插件结构对齐，便于多插件统一管理
