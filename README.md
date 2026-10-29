# ChatMemory

全量对话记录存档 + 合并存储（跨实例去重）+ 可切读的上下文源 + 自带查询台。

- 版本：`2.0.0-beta`；AstrBot `>=4.16,<5`；平台：`aiocqhttp`（NapCat）、`qq_official`
- 数据库：SQLite；旧表 schema `user_version = 8`；合并存储结构线 `SHADOW_SCHEMA_VERSION = 6`
- 仓库：<https://github.com/W-Wolfycz/chat_memory>

本文件按**当前代码**写，面向要在此基础上接入别的插件的人：数据模型、对外 API、读源切换规则、查询台契约都在这里。

---

## 1. 它做什么

| 能力 | 说明 |
|---|---|
| **全量落库** | 每条进入 ProcessStage 的消息立即写 SQLite：正文、发送者、会话、平台时间、内容形态（`content_kind`）、关系（@/引用/转发）、人格、轮次 |
| **强制双写** | 本版起旧表照旧写，同时镜像到 **7 张**合并存储表（6 张数据表：`messages` / `message_bots` / `reply_messages` / `tool_calls` / `session_seq` / `group_names`，外加 1 张结构元信息表 `shadow_meta`；一条物理消息一行：三个 BOT 看到同一条群消息也只存一份）。**没有关闭开关** |
| **切读** | 双写满 **7 天** 后，配置里的 `shadow_read` 才生效：上下文接管、公开查询 API **与查询台**一起改读合并存储；没满之前配置照收不生效（只写不读） |
| **上下文接管** | 可让 ChatMemory 成为唯一上下文源：按轮取数、跨会话/整群/按 persona、工具段回放、原生历史清除 |
| **查询台** | 自带 Plugin Page（数据概览 / 记录查询 / 工具调用），三种模式（只读 / 高级只读 / 删除），软删除与快照导出 |
| **媒体归档** | 图片/语音/视频/文件落盘，按保留期与总量上限清理 |

---

## 2. 安装

1. 插件目录放到 `data/plugins/chat_memory/`，重载插件。
2. 首次加载自动建库：`data/plugin_data/chat_memory/chat_memory.db`（查询台的「数据库路径」可切到别的只读副本）。
3. 依赖见 `requirements.txt`：`sqlalchemy[asyncio]`、`aiosqlite`、`aiohttp`、`Pillow`、`quart`、`tzdata`（Windows）。

---

## 3. 配置（`_conf_schema.json`）

顶层平铺，无隐藏开关。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_content_length` | int | `0` | 单条正文落库上限（0＝不截断） |
| `auto_cleanup_days` | int | `0` | 自动清理阈值（0＝不清理） |
| `log_with_bot_id` | bool | `false` | 日志前缀加 `[platform:{platform_id}]` |
| `shadow_read` | bool | `false` | 切读开关（**受 7 天门槛约束**） |
| `sent_capture_history` | bool | `false` | 拉取通道：发送后拉群历史补自己发言（push 回显的兜底） |
| `context_takeover.enable` | bool | `false` | 接管上下文 |
| `context_takeover.cross_session` | bool | `false` | 跨 UMO 取数（群私聊互通） |
| `context_takeover.cross_session_blacklist` | list | `[]` | 黑名单，`G:<群号>` 或 `F:<用户号>` |
| `context_takeover.full_group` | bool | `false` | 整群取数（不限当前用户） |
| `context_takeover.limit_rounds` | int | `30` | 取多少轮 |
| `context_takeover.max_context_chars` | int | `0` | 上下文字符上限（0＝不限） |
| `context_takeover.llm_status_filter` | list | `["llm_success"]` | 取哪些 LLM 状态（`["no_llm"]` 表示空值） |
| `context_takeover.include_content_kinds` | list | `["text"]` | 内容类型白名单：`text` / `image` / `video` / `voice` / `emoji` / `poke` / `file` / `forward` / `event` |
| `context_takeover.event_kinds` | list | 全部 8 类 | 事件子类白名单 |
| `context_takeover.recall_skip_context` | bool | `false` | 撤回消息不进上下文 |
| `context_takeover.include_all_match` | bool | `false` | 白名单"全含"还是"任一" |
| `context_takeover.clear_native_history` | bool | `true` | 清原生历史，避免两套上下文并存 |
| `context_takeover.fallback_to_native_on_empty` | bool | `false` | 取不到时回退原生历史（默认严格清空） |
| `context_takeover.filter_by_persona` | bool | `false` | 按当前生效 persona 过滤 |
| `context_takeover.keep_tool_turns` | int | `2` | 保留最近几轮的工具段回放 |
| `media_archive.enabled` | bool | `true` | 归档开关 |
| `media_archive.include_video` | bool | `false` | 是否归档视频 |
| `media_archive.retention_days` | int | `30` | 保留天数 |
| `media_archive.max_total_mb` | int | `2048` | 归档总量上限 |

---

## 4. 数据模型

### 4.1 两套表

**旧表（V1，仍在双写）**

| 表 | 粒度 | 关键列 |
|---|---|---|
| `chat_memory_records` | 每实例每轮一行（user / assistant 各一行） | `umo`、`conversation_id`、`user_id`、`role`、`content`、`message_id`、`pair_id`、`llm_status`、`content_kind`、`persona_id`、`turn_id`、`send_status`、`deleted_at`、`relation_data` |
| `chat_memory_tool_records` | 每次工具调用一行 | `umo`、`conversation_id`、`turn_id`、`call_index`、`tool_name`、`tool_args`、`tool_result` |
| `chat_memory_media_archive` | 每个归档文件一行 | `media_id`、`file_name`、`kind`、`size_bytes`、`content_hash` |

**合并存储（V2，`SHADOW_SCHEMA_VERSION = 6`）**

| 表 | 粒度 | 说明 |
|---|---|---|
| `chat_memory_messages` | **一条物理消息一行** | `msg_uid` 主键（平台键 `p:…` / 本地键 `l:…`）、`seq`（会话内递增）、`platform_timestamp`、`platform_seq`、`content`（模板，读前须按 `relation_data` 渲染）、`content_kind`、`sender_id`、`sender_nickname`、`recalled_at`、`deleted_at` |
| `chat_memory_message_bots` | 每实例的参与行 | `(msg_uid, platform_id, self_id, conversation_id)` 唯一；`role`、`llm_status`、`send_status`、`persona_id`、`target_id`、`turn_id`、`platform_message_id`、`raw_timestamp` |
| `chat_memory_reply_messages` | **一轮一条计划**（主键 `turn_id`） | 发送前抓到的正文与轮次状态；`content_kind`/`send_status` 带 CHECK |
| `chat_memory_tool_calls` | 每次工具调用一行 | 幂等键 `(turn_id, call_index)`；按会话取用 `ix_cmtc_umo_turn` |
| `chat_memory_session_seq` | 会话序号计数器 | `(platform_name, message_type, session_id)` → `last_seq`，**只增不减**（会话删空也不回退） |
| `chat_memory_group_names` | 群名快照 | `(platform_name, session_id)` → 最近一次读到的群名（不保留改名历史） |
| `chat_memory_shadow_meta` | 元信息 | `schema_version`、`dual_write_started_at`、`shadow_migrate`（键白名单由 CHECK 约束） |

### 4.2 三个必须分清的概念

- **物理消息**：平台上真实存在的一条（`chat_memory_messages`）。三个实例看到同一条群消息 → **一行**。
- **参与行**：某个实例看到这条消息时的视角（`chat_memory_message_bots`）。`role` 表示"属于谁的轮次侧"：`user`＝入站观察（含别人做的动作、系统事件），`assistant`＝我们发出的回复。
- **逻辑轮次**：`turn_id`（uuid4）把"一条提问 + 它的回复 + 这轮工具调用"绑在一起。一轮的 assistant 侧可能是 1 条计划成员 + N 条实发段。

### 4.3 状态字段

- `llm_status`：`""`（未走 LLM）/ `llm_pending` / `llm_success` / `proactive`（主动消息）/ `orphan`（用户消息漏存）
- `send_status`：`""` / `prepared`（发送前的计划）/ `send_attempted`（AstrBot 发送流程结束）
- `content_kind`：JSON 数组，如 `["text"]`、`["image"]`、`["event"]`、`["text","image"]`

---

## 5. 读源切换（切读）

```
dual_write_started_at + 7 天 <= now   →  允许读合并存储
```

- **三个读侧共用同一个判据**（`shared_store.SHADOW_READ_MIN_DAYS = 7`）：上下文接管、公开查询 API、查询台。同时切或同时不切。
- 未满 7 天：配置照收但不生效，启动日志写"还需约 N 天"，此时只写不读。
- 合并存储结构不认识（`schema_version` ≠ 3）→ 加载时丢弃重建（只清镜像，`dual_write_started_at` 沿用，窗口不清零）。
- 查询台读源额外要求：3 张核心影子表齐 + `schema_version` 等于写入侧常量 + 窗口已满；否则读旧表（旧表**不去重**）。

---

## 6. 对外查询 API（Python）

从别的插件里调：`cm = context.get_registered_star("chat_memory")`。

```python
rows = await cm.query_history(
    umo="BOT1:GroupMessage:10001", conversation_id="cid", user_id="10002",
    limit=20, llm_status=["llm_success"], content_kind=["text"],
    role_filter=None, persona_id=None, since=None, until=None,
    from_oldest=False, after_id=None, content_kind_all_match=False,
)

rounds = await cm.query_rounds(umo, "cid", user_id="10002", limit_rounds=10)
# → [[user_dict, assistant_dict], ...]
```

| 方法 | 读源 | 说明 |
|---|---|---|
| `query_history(...)` | 跟随 `shadow_read`（含门槛） | 消息级平铺 |
| `query_rounds(...)` | 同上 | 轮次级，`[[user, assistant], ...]` |
| `query_history_v2(...)` | **强制合并存储** | 对拍/灰度用，不受开关与门槛限制 |
| `query_rounds_v2(...)` | **强制合并存储** | 同上 |

要点：

- `llm_status` / `content_kind` 接受字符串或列表。空值（从未走过 LLM 的行）在**配置项与查询台**里写作 `"no_llm"`；**进程内 API 直接传空串 `""`**——`"no_llm"` 这个别名只在配置解析与查询台做映射。
- **`since` / `until` 一律按 UTC naive 解释**（库内 `created_at` 就是 UTC naive）；返回的 `created_at` 是**插件配置时区的 naive 字符串**，另有 `created_at_utc` 给需要做游标的调用方。直接把返回的 `created_at` 回传给 `since`（或配 `after_id` 翻页）会整体偏一个时区差，请改用 `created_at_utc`。
- `query_rounds*` 的"配对"＝**这一轮有 assistant 侧**（计划成员或实发段），**不额外按 llm_status 过滤**（与旧表同名方法同语义）。
- `storage.*` 底层方法（`query_latest` / `query_rounds` / `query_latest_v2` / `query_rounds_v2` / `query_tool_records` …）可直接用，但**不做 scope 与黑名单解析**。

---

## 7. 查询台（Plugin Page）

入口：AstrBot → 插件 → ChatMemory。三页：

| 页 | 内容 |
|---|---|
| **数据概览** | 记录/来源/会话/用户/工具/媒体计数、近 30 天活动、状态与工具分布、数据库诊断 |
| **记录查询** | 筛选 + 结果列表 + 详情 + 软删除 |
| **工具调用** | 工具筛选 + 调用卡片 + 详情 + 软删除 |

### 7.1 三种模式

| 模式 | 行为 |
|---|---|
| **只读**（默认） | 纯查看 |
| **高级只读** | 仅记录查询页可见；选一个 persona 后按其轮次收窄，并给每条追加 chip |
| **删除模式** | 列表与详情出现软删除/恢复图标（仅插件实时库开放） |

### 7.2 记录筛选（8 项 + 关键词）

| 控件 | 取值 | 查的列 |
|---|---|---|
| 聊天类型 | `group + private` / `group` / `private` | `message_type` |
| QQ 号 | 群号或对方号（精确） | `session_id` |
| 发言者 | 纯数字＝QQ 号精确；否则昵称模糊 | `sender_id` / `sender_nickname` |
| 内容类型 | `all` / `text` / `image` / … | `content_kind` |
| 事件子类 | `all` / `ban` / `member` / `title` / `recall` / `resource` / `poke` / `reaction` / `system` | `relation_data.event.type` |
| 起止时间 | 本地时区 | `created_at` |
| 删除状态 | `all` / 未删除 / 已删除 | `deleted_at` |
| 每页数量 | 20 / 50 / 100 / 200 | — |
| 关键词 | 正文 / 昵称 / user_id / message_id / turn_id | 多列 LIKE |

高级模式额外三项：**角色**（`user + assistant` / `user` / `assistant`）、**该轮 LLM 状态**、**配对**（`all` / 仅看配对）。

### 7.3 卡片 chip

消息卡（后四项只在高级模式出现）：

```
[text] [event·reaction] [群聊·group_demo（群名）] [已撤回]
[已配对#3] [assistant] [llm_success] [send_attempted]
```

- `群聊·群号（群名）`：群名走 OneBot `get_group_list` 现查（6 小时进程内缓存，**不落库**）
- `已配对#N`：同一轮的所有消息共用同一个号（当前页第几对）
- 状态 chip 按取值分色：`llm_success` 青 / `proactive` 琥珀 / `llm_pending` 蓝 / `orphan` 红 / `no_llm` 灰
- 事件行**不加**角色与状态 chip（事件不是提问也不是回答）

工具卡：

```
[头像] [轮 a92302f4 · 第 2 次调用] [BOT1] [persona_demo] [群聊·group_demo（群名）]
```

- 本轮只有 1 次调用时**不显示**步骤 chip
- `轮 xxxx` 是 `turn_id` 前 8 位（uuid 唯一、跨页稳定）
- `BOT1` 是 `umo` 第一段；`persona_demo` 是这一轮的人设；头像用该 BOT 的 `self_id` 现查
- 技术值（`turn_id` / `umo` / `conversation_id` / 记录 ID）在「详情」里

---

## 8. 查询台 Web API

前缀 `/api/plug/chat_memory/`，由 `ui/web_api.py` 注册。除软删除、恢复、导出与保存设置外全是只读。

| 端点 | 方法 | 参数 | 返回 |
|---|---|---|---|
| `about` | GET | — | `name` / `display_name` / `version` / `read_only` / `write_enabled` / `writable_path` / `default_page_size` / `max_page_size` / `timezone` |
| `settings` | GET | — | `database_path` / `using_default_path` / `default_database_path` / `immutable_fallback` |
| `settings` | POST | `{database_path, immutable_fallback}` | 保存后的设置 |
| `health` | GET | — | 数据库健康（路径、journal、WAL、schema 版本） |
| `overview` | GET | — | `summary` / `daily` / `roles` / `statuses` / `tools` / `media` / `database` |
| `facets` | GET | 与 `query` 同名的筛选参数 | `{kinds, database}` |
| `query` | POST | §7.2 的筛选键 + `persona_id` / `advanced` / `persona_role` / `persona_llm_status` / `paired_only` / `page` / `page_size` | `{items, total, page, page_size, page_count, filters, database}` |
| `record` | GET | `id` | `{record, database}` |
| `soft-delete` | POST | `{id, target?}`（`target` = `record` / `tool`） | 更新后的状态 |
| `restore` | POST | 同上 | 同上 |
| `tools` | POST | `tool_name` / `chat_type` / `session_id` / `platform_id` / `persona_id` / `since` / `until` / `deleted_state` / `page` / `page_size` | `{items, total, …}` |
| `group-names` | GET | — | `{groups: {群号: 群名}}`（现查，不落库） |
| `personas` | GET | — | `{personas: [{value, count}]}` |
| `media/<media_id>` | GET | `as=base64` 可选 | 文件字节或 base64 |
| `export` | POST | — | `{file_name, size_bytes}`（SQLite backup 快照） |
| `export/<file_name>` | GET | — | 下载快照 |

`query.items[]` 关键字段：

```text
record_id（合并存储行 = rowid + 1_000_000_000）, umo, conversation_id, role, content,
content_kind, relation_data, reply_view, media_view, sender_id, sender_nickname,
session_id, message_type, created_at, created_at_utc, llm_status, send_status,
persona_id, turn_id, message_id, recalled_at, deleted, deleted_at,
advanced（仅高级模式：{turn_id, llm_status, send_status, turn_llm_status,
                       turn_send_status, role, paired}）
```

---

## 9. 媒体归档

- 目录：`data/plugin_data/chat_memory/media/`（临时目录 `.tmp/`）
- 命名：内容哈希前缀 + 扩展名；同一文件只存一份（内容哈希去重）
- 清理：按 `retention_days` 与 `max_total_mb`；被多条记录共用的文件不删
- 归档/下载失败：前 3 次 WARNING，之后 DEBUG（带累计次数）
- 查询台按 `media_id` 回看与下载；`as=base64` 供沙箱 iframe 内桥接读取

---

## 10. 钩子与写入时机

| Hook | 优先级 | 用途 |
|---|---|---|
| `event_message_type(ALL)` | 2000 | 入站消息捕获（user） |
| `event_message_type(ALL)` | 1000 | 事件/通知类捕获 |
| `on_llm_request` | 默认 / `-100` / `-1000` | 补捕获、`llm_status → llm_pending`、上下文注入 |
| `on_llm_tool_respond` | — | 工具调用落库 |
| `on_decorating_result` | 10000 | 发送前写计划成员（一轮一条）并记发送侧账本 |
| `after_message_sent` | — | 标记 `send_status = send_attempted` |

自己发言的实际形态靠两条通道补：**push 回显**（NapCat 自身消息上报，主通道）与 **pull 拉取**（`sent_capture_history`，兜底）。两条都依赖发送侧内存账本（60 秒宽限）认领轮次。

---

### 10.1 自己发言的昵称是怎么来的

自己发出的消息，`sender_nickname` 有三条来源，按顺序兜底：

1. **写行前主动查一次**：群聊 `get_group_member_info`（群名片优先）、私聊
   `get_stranger_info`（备注 → QQ 昵称），拿到的名字直接写进这一行。结果缓存 6 小时；
   收到 `group_card`（群名片变更）通知立即作废该会话缓存。
2. 查不到时留空，`lookup_sender_nickname` 抄同会话、同发送者的最近一条**真名**（排除号码）。

**历史行不改**：没有"回填既有行"的通道——名字是写入当时的快照，旧数据保持原样；
只有新写入的行按当前规则取名。

取名优先级**按会话类型分档**（都从原始事件里分别取 `card` 与 `nickname`，适配器给的
`get_sender_name()` 分不出两者）：

| 会话 | 优先级 |
|---|---|
| 群聊 | **群名片** → QQ 昵称 → 自己的登录昵称 → QQ 号 |
| 私聊（含临时会话） | **好友备注** → QQ 昵称 → QQ 号（**不取群名片**） |

备注只能从 `get_friend_list` 拿（消息事件里没有该字段），进程内缓存 6 小时；`remark` 等于
`nickname` 时视为没设备注。号码只作最后一档用于查询台排障；上下文渲染时号码会被替换成
「未知成员」，账号不进 LLM 上下文。

**临时会话**（非好友私聊）会被记为私聊，并按私聊档位取名；来源群记在
`relation_data`（`sub_type` / `temp_group_id` / `temp_source`，NapCat 查不到时的哨兵值
`284840486` 会被丢弃），目前只存不用。

> 某账号在群里仍显示 QQ 昵称，只说明它在这个群**没有设群名片**（`card` 为空）；把 DEBUG 打开
> 就能看到接口返回的 `card` / `nickname` 原值。

已有真名的行不会被改写（昵称是当时那一刻的快照）。

## 11. 文件结构

```
chat_memory/
├── main.py                 # 入口：钩子、捕获、切读门槛、查询台装配
├── storage.py              # 旧表读写 + 迁移 + 软删除 + 媒体
├── shared_store.py         # 合并存储 DDL/镜像/投影 + SHADOW_READ_MIN_DAYS
├── shadow_read.py          # 合并存储读路径（消息级 / 轮次级 / 计划兜底）
├── shadow_diag.py          # 计划成员写入与诊断
├── context_builder.py      # 上下文组装（<cm_event>/<cm_mention>/<cm_solo>…）
├── message_classifier.py   # 内容形态与事件分类
├── relation_codec.py       # relation_data 编解码与模板渲染
├── media_archive.py        # 媒体归档 worker 与清理
├── qq_face.py              # QQ 表情/face 文本化
├── group_names.py          # 群名现查缓存（get_group_list）
├── models.py               # 枚举与校验集合
├── ui/repository.py        # 查询台数据层（读源判定、筛选、软删除、导出）
├── ui/web_api.py           # 查询台 Web API
└── pages/explorer/         # 前端（app.js / index.html / styles.css）
```

---

## 12. 已知限制

- **查询台首次取数会慢约 0.6 秒**：合并读取的视图（v2 投影 + 旧历史去重）按库指纹物化到
  TEMP 表，第一次查询要建一次（~0.6s）；之后取一页 70~140ms、概览 300ms 内且 30 秒缓存。
  库有写入（含 WAL 追加）时指纹变化、下次查询重建一次。


1. **历史 assistant 行的 `llm_status` 为空**：1.5.4 及更早的回显行只从内存账本抄状态，没命中就永久空。写入侧已修（账本带状态），历史数据不修复。影响：查询台高级模式的状态 chip、按状态筛 assistant 行；**不影响上下文内容**（回复按 `turn_id` 挂回，不看状态）。
2. **切读只覆盖双写之后**：`shadow_read` 没有旧表 UNION，切读后上下文与公开 API 看不到双写起点前的历史（查询台有旧表段，两侧口径不同）。
3. **自己发言的形状依赖账本**：60 秒宽限内没认领到轮次的回显段只写物理行（`turn_id` 为空），这一轮在配对读里会退回"计划全文"。
4. **`pair_id` 只存在于旧表**：合并存储恒为 `None`，依赖它的旧式配对在切读后失效（新轮次一律用 `turn_id`）。
5. **旧表退役不在本版范围**：单写新表是后续版本的事。

---

## 13. 外部只读读者契约（external reader）

给**不 import 本插件、不注册 AstrBot**、只用 `mode=ro` 打开数据库的独立进程（例如画像守护进程）。它们消费的是**表契约**，下面是当前版本（`schema_version = 6`）承诺能用的部分。

### 13.1 读哪些表

```sql
-- 物理消息：一条平台消息一行（多实例看到同一条也只一行）
SELECT msg_uid, seq, platform_name, message_type, session_id,
       sender_id, sender_nickname, platform_timestamp, content, content_kind,
       relation_data, platform_seq, recalled_at, deleted_at, created_at
  FROM chat_memory_messages
 WHERE deleted_at IS NULL                       -- 软删除过滤（必须）
 ORDER BY seq;

-- 每实例视角：谁看到的、属于哪一轮、什么状态
SELECT msg_uid, platform_id, self_id, conversation_id, umo, role,
       platform_message_id, turn_id, llm_status, send_status,
       persona_id, target_id, raw_timestamp
  FROM chat_memory_message_bots
 WHERE COALESCE(turn_id, '') <> '';
```

`role` 表示"属于谁的轮次侧"：`user`＝入站观察（含别人做的动作、系统事件），`assistant`＝本实例发出的回复。

另有一张**按需**才读的辅助表（不是每条消息都有行）：

```sql
-- 本轮计划内容：一轮一行（发送前抓到的正文），不是消息
SELECT umo, turn_id, conversation_id, content, content_kind,
       llm_status, send_status, persona_id, created_at
  FROM chat_memory_reply_messages WHERE deleted_at IS NULL;
```

「哪条消息属于哪一轮」不用单独的表：就是 `chat_memory_message_bots` 里
`role = 'assistant'` 那一行的 `turn_id`（一条消息只有一个发送方，天然唯一）。
用户消息反过来——被 N 个 BOT 看到就是 N 个轮次，所以 `turn_id` 只能是**每实例**的列。

### 13.2 `content` 是模板，读之前必须渲染

`content` 存的是**模板**，占位符指向 `relation_data`：

| 占位符 | 含义 | 渲染来源 |
|---|---|---|
| `⟦CM_AT:n⟧` | @某人 | `relation_data.mentions[n].nickname`（`all:true` 时是全体） |
| `⟦CM_IMAGE:n⟧` / `VIDEO` / `VOICE` / `FILE` / `EMOJI` / `FORWARD` | 媒体占位 | `relation_data.media[n]`（`kind` / `name` / `id`） |
| `⦑…⦒` | 用户自己打的尖括号（转义对） | 原样还原成 `⟦…⟧` |

实测分布（32,765 条中约 5,000 条带 token）：`⟦CM_AT:` 4,925、`⟦CM_IMAGE:` 112、`⟦CM_EMOJI:` 110。

**不渲染的后果**：模型会看到 `⟦CM_AT:0⟧` 这种内部标记，既不认识 At 对象，还会学着模仿。渲染逻辑在 `relation_codec.render_content_template()`，纯函数、无依赖，可以照抄实现；遇到**不认识的 token 建议计数告警**而不是静默吞掉。

### 13.3 消息表里没有「收/发」之分

`chat_memory_messages` 只记录**平台上真实存在的消息**，一条一行。谁发的看
`sender_id`，是不是本实例发出的看参与行的 `role`（`assistant`）——不需要一个
`origin` 列来区分（历史上有过，恒为 `inbound`，已删除）。

**发送前的计划全文不入消息表**：它没有平台事实，落在 `chat_memory_reply_messages`
（一轮一行，主键 `(umo, turn_id)`），只在"这一轮实际发送的分段缺失"时作为兜底显示。

### 13.4 `seq` 的语义（结构线 3 起）

- **会话内递增**：`(platform_name, message_type, session_id)` 各自一条序列，不是全局；
- **只增不减**：由 `chat_memory_session_seq` 表分配。会话被删空（`/reset`、保留期清理、孤儿回收）后**不会回到 1**，游标 `WHERE seq > :cursor` 永远向前；
- **允许空洞**：删除、并发重复观察都会留下空号，不能假设连续；
- **什么时候会重置**：只有影子层按"不认识的 schema_version"整体丢弃重建时，计数器才归零（`shadow_meta.shadow_migrate` 会记 `rebuild:unknown_version`；认识的旧版本走迁移，不丢数据）；
- **不要用 `rowid` 代替**：`chat_memory_messages` 是普通 rowid 表且没有 `AUTOINCREMENT`，删掉最后一行后 rowid 会被复用。

游标建议（会话维度）：

```sql
SELECT seq, msg_uid, content FROM chat_memory_messages
 WHERE platform_name = ? AND message_type = ? AND session_id = ?
   AND seq > ? AND deleted_at IS NULL
 ORDER BY seq LIMIT 500;
```

### 13.5 计划兜底行（合成记录）

一轮如果**没有**实际发到平台（被禁言/拒收/结果被清空），读路径会用 `chat_memory_reply_messages`
里这一轮的计划行兜底，合成一条记录：它的 `msg_uid` 是 `l:plan`、`record_id` 恒为 `0`。
外部消费者若要按 `record_id` 建索引，请把 `0` 当作"合成行"跳过——真实消息的 `record_id` 永远大于 0。

### 13.5.1 群名快照表 `chat_memory_group_names`（结构线 5 起）

```sql
CREATE TABLE chat_memory_group_names (
    platform_name TEXT NOT NULL,      -- 平台类型名（如 aiocqhttp），与 messages 同键
    session_id    TEXT NOT NULL,      -- 群号
    group_name    TEXT NOT NULL DEFAULT '',
    updated_at    DATETIME NOT NULL,
    PRIMARY KEY (platform_name, session_id)
)
```

**语义（读之前先确认这一点）**：

- 这是**群名的当前快照**，不是消息事实。一个群一行，值是最近一次从平台读到的群名。
- 消息表里**不存群名**。要用就按
  `messages.platform_name = group_names.platform_name AND messages.session_id = group_names.session_id` 关联；历史消息也能拿到名字（表按群存，不按消息存）。
- **不保存改名历史**：群改名会覆盖该行并更新 `updated_at`。要"当时的群名"请自己按时间增量采。
- **可能为空**：插件还没连上平台、或从未读取过群列表时，这张表没有对应行。缺行是正常状态，
  不是数据损坏——此时只能显示群号。
- 值由插件在读取群列表时刷新（加载后首次查询台请求、或平台的群列表有变化），不是每条消息都写。

### 13.6 幂等键

`msg_uid` 是稳定主键（平台键 `p:<platform>:<type>:<session>:<seq>`；无平台 id 的消息/事件用本地键 `l:<指纹>[:n]`）。跨实例去重请用 `msg_uid`，不要用 `(content, 时间)`。

### 13.7 版本与兼容

| 字段 | 说明 |
|---|---|
| `PRAGMA user_version` | 旧表 schema（当前 8） |
| `chat_memory_shadow_meta.schema_version` | 合并存储结构线（当前 6） |
| `chat_memory_shadow_meta.dual_write_started_at` | 双写起点（切读窗口用它算） |

外部读者应把 `schema_version` 存进自己的状态里：**值变化就当"结构可能变了"，重新校验列清单**；数值回退或变成不认识的值，说明有人在跑旧构建，此时表可能已被重建。

**结构线 6 的对外影响（会打断直接消费这两个字段的读者）**

- `query_history_v2` / `query_rounds_v2` 的返回 dict **不再包含 `reply_group`**（该列已删；同轮多段请按 `turn_id` 自行归组）。用 `.get("reply_group")` 的读者不受影响，用 `row["reply_group"]` 的会 `KeyError`。
- `chat_memory_messages` 不再有 `reply_group` 列；`chat_memory_reply_messages` 不再有 `instance_ts` 列、主键改为单列 `turn_id`（按会话取计划请用 `(umo, conversation_id)` 过滤，有索引）。
- `chat_memory_tool_calls` 的幂等键改为 `(turn_id, call_index)`；只读消费者无感，写入方（如果有）不要再依赖 `(umo, cid, turn_id, call_index)` 的冲突语义。
- 索引集合有增删（详见 `CHANGELOG` 的 2.0.0-beta 条目）。按索引名写死查询提示的读者要重新核对。

## 14. 版本

变更记录见 [CHANGELOG.md](CHANGELOG.md)。插件版本与数据库结构版本相互独立：`user_version = 8`（旧表）、`shadow_meta.schema_version = 6`（合并存储）。

结构线变迁（6）：工具表幂等键从 `(umo, cid, turn_id, call_index)` 收窄到 `(turn_id, call_index)`；计划表主键从 `(umo, turn_id)` 收窄到 `turn_id`；删掉只写不读的 `messages.reply_group` 与 `reply_messages.instance_ts`；删掉三条不会被命中的索引；计划表/工具表/元信息表补 `CHECK`。升级走 `SHADOW_MIGRATIONS`（累积应用），数据一行不丢，`messages` 的 `rowid` 保持不变（查询台的 `record_id` 契约）。
