# ChatMemory V2 外部读取契约

给**不 import AstrBot、不 import `chat_memory`、只用 `mode=ro` 打开数据库**的独立进程。
进程内消费者用插件实例的公开方法（见 README §9.3），本文只讲表契约。

- 适用结构线：`chat_memory_shadow_meta.schema_version = 6`（插件 2.0.x）
- 表全部带前缀 `chat_memory_`，与旧存档表（`chat_memory_records` 等，退役中）同库共存

---

## 0. 消费方式

```python
con = sqlite3.connect("file:.../plugin_data/chat_memory/chat_memory.db?mode=ro", uri=True)
```

1. 读 `chat_memory_messages` + `chat_memory_message_bots`（必要时加 `chat_memory_reply_messages`）；
2. 自己维护增量游标（见 §3）；
3. **不要**依赖查询台的临时表、`chat_memory_records`、或 `shadow_meta` 里本文没列的键——那些是内部面。

---

## 1. 稳定面与内部面

**稳定面**（可以依赖，改动会进 CHANGELOG 的兼容段）：

| 表 | 可依赖的列 |
|---|---|
| `chat_memory_messages` | `msg_uid`、`seq`、`platform_name`、`message_type`、`session_id`、`sender_id`、`sender_nickname`、`platform_timestamp`、`content`、`content_kind`、`relation_data`、`platform_seq`、`recalled_at`、`deleted_at`、`created_at` |
| `chat_memory_message_bots` | `msg_uid`、`platform_id`、`self_id`、`conversation_id`、`umo`、`role`、`turn_id`、`persona_id`、`target_id`、`llm_status`、`send_status`、`platform_message_id`、`raw_timestamp` |
| `chat_memory_reply_messages` | `turn_id`、`umo`、`conversation_id`、`persona_id`、`content`、`content_kind`、`llm_status`、`send_status`、`deleted_at`、`created_at` |
| `chat_memory_tool_calls` | `turn_id`、`call_index`、`tool_name`、`tool_args`、`tool_result` |
| `chat_memory_session_seq` | `platform_name`、`message_type`、`session_id`、`last_seq` |
| `chat_memory_shadow_meta` | `key`、`value`（键值表；本文用到 `key='schema_version'` 与 `'dual_write_started_at'`） |

**内部面**（不作为契约）：`fingerprint`（去重辅助）、所有 `id`/`rowid`、`chat_memory_records` /
`chat_memory_tool_records` / `chat_memory_media_archive`（1.x 旧表）、查询台的物化视图与 TEMP 表。

模型三条：**物理消息**（一行，跨实例唯一）／**参与行**（每个适配器实例一份视角）／**轮次**
（`turn_id` 把一条提问、它的回复、这轮工具调用绑在一起）。

---

## 2. `content` 是模板，读前必须渲染

`content` 里保留位置占位符，渲染信息在 `relation_data`：

```text
content       = '⟦CM_AT:0⟧看在亲爱的这么撒娇的份上…'
relation_data = '{"v":1,"mentions":[{"user_id":"10002","nickname":"小虎"}],"at_id":"10002"}'

content       = '[image]'                    -- 纯媒体消息直接存占位符
relation_data = '{"v":1,"media":[{"kind":"image","id":"…"}]}'
```

已知 token 前缀（与插件实现同源）：

```python
from chat_memory.relation_codec import KNOWN_CONTENT_TOKENS   # 需要能 import 插件时
# 进程内消费者走实例方法：cm.count_unrendered_tokens(text)
```

```text
⟦CM_AT:n⟧  ⟦CM_IMAGE:n⟧  ⟦CM_VIDEO:n⟧  ⟦CM_VOICE:n⟧
⟦CM_FILE:n⟧  ⟦CM_EMOJI:n⟧  ⟦CM_FORWARD:n⟧  ⟦CM_POKE:n⟧
```

- `n` 是 `relation_data.mentions` / `relation_data.media` 的下标，渲染该 token 时按下标取对应条目；
- **不认识的 `⟦…⟧` 不要吞掉**：建议按前缀计数告警（那说明 CM 升级了格式），并保留原文或替换成占位符，别把内部哨兵喂给模型（模型会照着模仿）；
- 用户正文里的 `⟦`/`⟧` 已被转义成哨兵字符 `⦑`/`⦒`，所以正文里出现的 `⟦` 一定是内部 token。

---

## 3. `seq` 是会话内序号，不是全局游标

分配方式：`chat_memory_session_seq(platform_name, message_type, session_id) → last_seq`
只增不减，新消息取它的下一个值写进 `chat_memory_messages.seq`。

1. **会话内递增**，不是全局；跨会话比较没有意义；
2. **允许空洞**：删除会留空号（实测长会话有数百个空号），只能 `seq > last` 扫描，不能假设连续；
3. **删除不会让 `seq` 回绕**（2.0 起）：会话被整段删空（`/reset`、保留期清理）后
   `last_seq` 保持原值，下一条消息继续往后编号——实测删空后新消息 `seq = 4`（历史值 3），
   不是从 1 重来；
4. **会整体归零的唯一场景是影子表按结构版本重建**（`DROP` + 重建）：这时所有会话的
   `seq` 从 1 重新开始；
5. **`rowid` 不能替代**：`msg_uid` 是 TEXT 主键、表无 `AUTOINCREMENT`，删掉最大行后可能复用。

### 正确做法：游标按“分区”维护

把**结构版本**（必要时再加读源）并入游标分区键，分区一变就重建基线（从当前位置重扫一遍，
不做跨分区增量）：

```sql
SELECT value FROM chat_memory_shadow_meta WHERE key = 'schema_version';
```

进程内消费者用插件实例的 `get_shared_store_status()`（返回 `source` / `schema_version` /
`dual_write_started_at` / `shadow_read_ready`），比直接读 meta 表更稳。

> **代际（epoch）暂不提供**：给会话加代际号需要动结构线（新表或新列 + 全部部署端迁移一次），
> 而 2.0 的 `seq` 已经不会因删除回绕，剩下能打破游标的只有“结构重建”，用上面的分区键就能
> 兜住——宁愿重扫一个分区，也不要静默漏读。将来有其它结构改动时会一并补上。

---

## 4. 判断“这条是不是 BOT 发的”“这轮发出去没有”

不要依赖任何“来源”字段（早期版本有 `origin`，2.0 已移除）。用参与行判断：

| 问题 | 判据 |
|---|---|
| 谁发的 | 消息表 `sender_id`（平台账号）；显示名看 `sender_nickname` |
| 是不是某个实例自己发的 | 该 `msg_uid` 在 `chat_memory_message_bots` 里存在 `platform_id = 该实例 且 role = 'assistant'` 的行 |
| 这一轮到底发出去没有 | 该 `turn_id` 存在 `role='assistant'` 的参与行 = 发出过。**没有任何 assistant 参与行**时，这一轮只活在 `chat_memory_reply_messages`（草稿行）里 |
| 一轮多段回复 | 同一 `turn_id` 的多条 assistant 参与行，按 `platform_seq` 升序拼起来 |

---

## 5. 一条消息在多个 persona 分区各可见一次是**预期**

物理消息只有一行，参与行是“每实例一份”。所以同一条 `msg_uid` 在
`query_rounds(persona_id=X)` 这类按 persona 取数的视角里，**X 参与的每一份都会各出现一次**
（同群 3 个适配器实例 → 最多 3 次）。这不是重复数据。

- 同一 `(msg_uid, persona)` **不会**重复参与（唯一键保证），消费方不需要按 `msg_uid` 去重；
- 需要“群里只消费一次”的消费方，请在分区键里去掉 persona。

---

## 6. `content_kind` 为空的记录在**白名单模式**下不可见

`content_kind` 是 JSON 数组（如 `["text"]`、`["image","text"]`）。空值是 `[]`：

- 不配白名单 → 空 kind 照常返回；
- 配了白名单 → **空 kind 视为不匹配、被过滤**（`all_match` 时必然不匹配）。

消费方遇到“过滤后为空”，应当理解为“这批没有可用类型信息”，而不是“CM 没数据”。

---

## 7. 草稿行不是“实际发出的分段”

`chat_memory_reply_messages` 一轮一行，正文来自发送前的装饰阶段。该阶段在 tool loop 里
**一轮会触发多次**（工具调用前后各一段），所以草稿正文是各段按序拼接（换行分隔）。

- 它是**兜底表示**：`chat_memory_messages` 里没有任何该轮实发段时才有意义；
- 真实分段边界、平台改写后的正文、送达状态，只有平台回显知道（写到物理消息表）；
- 读侧规则：**有实发段就用实发段，没有才回落草稿行**；
- 兜底有前提：只在该轮**已经有别的行落在本次查询范围内**时才会补草稿（这样用户/会话过滤沿用那一行的条件）。所以「只有草稿、没有任何参与行」的轮次（典型是主动消息没发出去）**不会出现**在结果里——实测占草稿行 0.4%。自行遍历 `chat_memory_reply_messages` 的读者不受此限，但要注意别把它当成实际发出的分段（见上）。

---

## 8. 撤回与删除

| 列 | 含义 |
|---|---|
| `messages.recalled_at` | 非空 = 该消息在平台上被撤回 |
| `messages.deleted_at` | 非空 = 查询台软删除（只在插件实时库生效） |

- 撤回标记是**消息级**的：三个实例看到的是同一条物理消息，一处撤回全体可见；
- 消费者做长期记忆/画像这类**落库**用途时，应当跳过 `recalled_at` 非空的行——否则撤回内容
  会被当成正常发言永久存进下游，事后无法与真实发言区分（进程内消费者用
  `skip_recalled=True`）；
- 事件行（禁言、成员变动、撤回、戳一戳、表情回应等）也在消息表里，`content_kind` 含 `event`，
  `relation_data.event` 给出子类。它们不是谁“说的话”，消费时自行决定是否排除。

---

## 9. 幂等键

- 物理消息用 **`msg_uid`**（平台键 `p:<platform>:<type>:<session>:<real_seq>` / 本地键
  `l:<指纹>[:n]`），它跨实例稳定；
- 不要用 `(content, 时间)` 或 `(sender, 时间)` 做键——同秒同内容的合法重复消息会被误并；
- 一轮的幂等键用 `turn_id`（uuid4，全局唯一）。

---

## 10. 自检清单

上线前逐条确认：

- [ ] 读的是 `schema_version = 6`（不是就用 §3 的分区逻辑重建基线）
- [ ] 拿到的 `content` 已渲染（`count_unrendered_tokens` 返回空）
- [ ] 游标带“结构版本”分区，不跨分区做 `seq > last`
- [ ] 用参与行判断收发与轮次，而不是任何“来源”字段
- [ ] 落库用途会跳过 `recalled_at` 非空的行
- [ ] 没有按 `rowid`、`fingerprint`、旧表做增量
