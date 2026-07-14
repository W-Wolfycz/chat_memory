# Changelog

## 2.3.4 — 2026-07-15

### persona 隔离（filter_by_persona）

新增 `persona_id` 列 + `filter_by_persona` 配置，让 CM 查询按当前 persona 严格过滤。切换 persona 时旧 persona 时期的对话自动隔离，切回后自然回归。

**改动**：

| 层 | 改动 |
|---|---|
| **schema** | 加 `persona_id TEXT` 列 + `ix_cm_persona` 索引；v2.3.3 老库（有 `llm_status` 缺 `persona_id`）自动 `ALTER TABLE ADD COLUMN` 补列（纯增列，不 RENAME 备份） |
| **入库** | `capture_user` / `capture_bot` 从 `conversation.persona_id` 取值填入；user 端缓存到 extras 供 bot 复用避免二次查 conv_mgr |
| **查询** | `query_rounds_raw` / `query_messages_raw` 加 `persona_id` + `filter_by_persona` 参数；user + EXISTS + assistant 三处一致加 `persona_id = :persona_id` 条件（配对模式） |
| **takeover** | `_takeover_query` 从 `req.conversation.persona_id` 取当前 persona 透传；`filter_by_persona=False` 时 persona_id 不参与过滤 |
| **配置** | `_conf_schema.json` 加 `filter_by_persona` 布尔项，默认 `false`（现行行为不变） |

**与 cross_session 的协同**：

| `filter_by_persona` | `cross_session` | 切 persona + /new + 切回旧行为 |
|---|---|---|
| F | F | 现行行为 |
| T | F | persona 隔离，但 /new 后切不回旧 persona 数据（cid 卡死） |
| F | T | 跨 cid 聚合，persona 不卡 |
| **T** | **T** | **完整隔离体验**——切 persona + /new + 切回仍能拉到旧 cid 的旧数据 |

**兜底**：`persona_id` 为空（老库 ALTER 补列后的 NULL 旧行 / 边缘平台取不到 persona）时即使 `filter_by_persona=True` 也跳过过滤，避免老数据全被滤光。

**未覆盖**：cid 不会自动随 persona 切换——同一 cid 下可能累积多个 persona 的数据。如需"切 persona 自动开新 cid"见 TODO。

新增 T38 测试覆盖 schema 静态 + 透传 + 真实 sqlite 行为 + 跨 cid persona 过滤。

### 对外 API 查询参数扩展

`query_history` / `query_rounds` 新增三个可选参数（纯增量，默认 None 时行为与 v2.3.3 一致）：

| 参数 | 类型 | 作用 |
|---|---|---|
| `persona_id` | `Optional[str]` | 按 persona 严格过滤（None / 空串跳过）；`query_rounds` 的 user + assistant 都加条件，保证配对同 persona |
| `since` | `Optional[datetime]` | `created_at >= since`（含端点）；tz-aware 自动转 UTC naive |
| `until` | `Optional[datetime]` | `created_at <= until`（含端点）；同上 |

**EXISTS 子查语义**：`query_rounds` 的 EXISTS 子查**不**加 persona / 时间条件，保持"有配对"语义。若配对 assistant 落在过滤范围外（如 persona 不一致或时间窗口外），该 user 仍被选中但 assistant 查询返回空，结果是 `[user_dict]` 一条。

**时区归一化**：新增 `_normalize_dt` 辅助函数——None 透传、tz-aware 转 UTC naive、naive 假定已 UTC（与 `insert` 的 `datetime.now(timezone.utc).replace(tzinfo=None)` 对齐）。

新增 T39 测试覆盖静态签名 + 真实 sqlite 行为 + EXISTS 语义。

## 2.3.3 — 2026-07-14

整合配对正确性修复、cross_session 升级、消息捕获分类、reasoning 污染防护、CODE_REVIEW 加固批次。

### 配对与捕获正确性

**P0-1：`assistant_map` 配对 key 错误** — `query_rounds*` 用 `setdefault(r[3], [])`，`r[3]` 是 `message_id`。但 `capture_bot` 写入 assistant 时 `message_id=None`（仅 user 有平台 mid）→ map 退化为 `{None: [所有 assistant]}`，查询用 `user.message_id`（非 None）查找永远空。**后果**：所有 `query_rounds*` 返回值退化为 `[[user_dict], ...]`，**assistant 配对全部丢失**（takeover 默认配置下 LLM 只看到 user）。**根因**：测试 T25 数据 `assistant.message_id="a1"` 与生产 `None` 不一致，长期掩盖。修复：3 处改用 `pair_id`（r[4]）；T25 还原为生产值；新增 T36 配对回归。

**user `content_kind` 错标 + 组件链读取源** — `AstrMessageEvent` 没有 `message_chain` 属性（官方 API 是 `event.get_messages()`），旧代码 `getattr(event, "message_chain", None)` 永远拿 None → Image/Video/Record/File/Face/Forward/At/Reply 全部漏抽；部分 Provider 把 user 文本放在 `event.message_str` 组件链为空 → kind 错标 `[]` → takeover 配置 `include_content_kinds=["text"]` 在 SQL 层滤光 → takeover 完全失效跳过注入。修复：`_extract_text` / `_classify_content` 改用 `get_messages()`，加 `message_str` 回退。⚠️ **老库不可回填**。

**assistant 端消息类型分类（CR2 #5）** — 新增 `_classify_assistant_chain(chain)`（与 user 端对称）。旧 `capture_bot` 只抽 `Plain`，纯媒体回复因 `not bot_text` 跳过不入库；现 `content_kind` 反映真实组件，纯媒体回复用占位符（`[image]` 等）入库，跳过条件改为 `not asst_kind`。媒体 URL 仍不入库（见 TODO）。

**reasoning parts 污染防护** — 部分 reasoning 模型（GLM / DeepSeek / o1）把 content parts `[{'type':'think',...}]` 整体 `str()` 塞进 Plain。`capture_bot` 入库前 + `_takeover_normalize` 注入前双保险用 `_strip_reasoning_prefix` 剥离（字符级跟踪 `[`/`]` 平衡 + 字符串/转义识别）。

### cross_session 升级为真正"群私聊互通"（含 T+T 混合语义）

旧行为仅跨 CID 同 umo，schema 写"跨群会话"但实际做不到。新行为（4 种 scope 组合）：

| `cross_session` | `full_group` | 数据范围 |
|---|---|---|
| F | F | 当前 umo 当前 user（默认） |
| T | F | 跨 umo 当前 user（群私聊互通） |
| F | T | 当前 umo 整群全员 |
| T | T | **混合**：当前 umo 整群 + 其他 umo 当前 user |

T+T 混合：SQL 层 OR 条件 — 当前 umo 不限 user（整群聚合），其他 umo 仅限当前 user（不跨到别的用户在其他群/私聊的对话）。

实现：新增 `_scope_filter(umo, user_id, cross_umo, full_group)` 构造 WHERE；`query_rounds_raw` / `query_messages_raw` 加双参数；`_takeover_query` 透传。EXISTS 子查 `a.umo = chat_memory_records.umo` 行内自连接保留，跨 umo 后 user 仍能在自己 umo 内找配对 assistant。

### 加固批次（CODE_REVIEW 反馈）

7 项低风险高价值可做项：

| 项 | 改动 |
|---|---|
| **时区一致性** | `insert` 用 `datetime.now(timezone.utc).replace(tzinfo=None)`，与 schema default `CURRENT_TIMESTAMP`（UTC）对齐 |
| **WAL busy_timeout** | `PRAGMA busy_timeout=5000`，锁冲突等 5s 而非立即报 locked |
| **公开 API limit 钳制** | `query_latest` / `query_rounds` 入口钳到 `[1, 1000]`，防传 -1 触发 SQLite `LIMIT -1`（=不限制）返回全库 |
| **混合模式 overfetch** | `_takeover_query` 混合模式 `limit * 2`，避免先 LIMIT 后规整（丢头部/尾部/纯媒体）导致空上下文 |
| **terminate flush 窗口** | `_pending_tasks` 不再直接 cancel，先 `wait_for(gather, timeout=5)` 保护在写的 assistant；超时才 cancel。timeout 抽为 `_TERMINATE_FLUSH_TIMEOUT` 常量便于测试注入 |
| **老库迁移加备份** | 不再 `DROP TABLE`，改为 `ALTER TABLE RENAME TO chat_memory_records_backup_<ts>`；老数据保留在同 .db 文件可手动恢复 |
| **/reset 审计痕迹** | 删除前 `SELECT count(*)` + `logger.warning`（含 CID + 条数 + "result_text 命中 'reset'" 标记）；误判可追溯 |

新增 T37 静态验证 7 项；T22（terminate）注入短 timeout（0.05s）覆盖超时回退路径，测试耗时从 5s+ 降到 0.25s。

### 清理

- 删除 `ct_drop_media` / `ct_merge_consecutive` 实例属性（仅赋值不读取）
- 删除 `query_rounds_umo` / `query_solo_assistants` 方法（无调用方）

### 未覆盖（见 TODO）

- 媒体 URL 存储（schema 变更 + 多平台字段差异）
- Forward 拆开（aiocqhttp 专属 API）
- 首条非 LLM 漏存兜底（高复杂低收益）
- _safe_insert orphan 区分（SQLite 错误码映射）
- fire-and-forget 单写队列 / 批量事务（架构层调整）
- token 估算 / tool_calls 存储 / schema version 工具 / 唯一约束 / 配对复合 key（大工程）

## 2.3.2 — 2026-07-12

### 轮数精确 + 内容白名单下沉 SQL

> ⚠️ **行为变化**：v2.3.1 升级后，takeover 默认只让含 `text` 的消息进入 LLM 上下文（纯图 / poke / 加好友通知等被滤掉）。capture 仍照常全量入库。清空 `include_content_kinds` 可恢复 v2.3.1 行为。

- **limit_rounds 含义动态化**：仅 `llm_success` → 轮数（配对）；含其他状态 → 消息数（单条）
- **include_content_kinds 下沉 SQL**：被过滤的记录不占用 limit 名额（之前查 N 轮 → 过滤后变 M 轮）
- **ANY / ALL 双语义**（`include_all_match` 开关）：默认 ANY（任一交集）；ALL（全部属于且非空）
- 新增 takeover 专用内部方法 `query_rounds_raw` / `query_messages_raw`；对外 API 语义不变

## 2.3.1 — 2026-07-09

### 上下文接管（context_takeover）

让 ChatMemory 可选成为唯一上下文源。开启后每轮 LLM 请求时用 CM 数据覆盖 `req.contexts`，并清空 native `conversation.history` 防累积。

**功能**：接管钩子（priority=-100 最后执行 + `_no_save` 不回写）/ cross_session + full_group 两维度 / 状态多选 / 4 种前缀模式（off/time/sender/time_sender）/ 单边 assistant `[主动]`/`[未配对]` 标识

**自动行为**：过滤纯媒体（图文混合保留文本）；合并连续同 role；丢头部非 user 与尾部单边 assistant

**修复（第三方评审）**：cron 主动消息误标 orphan；solo assistant 跨用户泄漏；`limit_rounds` 钳到 `[1, 100]`；新增 `terminate()` 钩子；ORDER BY 加 id 次级

**稳定性**：SQLite WAL + `synchronous=NORMAL`；fire-and-forget 任务引用保活

## 2.3.0 — 2026-07-08

### tag 拆双列 + 内容白名单分类

v2.x 的单 `tag` 列混合三个独立维度（LLM 路径、消息形态、配对状态），v2.3 拆为 `llm_status` + `content_kind`。引入组件白名单分类，修正 v2.x 对纯 @BOT、纯 Reply、Poke 通知的错误归类。

**破坏性变更**：不做迁移，启动时检测老 schema 直接 DROP 重建。`tag_filter` 参数删除。

## 2.1.0 — 2026-07-07

补全 8 个审计字段：`platform_id` / `platform_name` / `message_type` / `session_id` / `self_id` / `group_id` / `sender_nickname` / `raw_timestamp`。纯增字段，老库自动 ALTER 补列。

## 2.0.0 — 2026-07-03

### 全量捕获 + 7-tag 配对存储

v1.x 仅捕获 LLM 路径对话。v2.0 改为所有进入 ProcessStage 的 user 消息立即落库。新增 `message_id` / `pair_id` 字段支持配对查询。

**破坏性变更**：`query_history` 默认返回范围扩大到全量消息。要保持 v1.x 语义传 `tag_filter="llm_success"`。

## 1.1.0 — 2026-06-26

群聊场景：`user_id` 参数改为可选，为空时返回该会话所有用户混合记录。

## 1.0.0 — 2026-06-13

初始版本。以 `UMO + conversation_id + user_id` 为维度的异步 SQLite 对话存档。
