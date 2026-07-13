# Changelog

## 2.3.4 — 2026-07-14

### cross_session 升级为真正"群私聊互通"

**旧行为**：`cross_session` 仅跨 CID 同 umo（极少用），schema description 写"跨群会话"但实际做不到跨群。

**新行为**：开启后查询条件从 `umo = :umo` 改为 `platform_id = :pid`（从 umo 提取首段），按 `platform_id + user_id` 聚合 → 同一用户在所有群 + 私聊的消息都进入上下文。

**接管范围矩阵**：

| `cross_session` | `full_group` | 数据范围 |
|---|---|---|
| F | F | 同 CID 同用户 |
| T | F | **跨 CID 跨 umo 同用户**（群私聊互通） |
| F | T | 同 CID 同群全员 |
| T | T | 跨 CID 跨 umo 全 platform（慎用） |

**实现**：
- `storage.py` 加 `_umo_filter(umo, cross_umo)` 辅助函数 + `query_rounds_raw` / `query_messages_raw` 加 `cross_umo: bool=False` 参数
- `main.py` `_takeover_query` 在 `ct_cross_session=True` 时传 `cross_umo=True`
- EXISTS 子查的 `a.umo = chat_memory_records.umo` 行内自连接保留，跨 umo 后每条 user 仍能在自己 umo 内找配对 assistant（依赖 `message_id` 全平台唯一，aiocqhttp 满足）

**副作用**：旧行为依赖者（极少）从"跨 CID 同 umo"变"跨 CID 跨 umo"，对单群单私聊用户无变化。

**文档同步**：`_conf_schema.json` description 改"跨会话目标（群私聊互通）"，hint 重写；`README.md` 表格 + 接管范围说明对齐。

### 修复 user content_kind 错标导致 takeover 完全失效

**Bug**：`_classify_content` 只从 `event.message_chain` 提取 Plain 组件判 text kind。但 AstrBot 部分 Provider/适配器把 user 文本放在 `event.message_str`，message_chain 为空 → kind 错标为 `[]`。生产环境实测 100% user 消息都被错标。

**连锁反应**：
1. user 入库 content_kind=[]
2. takeover 配置 `include_content_kinds=["text"]` 把所有 user 在 SQL 层滤光
3. 只剩 assistant 进 `_takeover_normalize` → 头部 pop（非 user）→ 全 pop → 空 → "规整后为空"
4. takeover 跳过注入，AstrBot 用 native history 兜底（含 `<interaction>` / `<system_reminder>` 等各插件 prefix 累积）

**修复**：`_classify_content` 加 message_str 回退 — chain 中无非空 Plain 但 message_str 有文本时，补 `_K_TEXT`（与 `_extract_text` 回退逻辑对齐）。

### 修复组件链读取源错误（图片 / @ / 回复 / 转发全不入库）

**真正根因**：`AstrMessageEvent` 上**没有** `message_chain` 属性，官方 API 是 `event.get_messages()`（或 `event.message_obj.message`）。`_extract_text` / `_classify_content` 用 `getattr(event, "message_chain", None)` 永远拿到 `None`，组件链恒为空 → 所有非文本组件（`Image` / `Video` / `Record` / `File` / `Face` / `Forward` / `At` / `Reply`）全部漏抽。

**影响**（v2.3.0 起一直坏）：
- 纯图片 / 纯视频 / 纯语音消息：kind=[] + message_str="" → 触发"消息完全为空"跳过，**根本不入库**
- 纯 @BOT：kind=[] + at_id=None → 跳过不入库
- 文本 + @/回复/转发：文本能入库（靠 message_str 回退），但 `at_id` / `reply_id` / `forward_id` 全丢
- 合并转发：kind 不含 forward，forward_id 不提取

**修复**：把 `_extract_text` 和 `_classify_content` 的 chain 读取源从 `getattr(event, "message_chain", None)` 改为 `event.get_messages()`。

**v2.3.4 加的 message_str 回退保留**：AstrBot 部分 Provider 确实只填 `event.message_str`、组件链为空，回退仍是必要兜底。

> ⚠️ **老库不可回填**：被错跳过的图片 / @ / 回复 / 转发消息**根本没入库**，信息从一开始就丢了。本修复只覆盖未来消息。已入库的纯文本消息 `content_kind` 错标问题仍可用 `scripts/fix_content_kind_v2.3.4.py` 修。

## 2.3.3 — 2026-07-14

### 修复 reasoning parts 污染上下文

**Bug**：AstrBot 部分 Provider（GLM / DeepSeek / o1 等 reasoning 模型）把 content parts 列表 `[{'type': 'think', 'content': '...', 'encrypted': None}]` 整体 `str()` 后塞进 Plain 组件，紧跟实际回复。`capture_bot` 入库时把这个序列化字符串当回复文本一起存了，takeover 注入时 think 推理内容污染了 LLM 上下文。

**修复**（双保险）：
- `capture_bot` 入库前用 `_strip_reasoning_prefix` 剥离前缀
- `_takeover_normalize` 注入前同样剥离，处理老库已有数据（无需迁移）

剥离逻辑：字符级跟踪 `[` / `]` 平衡 + 字符串/转义识别，找到列表结束位置返回剩余部分。非该前缀格式 / 解析失败均保守返回原文。

## 2.3.2 — 2026-07-12

### 轮数精确 + 内容白名单下沉 SQL

> ⚠️ **行为变化**：v2.3.1 升级到 v2.3.2 后，开启 takeover 时默认只让含 `text` 的消息进入 LLM 上下文（纯图 / poke / 加好友通知等被滤掉）。capture 仍照常全量入库。如需恢复 v2.3.1 行为（不过滤），清空 `include_content_kinds` 即可。

**limit_rounds 含义动态化**（依赖 `llm_status_filter`）：
- 仅 `llm_success` → **轮数**（user-assistant 配对，一轮 = 一对）
- 含其他状态 → **消息数**（单条记录）

**include_content_kinds 下沉 SQL 层**：之前在应用层过滤导致轮数不精确（查 N 轮 → 过滤后剩 M 轮）。现在 SQL 直接判定白名单，limit 名额只算有效记录。

**白名单 ANY / ALL 双语义**（`include_all_match` 开关）：
- ANY（默认）：消息的 `content_kind` 与白名单**任一交集**即进入上下文
- ALL：消息的所有 kind 都须在白名单内（且非空）才进入

默认 `["text"]` + ANY：只让含文本的消息进入上下文。清空白名单 = 不过滤（全量进入）。

**新增 takeover 专用内部方法**（storage 层，不影响对外 API）：
- `query_rounds_raw(umo, cid, user_id, limit, include_kinds, all_match)` — 配对模式
- `query_messages_raw(umo, cid, user_id, limit, statuses, include_kinds, all_match)` — 混合模式

对外 API（`query_rounds` / `query_latest`）语义不变。

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
