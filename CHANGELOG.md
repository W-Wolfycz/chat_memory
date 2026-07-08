# Changelog

## 2.3.1 — 2026-07-09

### 新增：上下文接管（context_takeover）

让 ChatMemory 成为唯一上下文源。每轮 LLM 请求时覆盖 `req.contexts` 注入 CM 数据，并清空 native `conversation.history` 防累积。

#### 功能

- **接管钩子**：在所有 `on_llm_request` 钩子中最后执行，覆盖其他插件对 contexts 的修改；注入的每条 dict 标 `_no_save`，不会被写回 native history
- **接管范围**：`cross_session`（跨 CID）+ `full_group`（跨用户）两个独立 checkbox，可任意组合；私聊下 `full_group` 自动降级为本用户
- **状态过滤**：chip 多选 `llm_status_filter`，决定哪些状态的消息进入上下文。`no_llm` 是 UI 占位符（DB 实际值是空串）。开启 `proactive`/`orphan` 后会补查单边 assistant 注入
- **前缀增强**：4 种模式（off/time/sender/time_sender）。user 加 `[MM/DD HH:MM:SS] Sender:` 前缀；单边 assistant 加 `[主动]`/`[未配对]` 标识让 LLM 区分 bot 单方面发言
- **新查询接口**：`query_rounds_umo`（跨 CID 配对查询）、`query_solo_assistants`（查单边 assistant）
- **新索引** `(umo, role, created_at)`：加速跨 CID 查询

#### 自动行为（不可配置）

- **过滤纯媒体**：`content_kind` 全是媒体才丢；图文混合保留文本部分
- **合并连续同 role**：避免部分 LLM API 报错；单边与配对 assistant 不合并（语义不同）
- **丢头部非 user / 丢尾部单边 assistant**：保证 messages 格式合法

#### 配置项

`context_takeover` 节共 7 项：`enable` / `cross_session` / `full_group` / `limit_rounds` / `llm_status_filter` / `prefix_enhance` / `clear_native_history`。详见 README。

#### 已知限制

- **首条消息 cid 未就绪**：跳过接管，native 兜底，下一轮生效
- **不接管 tool_call 中间消息**：`astr_event.send()` 绕过 pipeline
- **不接管压缩摘要**：AstrBot 内置 summary 机制无法拦截；靠 `clear_native_history=true` 在下一轮清空
- **与其他接管类插件冲突**：LivingMemory 等会互相覆盖，建议二选一
- **整群消息的隐私**：群内其他人发言会注入 LLM，含昵称

## 2.3.0 — 2026-07-08

### 重大变更：tag 拆双列（llm_status + content_kind）+ 内容白名单分类

v2.x 的单 `tag` 列混合了三个独立维度（LLM 路径、消息形态、配对状态），扩展性差。v2.3.0 拆为两个独立列：`llm_status` 描述 LLM 路径，`content_kind` 描述内容形态（JSON 数组，可多值）。同时引入组件白名单分类，修正 v2.x 对纯 @ / Reply / Poke 的错误归类。

### 新增

- **`llm_status TEXT NOT NULL DEFAULT ''`**：LLM 配对状态（单值）
  - `""`（空）—— 默认，未走 LLM（命令、`set_result`、无回复、纯媒体等）
  - `llm_pending` —— LLM 触发但 assistant 未成功
  - `llm_success` —— LLM 路径且 assistant 成功回复
  - `proactive` —— 主动消息（assistant 单边，含 cron）
  - `orphan` —— user 漏存（DB 写入失败）
- **`content_kind TEXT NOT NULL DEFAULT '[]'`**：内容形态（JSON 数组，可多值）
  - 取值：`text` / `image` / `video` / `voice` / `file` / `face` / `forward` / `system_event`
  - `[]` 空数组表示 empty（如纯 @ 无文字、纯 Reply 无文字）
- **`at_id TEXT`**：At 组件目标 ID，仅 at 时存
- **`reply_id TEXT`**：Reply 组件引用消息 ID，仅 reply 时存
- **`forward_id TEXT`**：Forward 组件平台 ID（备查，不调 API 拆开）
- **`_classify_content(event)`**：用组件白名单分类，返回 `(content_kind, at_id, reply_id, forward_id)`
- **新索引**：`(llm_status)` 加速按状态过滤
- **cron 平台过滤**：capture_user 入口跳过 `platform_name == "cron"`，让 cron 触发的 assistant 自动标 `proactive`
- **system_event 强制覆盖**：`MessageType.OTHER_MESSAGE`（poke / 加好友请求 / 通知等）→ `content_kind=['system_event']`

### 修复

- **修正纯 @BOT 无文字的误归类**：v2.x 标 `media_only`（错），v2.3.0 标 `content_kind=[] + at_id=<bot_id>`（正确）
- **修正纯 Reply 无文字的误归类**：v2.x 标 `media_only`（错），v2.3.0 标 `content_kind=[] + reply_id=<msg_id>`（正确）
- **修正 Poke 等通知事件的误归类**：v2.x 标 `media_only`（错），v2.3.0 标 `content_kind=['system_event']`（正确）
- **修正图文混合消息的媒体丢失**：v2.x content 只存文本，v2.3.0 content_kind 同时含 `['text','image']`

### 变更

- **API 重命名**：`tag_filter` 参数 → `llm_status` + 新增 `content_kind` 参数
- **`content_kind` 用 SQL `json_each` + `IN` 实现 ANY 语义**：返回 content_kind 数组中任一包含指定值的记录
- **空串 `""` 替代 `non_llm`**：`llm_status=""` 是默认值，过滤时传 `llm_status=[""]`（list 形式）
- **`_TAG_*` 常量删除**：替换为 `_LLM_*` 与 `_K_*`（content_kind）
- **`_is_media_only` / `_media_placeholder` 删除**：用 `_classify_content` + `_content_placeholder` 替代
- **`update_tag` → `update_llm_status`**：方法名匹配新字段名
- **mark_llm_triggered 简化**：所有 user（含图片/语音）都从 `""` 升级到 `llm_pending`，不再有"终态 tag"概念
- **capture_bot 的 no_mid 分支简化**：不再用专门 tag，用 `pair_id=NULL` 表达（仍按真实 LLM 路径走 llm_status）
- **Forward 一律不拆**：用户发的合并转发只存 1 条记录（v2.x 也是这样，v2.3.0 显式文档化）

### 删除

- `tag TEXT NOT NULL DEFAULT 'non_llm'` 列（拆为 `llm_status` + `content_kind`）
- `_TAG_NON_LLM` / `_TAG_LLM_PENDING` / `_TAG_LLM_SUCCESS` / `_TAG_MEDIA_ONLY` / `_TAG_NO_MID` / `_TAG_PROACTIVE` / `_TAG_ORPHAN` 七个常量
- `_MIGRATION_ADD_COLUMNS` 迁移元组（v2.3.0 不做迁移）
- `chat_memory_is_media` extra（v2.x 用来跳过 media_only 终态升级）
- `chat_memory_no_mid` 在 capture_bot 中的专门分支（v2.3.0 用 pair_id=NULL 表达）
- `query_history` 的 `tag_filter` 参数（替换为 `llm_status` + `content_kind`）
- `query_rounds` 的 `tag_filter` 参数（替换为 `llm_status` + `content_kind`）

### 兼容性

#### ⚠️ 破坏性变更：不做迁移，老数据丢失

v2.3.0 启动时检测到老 schema（缺 `llm_status` 列）直接 `DROP TABLE chat_memory_records` + `CREATE TABLE`。**老数据全部丢失**。

理由：v2.x 数据没保存多久，迁移成本（按老 tag 推导新两列，且 content_kind 无法精确恢复组件信息）高于价值。

#### API 破坏性变更

- `tag_filter` 参数完全删除，调用方必须改为 `llm_status` / `content_kind`
- 返回 dict 的 `tag` 字段删除，新增 `llm_status` / `content_kind` / `at_id` / `reply_id` / `forward_id`
- `query_history` / `query_rounds` 的过滤语义变化见上

### 已知限制

- 用户在该 umo 的**第一条消息是非 LLM 命令**时漏存（cid 尚未创建，且无 LLM 钩子兜底）
- 主动消息不经过 ProcessStage 时 `proactive` 不会出现（取决于宿主如何发起）
- 进程退出时 `_cleanup_task` 自然消亡，无 `on_unload` 显式取消
- **Forward 不拆开**：用户发的合并转发只存 1 条记录，不调 OneBot API 拉取内部 chain

## 2.1.0 — 2026-07-07

### 新增：审计/上下文字段补全（8 个）

每条记录额外携带 8 个上下文字段，便于下游按平台/群/发送者/时间等维度过滤与展示：

- `platform_id TEXT` —— 平台**实例 ID**（umo 拆分第 1 段，自 AstrBot v4.0 起对应 `platform_meta.id`，多实例时唯一）
- `platform_name TEXT` —— 平台**类型**（`platform_meta.name`，如 `aiocqhttp` / `lark` / `discord`）
- `message_type TEXT` —— 消息类型（umo 拆分第 2 段，`FriendMessage` / `GroupMessage` / `OtherMessage`）
- `session_id TEXT` —— 会话标识（umo 拆分第 3 段，QQ 号 / 群号）
- `self_id TEXT` —— 机器人自身 ID（`message_obj.self_id`，多 bot 同群时隔离审计）
- `group_id TEXT` —— 群号（私聊为 NULL，来自 `event.get_group_id()`）
- `sender_nickname TEXT` —— 发送者昵称（`event.get_sender_name()`，平台未给则为 NULL）
- `raw_timestamp INTEGER` —— 消息原始 Unix 时间戳（`message_obj.timestamp`，秒）

> **platform_id vs platform_name**：自 AstrBot v4.0 起 umo 第一段是实例 ID 而非类型；要"按类型聚合"用 `platform_name`，要"精确定位实例"用 `platform_id`。
>
> **raw_timestamp vs created_at**：`raw_timestamp` 是消息到达 AstrBot 的时间，`created_at` 是落库时间，通常差几毫秒到几秒。

### 变更

- **新索引**：`(platform_id, group_id, created_at)` 加速"某群最近 N 条"扫描
- **`query_history` / `query_rounds` 返回 dict 新增 8 字段**，向后兼容（纯增）
- **`_collect_audit_fields`**：统一在 main.py 提取审计字段，user 与 assistant 入口共用，避免重复代码

### 兼容性

- **纯增字段、向后兼容**：老调用方读旧字段不受影响
- **老库自动迁移**：启动时 `PRAGMA table_info` 检测列，缺则 `ALTER TABLE ADD COLUMN` 补，老数据新字段为 NULL
- umo 解析用 `split(":", 2)`，兼容 session_id 内含冒号的边角格式

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
