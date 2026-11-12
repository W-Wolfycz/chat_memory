# ChatMemory 2.0

把群聊和私聊的每一条消息存下来，再让你的 Bot 用这份存档当上下文。

从 1.x 的"每条消息按实例各存一份"，到 2.0 的"一条消息只存一份、三个 BOT 共享同一份事实"——这条合并存储的路拆过好几版设计、也踩过双写丢正文的坑，现在总算落地了。V2 上线。

- 版本 `2.0.0` ｜ 平台：NapCat（`aiocqhttp`）、`qq_official` ｜ AstrBot `>=4.16,<5`
- 仓库：<https://github.com/W-Wolfycz/chat_memory>

---

## 0. 前置配置：开启平台自身消息上报

CM 要记录**你自己说了什么**——分段回复的每一段、平台改写后的正文、这句话到底发没发出去——这些只有平台回显能给。发送那一刻，插件只知道"准备发什么"。

**怎么做**：

1. **NapCat 侧**：打开「上报自身消息」（`reportSelfMessage`，对应 OB11 的 `message_sent` 事件）。
2. **插件侧**：`sent_capture_history` 保持关闭（`false`）。它是"发送后去拉群历史"的兜底，只有回显用不了时才需要开。

**怎么确认生效**：启动日志里会有 `已订阅平台自身消息回显（platform=BOT1…）`，每个实例一条；查询台里能看到自己的回复分段和送达状态。一直订不上时插件会提醒你一次。

**不开会怎样**：存档照常（别人说的、事件、工具调用都不受影响），但 BOT 说过的话只剩"发送前的全文"——看不到分段，也判断不了那一句到底发没发出去。

---

## 1. 功能概览

| 你想要的 | CM 给你的 |
|---|---|
| 不丢聊天记录 | 每条消息即时落库：正文、发送者、时间、@/引用/转发、图片语音等形态、人格、所属轮次 |
| 多个 BOT 别重复存 | 一条群消息只存一份，三个 BOT 共享同一份事实，各自视角（谁发的、属于哪轮、什么状态）单独记 |
| 让 BOT 记得上下文 | 可选接管：CM 成为唯一上下文源，按轮取、跨会话/整群/按人格，重启也不丢 |
| 随时查历史 | 自带查询台：检索、工具调用、媒体回看、数据库体检 |
| 媒体别丢 | 图片/语音/视频/文件落盘，同一张图只存一份，按保留期与总量自动清理 |

---

## 2. 安装

1. 插件目录放到 `data/plugins/chat_memory/`，重载插件。
2. 首次加载自动建库：`data/plugin_data/chat_memory/chat_memory.db`。
3. 依赖见 `requirements.txt`（`sqlalchemy[asyncio]`、`aiosqlite`、`aiohttp`、`Pillow`、`quart`、`tzdata`）。
4. 按 §0 打开平台自身消息上报。

---

## 3. 从 1.x 升级

**`2.0.0` 是目前唯一「双写」的版本**（旧表和新表同时写）。它是个桥接版：

```
1.x  ──升级──▶  2.0.0（双写开始计时）  ──等 7 天──▶  勾选切读  ──确认没问题──▶  后续版本（单写）
```

- **不要跳过 2.0.0 直接升后续版本**：后续版本会停写旧表，而"双写满 7 天"这个窗口只有 2.0.0 能积累。跳过就永远凑不满条件，新表里也只是一段不完整的历史。
- **推荐等切读生效（≥7 天）再往后升级**：先用旧表兜着跑几天，确认新表数据完整、查询台显示正常，再勾 `shadow_read`；切读稳定后再升后续版本。
- 升级是自动的：加载时检测结构版本并迁移，**数据一行不丢**，消息行号与工具调用 id 都保持不变。
- 双写期间磁盘占用会翻倍（两份数据），7 天后切读、后续版本停写旧表就会回落。

详细信息见 [CHANGELOG](CHANGELOG.md) 的 2.0.0 段。

---

## 4. 常用配置示例

**只想存档，不碰上下文**（最省心）
```text
context_takeover.enable = false
shadow_read = false
```

**让 CM 当唯一上下文源**（私聊/群聊各自记忆）
```text
context_takeover.enable = true
context_takeover.clear_native_history = true     # 别让两套上下文打架
context_takeover.limit_rounds = 30
context_takeover.llm_status_filter = ["llm_success"]
```

**群聊整群感知**（Bot 知道群里大家在聊什么）
```text
context_takeover.enable = true
context_takeover.full_group = true
context_takeover.include_content_kinds = ["text", "image", "emoji", "event"]
context_takeover.limit_rounds = 60
```

**按人格隔离**（同群多个 BOT，各记各的）
```text
context_takeover.filter_by_persona = true
```

**跨会话，但群聊不串味**（只在私聊里想起这个人在别处说过的话）
```text
context_takeover.cross_session = true
context_takeover.cross_session_private_only = true
```

**媒体归档**：`media_archive.enabled = true`（默认），保留期 `retention_days = 30`、总量上限 `max_total_mb = 2048`。视频默认不归档（`include_video = false`）。

完整配置项（含说明与默认值）见文末 §9.1。

---

## 5. 查询台使用

AstrBot → 插件 → ChatMemory，三个页面：

- **数据概览**：记录/会话/用户/工具/媒体计数、近 30 天活动、状态分布、数据库体检。
- **记录查询**：8 个筛选 + 关键词；卡片上直接标出内容类型、群聊与否、是否已撤回；高级模式还能按 persona 收窄、看到"这一轮配没配上"。
- **工具调用**：Bot 调过哪些工具、参数与结果、属于哪一轮。

三种模式：只读（默认）/ 高级只读 / 删除（软删除与恢复，只在插件实时库可用）。数据源可以切成只读副本，切走后写操作自动禁用。

---

## 6. 记录内容

一句话：**一条物理消息一行**，另记"每个 BOT 是怎么看待这条消息的"。

- **消息本身**：正文、发送者与显示名、平台时间、@/引用/转发、媒体形态、是否被撤回/删除。
- **每个 BOT 的视角**：谁发的（`role`）、属于哪一轮、这一轮的 LLM 状态与发送状态、哪个 persona 处理的。
- **一轮回复可以是多段**：同一轮的多段回复会按平台顺序拼回一句（查询台与上下文都是合并后的样子）。
- **事件也有记录**：禁言、成员变动、撤回、戳一戳、表情回应等作为"事件行"存在，不当成谁说的话。

旧的 1.x 表还在写，但只为"切读回退"服务；新功能一律用新表。

---

## 7. 常见问题

**自己的回复在存档里显示不全 / 只有一段？**
先看 §0 的回显有没有开；启动日志里没有"已订阅…回显"就是没开。

**勾了切读，为什么没生效？**
双写没满 7 天。启动日志会写还差几天；门槛是全局规则，勾选开关本身不绕过它。

**查询台空白 / 打不开？**
检查插件实时库路径是否存在；数据源被切成别的只读副本时写操作会禁用（只读仍可查）。

**上下文很长、回复变慢？**
调低 `context_takeover.limit_rounds` 或设 `max_context_chars`；群聊整群模式（`full_group`）消息量大，建议从 60 轮起试。

**BOT 记不住自己刚说过的话？**
看查询台里那段回复有没有"已配对"标记；没有的话多半是回显没到（§0），或那一轮在发送后 60 秒内还没认领到轮次。

---

## 8. 已知限制

1. **切读只覆盖双写之后**：切读后看不到 2.0.0 装上之前的历史（查询台仍能看到旧表段，两侧口径不同）。
2. **自己发言的形态依赖回显**：宽限内没认领到轮次的段只写物理行，这一轮会退回"计划全文"。
3. **上下文窗口是"最近 N 轮"**：超限后窗口左移，对模型侧的前缀缓存不是最优（改成块对齐窗口是下一步）。
4. **旧表还没删**：2.0.0 双写，后续版本才停写并移除。

---

## 9. 附录

### 9.1 完整配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_content_length` | int | `0` | 单条正文落库上限（0＝不截断） |
| `auto_cleanup_days` | int | `0` | 自动清理阈值（0＝不清理） |
| `log_with_bot_id` | bool | `false` | 日志前缀加 `[platform:{platform_id}]` |
| `shadow_read` | bool | `false` | 切读开关（受 7 天门槛约束） |
| `sent_capture_history` | bool | `false` | 发送后拉群历史（回显的兜底） |
| `context_takeover.enable` | bool | `false` | 接管上下文 |
| `context_takeover.cross_session` | bool | `false` | 跨 UMO 取数 |
| `context_takeover.cross_session_private_only` | bool | `false` | 只在私聊里跨会话（群聊只读当前群）；勾了 `cross_session` 才显示 |
| `context_takeover.cross_session_blacklist` | list | `[]` | 黑名单 `G:<群号>` / `F:<用户号>` |
| `context_takeover.full_group` | bool | `false` | 整群取数（不限当前用户） |
| `context_takeover.limit_rounds` | int | `30` | 取多少轮（只选 `llm_success` 时按轮，否则按消息条数） |
| `context_takeover.max_context_chars` | int | `0` | 上下文字符上限（0＝不限） |
| `context_takeover.llm_status_filter` | list | `["llm_success"]` | 取哪些 LLM 状态（`["no_llm"]`＝空值） |
| `context_takeover.include_content_kinds` | list | `["text"]` | 内容白名单 |
| `context_takeover.event_kinds` | list | 全部 8 类 | 事件子类白名单 |
| `context_takeover.recall_skip_context` | bool | `false` | 撤回消息不进上下文 |
| `context_takeover.include_all_match` | bool | `false` | 白名单全含 / 任一 |
| `context_takeover.clear_native_history` | bool | `true` | 清原生历史 |
| `context_takeover.fallback_to_native_on_empty` | bool | `false` | 取不到时回退原生历史 |
| `context_takeover.filter_by_persona` | bool | `false` | 按当前 persona 过滤 |
| `context_takeover.keep_tool_turns` | int | `2` | 工具段回放轮数 |
| `media_archive.enabled` | bool | `true` | 归档开关 |
| `media_archive.include_video` | bool | `false` | 归档视频 |
| `media_archive.retention_days` | int | `30` | 保留天数 |
| `media_archive.max_total_mb` | int | `2048` | 归档总量上限 |

### 9.2 表结构（合并存储，结构线 6）

| 表 | 粒度 | 关键列 |
|---|---|---|
| `chat_memory_messages` | 一条物理消息一行 | `msg_uid`(PK)、`seq`、`platform_seq`、`content`(模板)、`content_kind`、`relation_data`、`recalled_at`、`deleted_at` |
| `chat_memory_message_bots` | 每实例参与行 | 唯一键 `(msg_uid, platform_id, self_id, conversation_id)`；`role`、`llm_status`、`send_status`、`persona_id`、`target_id`、`turn_id`、`platform_message_id` |
| `chat_memory_reply_messages` | 一轮一条计划 | `turn_id`(PK)、`umo`、`conversation_id`、`content`、`content_kind`、`llm_status`、`send_status` |
| `chat_memory_tool_calls` | 每次工具调用 | 唯一键 `(turn_id, call_index)` |
| `chat_memory_session_seq` | 会话序号计数器 | `(platform_name, message_type, session_id) → last_seq`，只增不减 |
| `chat_memory_group_names` | 群名快照 | `(platform_name, session_id) → group_name` |
| `chat_memory_shadow_meta` | 结构元信息 | `schema_version` / `dual_write_started_at` / `shadow_migrate` |

概念三条：**物理消息**（一行）／**参与行**（每个 BOT 的视角）／**轮次**（`turn_id` 把提问、回复、工具调用绑在一起）。

### 9.3 对外查询 API（Python）

```python
cm = context.get_registered_star("chat_memory")
rows = await cm.query_history(umo, cid, user_id="10001", limit=20,
                              llm_status=["llm_success"], content_kind=["text"])
rounds = await cm.query_rounds(umo, cid, user_id="10001", limit_rounds=10)
# → [[user_dict, assistant_dict], ...]，assistant 侧是合并后的一轮回复
```

| 方法 | 读源 |
|---|---|
| `query_history` / `query_rounds` | 跟随 `shadow_read`（含 7 天门槛） |
| `query_history_v2` / `query_rounds_v2` | 强制合并存储（对拍/灰度用） |
| `build_takeover_contexts` | 复用接管配置拼上下文（不写库、不清原生历史） |

注意：`since`/`until` 按 UTC naive 解释；返回的 `created_at` 是配置时区的 naive 字符串，做游标请用 `created_at_utc`。2.0 起返回 dict 不再有 `reply_group`（按 `turn_id` / `segments` 归组）。

### 9.4 查询台 Web API

前缀 `/api/plug/chat_memory/`：`about`、`snapshot`（锁定当前库快照）、`settings`(GET/POST)、`health`、`overview`、`facets`、`query`、`record`、`soft-delete`、`restore`、`tools`、`group-names`、`personas`、`media/<id>`、`export`、`export/<file>`。除软删除/恢复/导出/保存设置外全只读。

### 9.5 钩子与写入时机

| Hook | 优先级 | 用途 |
|---|---|---|
| `event_message_type(ALL)` | 2000 | 学平台实例、确认自身消息回显订阅 |
| `event_message_type(ALL)` | 1000 | 入站消息 / 事件通知捕获 |
| `on_llm_request` | 默认 / `-100` / `-1000` | 补捕获、升 `llm_pending`、接管上下文、当前轮焦点 |
| `on_llm_tool_respond` | — | 工具调用落库 |
| `on_decorating_result` | 10000 | 写计划行、记发送侧账本 |
| `after_message_sent` | — | 标记 `send_attempted` |

自己的消息靠 **push 回显**（主）与 **pull 拉群历史**（兜底）进入合并存储；撤回通知先于消息落库时按 0.5/1.5/3/8 秒退避重试。

### 9.6 外部只读读者契约

给只用 `mode=ro` 打开的独立进程：

```sql
SELECT msg_uid, seq, platform_name, message_type, session_id, sender_id, sender_nickname,
       platform_timestamp, content, content_kind, relation_data, platform_seq,
       recalled_at, deleted_at, created_at
  FROM chat_memory_messages WHERE deleted_at IS NULL ORDER BY seq;

SELECT msg_uid, platform_id, self_id, conversation_id, umo, role, platform_message_id,
       turn_id, llm_status, send_status, persona_id, target_id, raw_timestamp
  FROM chat_memory_message_bots WHERE COALESCE(turn_id, '') <> '';
```

- `content` 是**模板**：`⟦CM_AT:n⟧` 等占位符要按 `relation_data` 渲染（`relation_codec.render_content_template()` 是纯函数，可照抄）；不认识的 token 建议计数告警。
- 消息表没有"收/发"之分：谁发的看 `sender_id`，是不是本实例发的看参与行 `role`。
- `seq` 只增不减、**允许空洞**（删除与并发观察会留空号）；不要用 `rowid` 代替。
- 幂等键用 `msg_uid`，不要用 `(content, 时间)`。
- `schema_version` 变化就当结构变了，重新校验列清单。

### 9.7 版本与结构

- 插件版本 `2.0.0`；合并存储结构线 `6`；旧表 `user_version = 8`（退役中）。
- 结构变更走 `SHADOW_MIGRATIONS` 累积迁移，不丢弃重建；切读窗口 `dual_write_started_at` 随迁移保留。
- 后续路线：停写旧表 → 删旧表与旧索引 → 去掉 `shadow_read` 开关与 7 天门槛 → 去掉查询台的双 id 偏移（`record_id = rowid + 1e9`）。
