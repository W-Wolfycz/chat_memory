# Changelog

## 2.3.1 — 2026-07-09

### 上下文接管（context_takeover）

让 ChatMemory 可选成为唯一上下文源。开启后，每轮 LLM 请求时用 CM 数据覆盖 `req.contexts`，并清空 native `conversation.history` 防累积。

**功能**

- **接管钩子**：在所有 `on_llm_request` 钩子中最后执行；注入内容标 `_no_save` 不回写 native
- **接管范围**：`cross_session`（跨 CID）+ `full_group`（跨用户）两个独立维度
- **状态过滤**：chip 多选 `llm_status_filter`，决定哪些状态的消息进入上下文
- **前缀增强**：4 种模式（off/time/sender/time_sender），给 user 加时间戳/发送者前缀
- **单边 assistant 注入**：开启 proactive/orphan 后，bot 主动消息带 `[主动]`/`[未配对]` 前缀注入

**自动行为**

- 过滤纯媒体（图文混合保留文本）；合并连续同 role；丢头部非 user 与尾部单边 assistant

**修复（第三方评审）**

- cron 主动消息误标 orphan
- solo assistant 跨用户泄漏（standard 模式现在按当前用户过滤）
- `limit_rounds` 钳到 `[1, 100]`
- 新增 `terminate()` 钩子：卸载时取消 task + 释放 DB 连接池
- ORDER BY 加 id 次级，避免同毫秒排序抖动

**稳定性**

- SQLite WAL 模式 + `synchronous=NORMAL`，避免群聊高频写入时 `database is locked`
- fire-and-forget 任务引用保活，避免被 GC

**边缘变化**

- `user_id == self_id`（BOT 自身消息）原走 orphan，现走 proactive

## 2.3.0 — 2026-07-08

### tag 拆双列 + 内容白名单分类

v2.x 的单 `tag` 列混合三个独立维度（LLM 路径、消息形态、配对状态），扩展性差。v2.3 拆为：

- `llm_status`：LLM 配对状态（`""` / `llm_pending` / `llm_success` / `proactive` / `orphan`）
- `content_kind`：内容形态 JSON 数组（`text` / `image` / `voice` / `forward` / `system_event` 等）

同时引入组件白名单分类，修正 v2.x 对纯 @BOT、纯 Reply、Poke 通知的错误归类。

**破坏性变更**：不做迁移，启动时检测老 schema 直接 DROP 重建，老数据丢失。`tag_filter` 参数删除，调用方改用 `llm_status` + `content_kind`。

## 2.1.0 — 2026-07-07

补全 8 个审计字段：`platform_id` / `platform_name` / `message_type` / `session_id` / `self_id` / `group_id` / `sender_nickname` / `raw_timestamp`。纯增字段，老库自动 ALTER 补列。

## 2.0.0 — 2026-07-03

### 全量捕获 + 7-tag 配对存储

v1.x 仅捕获 LLM 路径对话。v2.0 改为所有进入 ProcessStage 的 user 消息立即落库，并通过 7 个 tag 标识成因/形态。新增 `message_id` / `pair_id` 字段支持配对查询。

**破坏性变更**：`query_history` 默认返回范围扩大到全量消息。要保持 v1.x 语义传 `tag_filter="llm_success"`。

## 1.1.0 — 2026-06-26

群聊场景：`user_id` 参数改为可选，为空时返回该会话所有用户混合记录。返回 dict 新增 `user_id` 字段。

## 1.0.0 — 2026-06-13

初始版本。以 `UMO + conversation_id + user_id` 为维度的异步 SQLite 对话存档。
