# Changelog

ChatMemory 在 `1.0.0` 前均视为内部测试版。以下版本号是对原开发历史的重新压缩，不对应旧仓库曾使用的版本号；数据库 schema 版本独立维护，当前为 `8`。

> 下方各版本记录的是该版本发布时的实际格式。旧条目中的 `[当前发言者]`、`<cm s="N"/>`、`<cm_history_tail/>` 等均为已经退役的历史协议，不代表当前版本仍会生成或接受这些标签；当前格式以最新版本条目和 README 为准。

## 2.0.0-beta — 2026-11-01

> GitHub 预发布（`2.0.0-beta`，pre-release）：整批改动＝**一次 change**（合并存储首次发布、内部结构线从 `1` 起算；媒体归档表 schema 7 → 8；配置默认值；删两张派生表与旧库兼容字段一起改，不做分批迁移）。合并存储**默认只写不读**（双写强制启用），旧表与现有读路径完全不变；切读按配置启用，且要双写满 7 天才生效。
>
> **运行前提（本版明确声明）**：`chat_memory_messages` 等合并存储要**满效率**工作，需要 **NapCat 上报自身消息（`reportSelfMessage` / `message_sent`）**。满足时自己发出的每条消息秒级到货（平台号 + 平台侧正文）；不满足时退化为"装饰时的计划全文"，或由可选的 pull 兜底。

### 新增
- **多 BOT 合并存储（双写，强制启用）**：除旧表照旧写入外，同一批数据再镜像到 `chat_memory_messages`（消息本体，主键 `msg_uid`）与 `chat_memory_message_bots`（每 BOT 视角与状态：cid、role、llm_status、send_status、turn_id、persona、target_id、本地接收时刻），工具调用镜像到 `chat_memory_tool_calls`。影子写失败只累加计数并打一条告警（异常串可能带正文，只记异常类型），**绝不影响存档**。
- **跨实例归一键**：`msg_uid` 优先取平台共享 id（NapCat OB11 事件 `real_seq`＝服务器 `msgSeq`，各 BOT 算出同一个值）——形如 `p:{平台类型}:{会话类型}:{会话id}:{real_seq}`，**不哈希、可读**；客户端不提供时退化为内容指纹 + 同秒出现序号（`l:{md5}:{n}`）。平台号同时作为 `platform_seq` 落列，**段序一律用它**（实测按平台号排序与平台历史顺序 158/158 一致）。
- **自己发言的供货：push 主 + pull 兜底**。push＝插件直接订阅 OneBot 适配器事件总线上的 `message_sent`（不经 AstrBot 流水线、不碰 `ignore_bot_self_message`、无自回复风险），**恒开（无开关）**；pull＝发送后调 `get_group_msg_history` 把自己刚发的消息补回来，**默认关**。两者写的是**同一条平台行**（同一 `real_seq`），**存在即丢弃**——pull 因此不需要任何专属字段（没有 part_index、没有单独的 origin）。
- **轮次归属＝发送侧记账（内存）**：装饰完成时记 `(会话 → turn_id, cid, 开始时刻)`，发送流程结束时打时间戳；回显到达时归到该会话"最近一轮"（关闭后另有 60 秒宽限，覆盖多段陆续到齐）。同一实例的发送串行，所以**不需要**"正文相等/互含 + 时间窗"的配对启发式，也不需要为它设计字段。覆盖不了的回显（迟到 / 非本实例发送）只落物理行。
- **多 BOT 观察＝message 表的额外写入方式（不是通道）**：同一进程里每个实例都会把自己收到的消息写进 message 表——同一 `real_seq` 落到**同一行**，每个实例各写一份参与行（`role` 是各自的视角：BOT1 的发言对 BOT1 是 assistant、对 BOT2 是 user）。它不参与配对、不写逻辑表、不改组；"这条发言是不是某个 BOT 发的"由 `sender_id` 派生即可。
- **逻辑回复表 `chat_memory_reply_messages`（轮次成员表）**：只表达"这条物理消息属于哪一次逻辑回复"，外加这一轮的计划成员行——`msg_uid` 非空＝真实消息成员，`msg_uid IS NULL`＝计划成员（发送前抓到的正文与轮次状态，一轮一条，部分唯一索引 `ux_cmrm_plan` 保证）。谁看到了由 `message_bots` 的参与行表达；没有 `relation`、没有 `part_index`、没有 `instance`。组（`reply_group`）由 `turn_id` 派生（`rg:<turn_id>`），认领时写入，所以"一次回复一个组"是结构性成立。
- **自身消息回显的订阅与自愈**：插件在 `initialize()` 直接起巡检任务（前 10 轮每 2 秒、之后每 60 秒），平台实例一就绪就订阅；订阅以"总线订阅表里有没有我们的回调"为准（幂等标记会因平台重载换总线而失真）。巡检结果按 DEBUG 记录（实例数、就绪客户端数、已订阅数、pull 开关、客户端类型），**连续几轮一个实例都没订上时明确告警一次**："未订阅到任何平台自身消息回显（push）……请确认 NapCat 已启用自身消息上报"。
- **媒体归档按内容去重**：归档文件不再用 `media_id` 命名，改为**内容寻址** `<sha256 前 32 位>.<ext>`（归档行新增 `content_hash`，旧表 schema `7` → `8`）。多 BOT 各自归档同一张图时平台 file id 不同、字节相同 → 落到**同一个文件**，磁盘上只留一份；归档行仍是一行一条（各留自己的来源与时间），删除改为**引用计数**（还有别的行在用就不删，否则会删掉别人的图）；总量上限按**文件**计（同一内容只算一次，不按行重复累加），配额清理也只在删掉某个文件的**最后一个**引用时才把它算作腾出空间——否则会把还不需要删的归档行删掉。老库文件由每日清理滚动迁移（每轮 1000 行、单轮读取上限 1 GB，按 `content_hash IS NULL` 断点续跑）：改名 + 顺手删掉重复文件，迁移完即空转；文件已不在的行标成"无需迁移"（空哈希），否则它们会一直占着待迁移集合的前排、把后面要改名的行永久堵死。
- **双写强制启用 + 切读门槛 7 天**：本版安装后自动双写、**没有配置开关**（合并存储随本版首次发布；起点＝装上本版后第一次加载那一刻，重启不重置）；双写起点记在 `chat_memory_shadow_meta.dual_write_started_at`。切读**必须等双写跑满 7 天**才生效，没满之前勾了也不生效，启动日志写明还差几天；审阅脚本打印「双写窗口：N 天（门槛 7 天）」。单写（旧表退役）留到下一个版本。
- **切读开关 `shadow_read`（单个 checkbox）**：勾上后**接管上下文与公开查询 API 一起改读合并存储**（模型看到的是实际发出的分段拼接；方法名与返回形状不变），工具调用段与 Reply 引用目标跟随同一读源（不能一半新一半旧）。读失败自动回退旧表（前 3 次 WARNING、之后 DEBUG，回退次数在内存里计数用于限流）。显式 `source="v1"|"v2"` 与 `query_*_v2` 是程序化对拍入口，不受开关限制。
- **切读后同一句回复不重复**：消息级 V2 读路径也按轮合并 assistant（计划行让位、各段按平台号拼接，来源见 `segments`）——影子层一轮有「计划行 + 各段实际形态」多行，不合并的话混合模式（状态白名单含空串）会把同一句回复重复塞进上下文，而 V1 那边一轮只有一行。
- **V2 读接口（合并存储只读，V1 保留）**：新增 `query_history_v2` / `query_rounds_v2` / `build_takeover_contexts_v2`，参数与返回 dict 与 V1 同形（额外带 `msg_uid`/`platform_seq`/`reply_group`/`origin` 等影子层字段），数据来自合并存储；`build_takeover_contexts(..., source="v2")` 可切读源。`query_rounds_v2` 的 assistant 侧是**合并后的一轮回复**：该轮实际发出的段按平台号顺序拼接（群里看到的样子，`segments`/`segment_count` 给出拼接来源），完全没有实际形态时才回落计划全文行。开关关着时 V2 明确报错，不返回空列表假装没数据。
- **查询台软删除双写**：软删除/恢复除旧表外，同步写合并存储的 `chat_memory_messages.deleted_at` 与 `chat_memory_tool_calls.deleted_at`（按平台 mid 或 `(umo, cid, turn_id)` 定位同一条消息）。不双写的话，切读之后被删的记录会"复活"。
- **回显写进来的自己发言带上 persona**：`message_sent` 事件本身没有 persona，改由发送侧记账一并携带；加载与每日清理时按"同一轮别的行有、这一行空着"就地回填历史空缺（线上 111 行 / 47 轮），否则按 persona 过滤的读路径会整段看不到。

### 变更
- **群名不再只活在内存里**：新增 `chat_memory_group_names` 快照表（结构线 `4 → 5`，只建表、不动消息数据），按 `(platform_name, session_id)` 存最近一次读到的群名（与消息表同键，可直接 join）；查询台与离线/快照库都能把群号翻成名字。消息表不存群名、不保存改名历史，语义写进 README §13.5.1；值没变时不产生写入。顺带删掉与唯一键完全重复的 `ix_cmtc_turn`，`ix_cmm_reply_group` 改成部分索引（NULL 占 79.6%）。
- **轮次表只留计划内容**：`chat_memory_reply_messages` 原有的消息成员行（7,139 行）里
  `content`/`relation_data`/`llm_status`/`send_status`/`deleted_at` **全部为空**，读路径
  也只读计划行。这些行直接删除（连同迁移时 467 条指向已回收消息的孤儿行）："哪条消息属于
  哪一轮"已由参与行的 `turn_id` 表达——一条消息只有一个发送方，`role='assistant'` 那行
  天然唯一；用户消息则相反，被 N 个 BOT 看到就是 N 个轮次，只能是每实例的列。
  `link_owner_turn` 改查 `chat_memory_message_bots`。
- **再去三列冗余**：`chat_memory_messages.origin`（恒为 `inbound`，读侧无人使用，
  "谁发的"看 `sender_id`、"是不是本实例发的"看参与行 `role`）；`chat_memory_reply_messages`
  的 `msg_uid`（拆行后恒为 NULL）与 `relation_data`（3,075 行全空）。计划表主键改为
  `(umo, turn_id)`（一轮一条计划），条件唯一索引与三处 `msg_uid IS NULL` 过滤一并去掉。
- **查询台一次取数要等几秒**：合并读取把「v2 投影（每实例挑一行的窗口函数）+ 旧历史去重」的 UNION 在**每条查询里重算一遍**，一次页面要跑十几次、总计 4~5 秒。现在把它按库指纹物化成 TEMP 表（`_cm_merged_rows`，带 `created_at`/`role` 索引）：建一次 ~0.6 秒，之后取一页 76ms（原 770ms）、概览 302ms 且 30 秒内命中缓存、facets 58ms；库或旧历史缓存一变指纹就变、下次读自动重建，不会读到过期数据。读连接同时开了 `cache_size=64MB` 与 `mmap_size=256MB`。
- **概览重复扫描**：`role` 与 `llm_status` 原来各扫一遍合并源，现在一次二维分组求和还原；软删除计数并进 summary 的同一次扫描（省掉一次全量扫）。
- **`personas` 计数按消息去重**：参与行是"每实例一行"，同一条消息被 N 个 BOT 看到会算 N 条；
  改为 `COUNT(DISTINCT msg_uid)`，与"这个 persona 有多少条记录"的直觉一致。
- **补齐按时间扫描的索引**：新增 `chat_memory_messages(created_at)` 与
  `chat_memory_message_bots(created_at)`——撤回重放、保留期清理、双写窗口起点都在按时间扫，
  之前每次启动/日清都是全表扫加排序。结构线 `3 → 4`，认识的旧版本走迁移（只建索引，不重建、
  不重置切读窗口）。
- **删掉两张派生表：`chat_memory_send_intents` 与 `chat_memory_conversation_bots`**。轮次（消息意图）不再单独建表，也不再维护 `observed_parts` 之类的计数器——"这条消息属于哪一轮"就是链接表里的一行。会话×实例同样不再建表：多 BOT / 单 BOT 与"兄弟 BOT 账号"都由参与行派生。影子表因此从 13 张收敛到 **9 张**，最终收敛为 **6 张**（见「兼容」末条）。同时清掉旧库的兼容/渲染字段：参与行不再复制 `pair_id`/`reply_id`/`forward_id`，消息表不再存 `at_id`（`relation_data` 完整覆盖）。
- **逻辑表按 persona 视角**：链接行带 `persona_id` 与 `instance_ts`（**写入方自己的时刻**）。平台时间跨实例一致、只用于归一键；persona 视角的先后一律用各自的时刻。
- **转发消息（合并转发）口径固化**：只落占位符 `content='[forward]'` + `content_kind=['forward']`，LLM 上下文渲染成「转发消息」、查询台渲染成 `⟦转发消息⟧`；节点内容不展开入库（留到「媒体内容表」阶段），调研结论记入 `docs/PROJECT_CONTEXT.md`。
- **撤回标记会永久丢失（通知早于落库）**：撤回镜像按 `(umo, platform_message_id, role='user')` 当场匹配，而通知常早于落库 → 线上 38/62 次没打上标记。现在加载时与每日清理各**重放**一次撤回事件行（撤回通知本身就是一条带 `relation_data.event.type=recall` 的真实事件行，近 30 天）：仍有行没标上就补标，已标过的不重复处理——不再需要一张"异常表"来记住谁没对上。
- **跨会话提示词以当前会话为主**：`cross_session` 注入的来源规则改为"当前会话是主场，其他来源只作背景参考"。

### 修复
- **自己发言的昵称会一直是 QQ 号**：回显（push）与拉取（pull）两条通道写消息行时都不带昵称，只靠"抄同会话最近一条非空"补；BOT 单独在某个群里、没有别的实例观测它时永远抄不到，这行的 `sender_nickname` 就固定成号码。现在**写行前主动查一次**：群聊用 `get_group_member_info`（名片优先、退回 QQ 昵称）、私聊用 `get_stranger_info`，直接写进这一行（**不回溯修改历史行**，名字是写入当时的快照）；取名优先级**按会话类型分档**：群聊「群名片 → QQ 昵称 → 登录昵称 → QQ 号」，私聊/临时会话「**好友备注** → QQ 昵称 → QQ 号」，私聊不取群名片（群名片只属于群维度）。备注来自 `get_friend_list`，6 小时缓存。号码只作最后一档，上下文里替换成「未知成员」，账号不进 LLM。原来那条「按会话回填空/号码行」的通道已删除，历史行一律不动。临时会话的来源群（`sub_type`/`temp_group_id`/`temp_source`）记进 `relation_data`，暂不消费。事件行（自己的贴表情/戳一戳、别人的名片变更等）原来写的是登录昵称，现在也走同一条「群名片优先」查询；走 pipeline 的自身消息回显同样处理。结果缓存 6 小时，收到群名片变更通知立刻作废缓存；DEBUG 日志会打出接口返回的 `card` 与 `nickname` 原值，便于确认某个群到底有没有设名片。查询失败只记异常类型，不影响任何发送路径。
- **号码昵称会「传染」**：`lookup_sender_nickname`（出站行补名）原来只过滤空串，会把旧通知事件写下的"昵称=号码"当名字抄进后续行。现在排除这类候选，斩断传染链。
- **回显被静默丢弃时没有任何痕迹**：`_capture_push_echo` 的四条丢弃分支（post_type 不符 / 缺平台共享 id / 会话类型或 id 缺失 / 会话尚无记录）原来一律静默 `return False`，排障只能靠"没有成功日志"反推。现在各留一条 DEBUG（带 `platform_id` 与原因）；回显的 `session_id` 也不再回落到发送者本人（那会把消息记进一个不存在的会话）。
- **回显巡检一次异常就永久停摆**：`_echo_subscribe_loop` 原来没有外层兜底，`_poll_echo_subscriptions` 抛一次异常任务就结束。现在按轮捕获并记 WARNING，下一轮继续。
- **详情页昵称与列表不一致**：同一行在列表里显示补出来的昵称、点进详情又变回号码——`_record_query` 现在也走与列表相同的昵称补全。
- **引用原文二次转义**：turn 分支正文来自已渲染记录（`&` 已是 `&amp;`），又被转义一次成了 `&amp;lt;`；现在只有快照分支的原始正文补转义。
- **`media_kinds` 形参被静默忽略**：只赋值不读取，改它没有任何效果；现在真正决定哪些媒体形态补占位（传全部媒体 kind 时行为不变）。
- **计划标记污染健康度**：`mark_plan_send_attempted` 更新 0 行（这一轮有实发消息，属正常）原被记进 `shadow_stats.skipped`；现在不再计入"数据不足被跳过"。
- **勾了 `event` 仍看不到旧事件行**：1.5.0 前旧 `content_kind` 是 `system_event`，白名单只放行字面值 `event`，历史事件在上下文里被静默排除。现在勾 `event` 一并放行旧值。
- **查询台内部错误把异常串回给前端**：可能带出绝对路径/SQL 片段；改为固定文案，细节只进服务端日志。
- **空上下文 + 字数上限会抛 IndexError**：`normalize([], max_chars>0)` 取 `formatted[-1]`，当前唯一调用方已提前返回，但构造器对外公开；现在先判空。
- **`terminate` 未取消登录昵称任务**：重载后悬挂任务继续跑；现在随卸载一起取消。
- **计划兜底不看时间窗与 persona**：`_fetch_plan_members` / `_fetch_turn_replies` 只按
  `turn_id` 取，窗口之后才生成的计划全文、或别的 persona 的回复段会被补进上下文（V1 的时间窗
  同时作用于两侧）。现在两条路径都套 `since`/`until`/`persona`/撤回过滤。
- **查询台「未标记 persona」选不动**：该选项值是空串，前端 `value || undefined` 会把它吞掉，
  选了等于没筛、还静默退出高级模式。改用哨兵值 `__empty__` 原样发送（后端本来就认）。
- **下拉改动只刷候选不重查**：改聊天类型/内容类型/事件子类/删除状态后列表停在上一次结果；
  现在两者都做，页码也改绑了重查。
- **详情字段表显示 `[object Object]`**：对象/数组值跳过，只列标量。
- **请求失败报错不可读**：401/网关返回 HTML 时前端只会抛 `Unexpected token '<'`，
  现在按 HTTP 状态给出可读错误。
- **`<cm_unsent/>` 整条机制不可达**：判断"这一轮有没有实发消息"时用的是旧表 UPDATE 的 rowcount，而计划成员一定伴随发送前的 prepared 行 → rowcount 恒 ≥1，那条分支永远进不去。改为 `after_message_sent` 无条件调用计划标记、由影子侧"有没有 assistant 实发行"决定；并加 180 秒宽限期，避免把"回显还没到"的已送达轮次误标成未送达。
- **查询台删了还在**：`keep_open` 连接把旧历史去重结果缓存在 TEMP 表（含 `deleted_at`），软删除/恢复后不重建 → 页面读到删除前的快照、再点还报"已被软删除"。现在写成功后标记缓存失效并重建（DROP 也放进 `query_only=OFF` 区间，否则只读 pragma 会把 DROP 挡掉）。
- **账号被写进昵称列**：notice 事件的适配器兜底昵称就是 QQ 号，写路径原样落库后会被渲染成 `<cm_nickname>` 进上下文。现在纯数字昵称一律不落库（读侧本来就过滤，这次补齐写侧）。
- **登录昵称取数无超时**：`get_login_info` 在 aiocqhttp 下 `api_timeout` 是 180 秒，而它在回显巡检主循环里 `await`，平台连上未就绪会把巡检卡三分钟。现在 5 秒硬超时，缓存按 uid 合并（单实例失败不丢已知昵称）。
- **配对查询返回轮数偏少**：`fetch_rounds` 先按 `limit_rounds` 取 user 消息、再丢掉"没有回复"的轮次，群里 bot 只回一部分消息时轮数远小于配置。现在先超取再裁，与 V1 的 LIMIT 语义对齐。
- **BOT 回复被折进用户消息**：发送路径的事件属于触发它的用户消息，其 `real_seq` 一度被当作回复的平台 id（线上 777 条回复 + 777 条 user 状态受影响）。现在发送路径不采用平台共享 id，出站行只走内容指纹本地键。
- **观察侧分段漏归组**：出站行的消息时间此前取"触发它的那条用户消息"的时间，锚点比真实发送时刻早 20~30 秒（297 条观察副本里 103 条漏归组）。现在出站行时间＝发送尝试时刻。
- **push 回显整条通道静默丢弃**：回显入库时没带归一必需的接收时刻，`normalize_record` 整条拒收，而写入包装无论落没落库都返回真 → 通道上报"入库成功"、DB 里一行没有（`origin='echo'` 一行不落）。现在回显带接收时刻（平台时间优先，缺则回落本地接收时刻），`insert(shadow_only=True)` 如实返回是否真的落库。
- **一次回复出现两个 `reply_group`**：回显行在 message upsert 阶段会先把自己当锚点，配不上轮次时永久留在自己的组里（线上 176 轮里 90 轮两组）。现在**组只由计划行创建**、实际形态到货时并进来（并组只搬"这一行自己的锚组"），并加审阅脚本 ⑨ 单组不变量硬门槛：本次构建启动后写入的轮次多组数必须 0。
- **已删除的历史表留在库里**：结构升级的"丢弃重建"只删当前表清单，一张表从代码删掉后名字也从 drop 清单消失 → 旧库永远留着无人维护的结构（线上留了 314 行 `send_intents` + 10 行 `conversation_bots`）。现在有一份"已废弃表"清单，每次加载无条件 `DROP TABLE IF EXISTS`。
- **重载后回显订阅"看起来订好了、其实收不到"**：`EventBus` 不随插件卸载退订，重载后旧实例的回调仍挂在平台上并按**老代码**处理。现在每个实例持有归属 token：订阅前先摘掉别人的回调，`terminate()` 先摘自己的。
- **同一条消息被两轮认领（轮次串了）**：自己发出的一条消息先被 push 回显按发送侧记账认领进甲轮，几秒后乙轮的 pull 回看窗口又把它捞回来 → 同一 `msg_uid` 挂两次归属，参与行的 `turn_id` 也被覆写成乙轮（一次回复读成两轮）。现在逻辑表加 `UNIQUE (umo, msg_uid)`，认领路径按同一范围先查归属，**先认领者赢**：被拒的一方不抢归属、不并组，把参与行 `turn_id` 改回归属轮。回收物理消息时一并删悬空链接行（`/reset`、保留期清理后不再留下指向不存在消息的归属行）。
- **清理与孤儿扫描按文件名当 `media_id`（内容寻址后失效）**：保留期/总量清理会"删了 DB 行、文件一个都没删"；孤儿扫描会把多实例正在共用的文件当孤儿删掉（不可逆）。现在两处统一按归档行里的 `file_name` + 月份定位、按引用计数决定删不删。
- **计划成员行没有物理消息，三处"按物理行定位"的路径都漏了它**：① 切读后"只有计划、回显还没到"的轮次在消息级 V2 视图里**整段消失**（轮次路径本来有兜底，消息路径没有，同一份数据两条读路径给出不同答案）；② `/reset` 与保留期清理只删物理消息与参与行，计划成员留在库里 → 被 reset 掉的那一轮在切读后重新出现；③ 查询台软删除出站计划行时按本地键找行，`l:` 行已不存在 → 删除不生效、切读后复活。现在三处都按 `(umo, cid, turn_id)` 直接处理计划成员行（`msg_uid IS NULL`）：读路径补计划成员、清理路径一并删掉、软删除标在轮次表上。
- **撤回补标从来没真正跑起来**：`replay_pending_recalls` 用了未定义的常量（`NameError`），异常又被上层 `except Exception: return (0, 0)` 吞掉 → 每次都返回"没有待补标的"；`mirror_recall` 在删诊断时丢了返回值，补标路径拿 `None` 与数字比较同样抛错。现在常量补上、`mirror_recall` 返回命中行数、失败至少记一行异常类型（不写正文）。
- **回显行的 persona 补不上**：回填只看同表"同轮兄弟"，而回显写的参与行在 `message_bots` 里没有同轮带 persona 的行（计划成员只活在轮次表）→ 按 persona 过滤时回显字整段消失。现在参与行也会从本轮计划成员补 persona。
- **push 回显把 BOT 自己当"被回复人"**：`target_id` 写成了发送者（＝BOT 自己），而影子视图的 `user_id` 就取 `target_id` → 默认（当前用户）scope 下这些实际发出的段查不到，切读后永远只剩计划全文。现在按发送侧记账带上真正的被回复者，并在加载时把历史行纠正回同一轮 user 的发送者（旧表那一行的 `user_id` 优先，其次同轮 user 行；`target_id = self_id` 是唯一判定条件）。
- **切读的读源不再一半新一半旧**：v2 读失败时只回退失败的那一步，Reply 目标与工具段仍按 v2 查。现在一次请求只用一个读源（`_takeover_query` 返回实际读源），任一处 v2 失败就把**这次请求整段**用 v1 重跑。
- **查询台软删除 BOT 回复只标了计划成员**：旧表 assistant 行没有平台 mid，按 mid 定位永远失败；只标计划成员时回显段仍在，切读后被删的回复照旧出现。现在按 `(umo, cid, turn_id, role)` 把这一轮所有实发行一起标删/恢复。
- **切读门槛的判定时机**：门槛在插件加载时按双写窗口算一次（改配置本身就会触发 reload，所以勾上开关那一刻就会按当时的窗口重新判定），满 7 天即生效；不回写运行期状态，规则只有一条：装上本版满 7 天解锁。
- **`@`/引用/转发目标在切读后丢失**：影子层不建这三列，`relation_data` 也没写 → V2 返回的 `at_id`/`reply_id`/`forward_id` 恒为空。现在写进 `relation_data`（不建列、不动结构），读侧取回。
- **初始化失败会留下活着的后台任务**：AstrBot 在 `initialize` 抛错时不调 `terminate`，死实例的巡检会一直跑并把新实例的回显订阅摘掉。现在失败路径先取消任务、摘订阅、停归档，再 dispose。
- **发送侧账本不会过期**：`after_message_sent` 没来时这条账永远留着，之后该会话的回显全归到老轮次（persona 也错）。现在按 `closed_at` 兜底 `opened_at` 过期。
- **查询台仍按旧表读（三个 BOT 同一条消息显示三次）**：旧表按 `(umo, cid)` 存，三个实例看同一个群就有三行。现在查询台读合并存储——一条物理消息一行；双写起点之前的旧历史仍读旧表，并按 `(会话, 发送者, 正文, 平台时间)` 去重（那时同样被多实例各存一份）。删除/恢复写到合并存储那一行（`id` 用 `10^9` 偏移区分两种来源，列表不再显示这个 id）。
- **查询台筛选条件收敛成 8 项**：聊天类型（群聊/私聊/全部）、QQ 号（群号或好友号）、发言者（纯数字按 QQ 号精确、其它按昵称模糊）、内容类型、事件子类、起止时间、删除状态、每页数量；关键词搜索保留。删掉 UMO / Conversation ID / Role / LLM 状态 / Persona / 平台 / 消息类型 / 只看配对轮次——都是内部维度，按它们筛只会得到"看不懂的空结果"；工具调用页的 UMO、CID 下拉同步改成手填输入框（facets 不再提供这两组候选）。
- **查询台列表去掉编号、不分角色**：详情按钮只写「详情 ›」（数据库 id 只在详情字段表里），卡片不再有 AI/U 方块与按角色区分的色条，改为每条消息最前面显示发送者头像（直接引用 QQ 现成头像接口；非 QQ 号或加载失败回落首字母）与昵称。
- **新增「高级只读模式」**（顶栏第二个 pill，与删除模式互斥）：选定一个 persona 后，列表按该 persona 的轮次收窄，并为每条消息追加 chip——`turn:<轮次前 8 位>`、本条在轮次里的角色、该轮回复的 `llm_status` / `send_status`、`已配对`/`单边`。数据来自参与行（`chat_memory_message_bots`），不指定 persona 时按普通只读模式跑（同一条消息会被多个 persona 的轮次各看一次，不选就没有唯一答案）。
- **回显行的 llm_status 是空的**：push 回显那一路写参与行时状态恒为空（只有发送侧知道），查询台高级模式于是把每一轮回复都显示成 `no_llm`。现在发送侧记账带上状态、回显/拉取都写进去；历史空值不做事后修复（影响仅限状态展示与按状态筛选 assistant 行，上下文内容按 `turn_id` 挂回、不受影响）。
- **`l:` 键的含义被搞错过（开发期）**：`l:` 是平台没给 `message_id` 的真实消息/事件的本地键（表情回应、戳一戳、群名片变更…）。曾按「计划全文」处理并在消息级视图过滤，会让事件整类消失；现已取消过滤。计划全文本身从不进消息表（`origin=outbound` 只有真实发出的消息）。
- **关系编码缺 `v`**：`_merge_link_ids` 在 `relation_data` 为空时只写 `{reply_id: …}`，读侧要求 `v == 1` 才认，于是引用目标读不出来。现在补 `v: 1`。
- **V2 配对查询比 V1 多一条状态过滤**：`query_rounds_raw_v2` 曾硬编码 `statuses={llm_success}`，而 V1 的同名查询没有任何状态条件 → 切读会把「有回复但 user 行状态不是 llm_success」的整轮丢掉。现已对齐 V1。
- **查询台高级模式配对筛选慢到不可用**：`paired_only` / 该轮状态筛选用的是「参与行 JOIN 参与行」，缺 `(turn_id, role)` 索引时退化成 81k × 30k 的相关子查询（实测单次 195 秒）。现在补 `ix_cmmb_turn_role` 索引，并把条件改成只算一次的 IN 子查询（同一份库 195s → 0.76s）。
- **自己的动作事件没有署名**：主动贴表情、主动戳一戳这类事件，平台给的 sender 只有 `self_id`，于是落库的 `sender_nickname` 是号码、`<cm_event>` 的 `operator` 常常是空，查询台里读成「回应了一条消息」，按发言者（人格昵称）也筛不到。现在缓存各实例的登录昵称（`get_login_info`，6 小时 TTL），自己发出的动作事件用它署名，`operator` 同样补上；历史行不回填。
- **事件句子的主语与「发言者」筛选**：历史事件行里 operator 常是空（自己贴表情那类），列表里会渲染成「用 X 回应了 Y 的消息」，按「发言者=昵称」也搜不到（库里存的是号码）。现在渲染时用同发送者补出来的显示昵称补主语，且非数字发言者筛选会同时命中该昵称所属的发送者——历史数据无需修复即可读对。
- **发不出去的回复与"还没发"同形**：被禁言、平台拒收、或别的插件清空结果时，这一轮只有计划成员，`send_status` 永远停在 `prepared`。现在 `after_message_sent` 发现该轮没有 assistant 实发行时，把计划成员标成 `send_attempted`（只标这一轮，正常轮次不受影响）；上下文兜底用计划全文时再加 `<cm_unsent/>`，避免模型把这句当成已经说出口。
- **`seq` 不再是可靠游标（外部只读读者）**：原来的 `seq` 是「会话内 MAX(seq)+1」，会话被 `/reset`、保留期清理或孤儿回收删空后会从 1 重新开始，外部消费者的 `seq > cursor` 会永久漏读、幂等键还会撞车。现在由 `chat_memory_session_seq` 计数器分配、**只增不减**（结构线 `2 → 3`；认识的旧版本走迁移并播种到当前最大值，不丢数据、不重置切读窗口）。
- **文档把 origin 写错了**：docs/multi_bot_shared_store.md 曾写「outbound ＝ 发送路径写的计划全文」，被外部读者读成「outbound 行＝没发出去的假消息，应当过滤」。实际上计划全文从不进消息表，现存的 outbound 行都是真实消息——按它过滤会误删。文档与代码注释已改成「origin 由写入侧派生、只能当信息字段」。
- **README 增加「外部只读读者契约」**：哪些列稳定、`content` 是模板（必须先按 `relation_data` 渲染）、`origin` 只是信息（计划全文从不进消息表，按它过滤会误删真实消息）、`seq` 语义与游标写法、`msg_uid` 才是幂等键。
- **查询台读源判定写死结构版本**：`_shadow_ready` 比较的是字符串 `"2"`，结构线升到 3 后查询台静默退回旧表——症状是昵称全变号码、同一条消息出现两三张卡片。现在期望版本由写入侧常量传入（`expected_shadow_version`），写死与漏改两边都不可能再发生。
- **日志定级过一遍**：切读窗口未满是等待状态改 INFO；persona 为空、平台不支持拉历史这两类「每条消息都会命中」的提示降到 DEBUG；跨会话黑名单命中改为每会话 INFO 一次（其后 DEBUG）；订阅回显失败从 exception 降为 warning（是通道降级，不是异常）；工具上下文/焦点注入失败的异常串只留类型（SQL 串会带正文）。
- **群聊 chip 带上群名**：`群聊·<群号>（群名）`，群名走 OneBot `get_group_list` 现查（进程内缓存 6 小时，不落库）；平台未连接/客户端不支持时只显示群号。
- **计划兜底一跑就报错（切读实际一直回退旧表）**：`_fetch_plan_members` 还在 SELECT 结构线 5 已删掉的 `relation_data` 列，凡"有 user 行、该轮还没有实发段"的轮次都抛 `no such column`，被上层吞成"回退 V1"。现在按实际列取数，计划行的正文经 `strip_relation_tokens` 清掉残留 `⟦CM_*⟧` 占位（裸 token 不再进上下文）。
- **切读后记不住刚才聊的**：`fetch_rounds` 超取后取 `paired[:wanted]`，而内部列表是**时间升序**，实际留下的是"最新窗口里最旧的几轮"、把用户刚聊的丢掉。现在按方向切尾（`from_oldest` 时切头），与 V1 的 `ORDER BY … DESC LIMIT` 对齐。
- **结构线跨版本升级会写坏影子层**：`shadow_migration_statements` 只回一份"该版到下一版"的增量，从结构线 2/3 升上来会跳过 3→4、4→5 却把版本号写成 5，随后 `reply_messages` 不再是新主键 → 所有影子写失败。现在累积应用到当前版本，链断才回落到丢弃重建；`ALTER TABLE … DROP COLUMN` 先查 `PRAGMA table_info` 再执行（不再靠吞异常）。
- **写失败被当成写成功**：`INSERT OR IGNORE` 命中唯一键时 rowcount=0 但接口恒返回成功 → user 行没落库、assistant 照样升级 `llm_success`。现在 `insert` / `insert_tool_record` / `insert_media_archive` 如实返回"是否真的插入"，`_safe_insert` 原样上报并按 id/状态记一条 WARNING（不带正文）。存量库若已有重复 `(turn_id, role)`，加载时先去重并告警一次，避免建唯一索引时整插件加载失败。
- **内容寻址下误删别人在用的媒体**：归档复用已有文件后若登记失败，旧实现无条件 `unlink(dest)`——删掉的是**多个归档行共用的同一个文件**（引用计数守卫在异常路径上不生效，不可逆）。现在只有"本次真正落位"的文件才回删，复用分支只删临时文件。
- **查询台与上下文读源不一致**：查询台只判"三表齐 + 版本对 + 双写满 7 天"，默认配置（`shadow_read=false`）下模型读旧表、页面已读合并存储。现在读源由插件传入（`shadow_read_enabled`），overview 回显当前读源；V2 行软删除后同样失效物化缓存/锁定的快照（原来只清旧历史缓存，详情抽屉读到删除前的状态）。
- **几处"筛了等于没筛"**：高级模式未选 persona 时角色/状态/仅成对三个条件被静默丢弃 → 现在报 400；工具页「未标记 persona」空值被 `||` 吞成"全部" → 改用 `__empty__` 哨兵（与记录页一致），并补上「每页数量」的重查绑定；工具页 QQ 号筛选加尾部锚（`123456` 不再命中 `1234567`）；事件子类过滤加 `json_valid` 守卫（脏 JSON 不再让整条查询抛 malformed、被误判成库损坏）；工具删除/恢复后同步更新详情缓存（详情不再显示旧状态）。
- **日志与可诊断性**：回显订阅巡检拿到的 bot 现在登记进 `_platform_bots`（卸载时能真正摘干净）；`track_platform_context` 里读不到的 cid 死分支删除，改由捕获成功后登记；pull 通道"一条没拉到"留 DEBUG（含 error 类型）；群名落库用**给出结果的那个实例**的平台名；日志前缀统一为 `[ChatMemory][platform:{platform_id}]`；查询台导出失败只回固定文案，异常与路径只进服务端日志。
- **版本与文档单源**：查询台 `about.version` 改为从 `metadata.yaml` 读取（不再写死）；README 的合并存储表数量更正为 7 张，并写明 `since`/`until` 按 UTC naive 解释、`created_at` 回传做游标会偏一个时区；`PROJECT_CONTEXT.md` 的交接段与仓库现状对齐，删掉对已不存在的 `integration/` 脚本的引用。

### 兼容
- **插件升级/重载会被"启动维护"拖死几分钟（本版修复）**：`initialize()` 里同步 await 撤回补标、persona 回填、回显 target 纠正、孤儿回收这四步，而撤回补标的每条 UPDATE 写成 `WHERE msg_uid IN (SELECT …)` → 优化器全表扫 `messages`（单条 85ms），线上 415 个撤回事件累计约 4 分钟。表现：Dashboard 升级卡在"更新中"、期间一串 `合并存储读取失败，已回退旧表: OperationalError`（长事务把读路径锁在外面），连 `portrait` 读库也一起报 OperationalError。现在：① 启动维护**移到后台任务**（`initialize` 只等迁移，约 1 秒）；② 撤回定位改用先查主键再按主键更新，并新增 **`ix_cmmb_umo_mid (umo, platform_message_id)`** 部分索引——同一份 30 万行库实测 30s → **1.05s**；③ 补标每 20 条提交一次，缩短写锁窗口；④ 结构迁移完成打一条 INFO（`migrated:4->6（N 步）`），`_read_failed` 的告警带上异常消息（如 `database is locked`），不再只有异常类型。
- **撤回标记改为退避重试，不再靠启动扫描兜底**：撤回通知常早于目标消息落库（平台两个事件、或写入排队），当场 `UPDATE` 命中 0 行时以前只能等下次加载扫 3 万行事件补标。现在当场标不上就挂进待重试队列，按 0.5/1.5/3/8 秒退避重试（同一目标只挂一条链，队列上限 200），命中即停；13 秒内仍未落库才放弃并告警一次。`mark_recalled` 同时返回旧表命中数，重试判定精确且幂等。
- **删掉"扫历史补状态"的启动任务**：撤回补标（`replay_pending_recalls`）与回显 target 纠正（`repair_echo_targets`）整体移除——前者由退避重试覆盖，后者是一次性历史修补且新写入已带正确 `target_id`。启动期只剩**孤儿行回收**（无参与行的消息 / 指向已删消息的参与行，实测 8 万行 0.07s，属清理垃圾而非补历史）。persona 回填保留在每日清理里（覆盖"跨重启回显无 persona"，0.05s，`auto_cleanup_days=0` 时本就不跑）。
- **合并存储结构线 `5 → 6`（只收窄 + 补约束，不动语义、不丢数据）**：工具表唯一键从 `(umo, cid, turn_id, call_index)` 收窄为 `(turn_id, call_index)`（`turn_id` 是 uuid4，一轮的调用不可能跨 cid 重复；按会话取工具改走新增的 `ix_cmtc_umo_turn`）；计划表主键从 `(umo, turn_id)` 收窄为 `turn_id`；删掉只写不读的 `messages.reply_group` 与其部分索引、`reply_messages.instance_ts`（组键现推、实例时刻取参与行 `raw_timestamp`）；删掉两条 `EXPLAIN QUERY PLAN` 实测从未命中的索引（`ix_cmm_fingerprint`、`ix_cmmb_platform_mid`），新增 `(platform_name,message_type,session_id,created_at)` 与 `(umo, platform_message_id)` 两条索引；`shadow_meta` 键加白名单 CHECK、`reply_messages.content_kind`/`send_status` 与 `tool_calls.call_index` 加 CHECK。迁移全部走"建新表→搬→改名"（重复 `turn_id` 保留最早；非法 JSON/状态归一成默认值），`messages` 用 `DROP COLUMN`（实测 `rowid` 不变，查询台 `record_id` 契约不受影响），`tool_calls` 搬迁显式带 `id`（工具页 `tool_id` 不变）。
- **孤儿行回收**：合并存储没有外键，新增 `shared_store.purge_orphan_rows()`（无参与行的消息 / 指向已删消息的参与行），插件加载时跑一次并记 INFO——以前只有"本批参与行被删"的物理消息会被回收，更早批次的孤儿永远留着。
- **内部结构线独立编号 + 结构变更优先迁移**：合并存储的内部 schema 从 `1` 起算（本版最终定为 `6`），以后每次改结构 `+1` 并往 `SHADOW_MIGRATIONS` 加一条幂等迁移（`shadow_migrate=migrated:旧->新`；多个版本跨度会**累积应用**）。不认识的旧结构才丢弃重建：只清镜像，**切读窗口沿用**（`dual_write_started_at` 不变，不必重等 7 天）。（审阅/升级演练脚本只在本机运行，不入仓。）
- **合并存储：本版首次发布**——内部结构线最终定为 `SHADOW_SCHEMA_VERSION = 6`
  （`chat_memory_shadow_meta.schema_version`，以后每次改结构 +1、不跳号），升级一律走
  `SHADOW_MIGRATIONS`（**累积应用**，不是"版本不等就丢弃重建"）。旧表
  `chat_memory_records` / `chat_memory_tool_records` 完全不动，`chat_memory_media_archive`
  只加 `content_hash` 列。
- **新增 `sent_capture_history`（默认 `false`）**：发送后拉群历史兜底；只在平台不提供自身消息回显时才有意义，默认关。
- **媒体归档表结构 `7` → `8`（加 `content_hash`）**：旧库加载时 `ALTER TABLE` 加列，不重建、不动既有数据；文件改名与去重由每日清理滚动完成（每轮 1000 行、单轮最多读 1 GB）。
- **影子层收敛为 6 张表**：`messages` / `message_bots` / `reply_messages` / `tool_calls` / `session_seq`（会话序号计数器）/ `shadow_meta`。计划全文不再写进 `message` 表——它没有 msg_uid、没有平台事实，改写成 `reply_messages` 的**计划成员行**（`msg_uid IS NULL`，一轮一条，`ux_cmrm_plan` 保证）；组键改由 `rg:<turn_id>` 派生；三张诊断表（`shadow_stats` / `shadow_events` / `echo_channels`）与 `shadow_anomalies` 删除**且代码里的写入点、包装方法与空桩同步删干净**（不保留"有签名不落库"的桩），撤回补标改由撤回事件行派生；结构线 `1 → 2`，旧结构重建（1.5.4 没有影子层，直接建 5 表）。
测试：行为级 66 项 + UI 29 项（合计 95 passed，入口 `pytest chat_memory/test chat_memory/test_ui`，从插件父目录跑）。**真实 SQL 路径目前没有自动化覆盖**（早先的 `integration/` 脚本没有随仓库保留，别再引用它）：合并存储的合并/幂等/归组/段序/兜底/认领/软删除/撤回重放等行为靠部署端重载 + 查询台自查 + 临时库脚本验证。结构线 6 的迁移已在临时库上实战跑过 `4→5→6`（行数、`rowid`、工具 `id`、索引集合、CHECK 逐项比对）。

## 1.5.4 — 2026-10-10

### 新增
- **跨会话黑名单（`context_takeover.cross_session_blacklist`）**：名单内的群聊/私聊**不参与跨会话记忆**，双向生效——它不作为来源出现在别人的上下文里（跨会话查询按 `(message_type, session_id)` 在 SQL 层排除），自己也读不到其他会话（即便开着 `cross_session` 也强制只读本会话）。
- **格式与匹配**：`G:<群号>`（群聊）/ `F:<用户号>`（私聊），按类型精确匹配——群与私聊 id 空间不同，拉黑一个群不会连带同号的私聊；名单内的会话**自己的同会话记忆完全不受影响**（含 `full_group`）。
- **边界与可见性**：只影响上下文/记忆注入——存档照旧、查询台手动检索不受影响；写错的条目在插件加载时逐条 WARNING 并忽略（不会被静默当成通配），命中名单的会话首次使用打一条 INFO。

测试：行为级 49 项 + UI 20 项。

## 1.5.3 — 2026-09-10

### 新增
- **查询台软删除（schema v7，单条）**：主表与工具调用表新增 `deleted_at`（旧库按需 `ALTER TABLE`，各加 `(umo, deleted_at)` 索引），两张表同一套逻辑。点击顶栏「只读模式」进入「**删除模式**」，每条记录（列表与详情页）后面出现 times 图标，点击即软删除该条；删除后该条下方展示 `deleted` chip，图标变为 backup 图标可一键恢复。软删除只在查询台开放、一次只处理一条，且仅在数据源为插件实时库时可用（指向其他路径/快照库时返回 403）。
- **删除状态筛选与展示**：记录查询与工具调用查询各新增「删除状态」下拉（`All` / `未删除`（默认） / `已删除`）；记录详情页显示删除时间，工具卡片内显示删除时间与 `deleted` chip；概览与健康检查改为**未删除**口径并单列已删除计数。接管检索、公开查询 API、工具回放等所有读路径都过滤已删除行。

### 修复
- **迁移顺序（会导致插件加载失败）**：`init_db` 的建表/补列/建索引顺序统一由 `schema_statements()` 产出，先 `ALTER TABLE ADD COLUMN` 再建该列索引；此前把软删除索引排在补列之前，存量 v6 库会以 `no such column: deleted_at` 直接加载失败。新增不依赖 SQLAlchemy 的顺序守卫测试。
- **日志不再截断**：所有 id（umo / conversation_id / message_id / turn_id / media_id / at_id / reply_id / forward_id）在日志里输出完整值；DEBUG 与 WARNING 行的正文、原始事件 JSON、命令原文与残留明细（含同 UMO 其它 CID 列表）也全部完整输出。新增静态守卫测试，禁止日志/id 路径再出现 `[:N]` 切片。
- **其它截断清理**：合成 tool_call_id 由 `cm_tool_<turn_id 前 8 位>_<n>` 改为 `cm_tool_<完整 turn_id>_<n>`；撤回事件记录改为保存被撤回消息**全文**（原为前 60 字摘要）；文件名净化上限由 120 提到 255（文件系统上限）；UI 文件名占位与数据库路径/分布图标签不再截断。
- **写库失败不再回显异常串**：记录落库、工具调用落库、native history 覆盖三类承载内容的写路径失败时只记异常类型——SQLAlchemy 异常串会把 `[parameters: …]`（即聊天正文 / 工具结果 / history）带进 WARNING，与「INFO 不承载聊天内容」同源收紧；失败形态（异常类型）仍保留可定位。
- **`/reset` 可诊断性**：命令文本无法区分 reset/new 时 WARNING 带原文；`/reset` 后立刻回查同一 (umo, cid) 并按 `content_kind` 分组自检残留（无残留记 DEBUG，有残留记 WARNING 带类型明细），同时 DEBUG 输出同 UMO 其它 CID 的条数（说明这些属历史会话）；当前 UMO 无 conversation_id 时 WARNING 而非静默返回。命令识别只认 AstrBot 自带命令的原文形态（不做平台后缀/全角斜杠等花式兼容）。

### 变更
- **查询台展示不再截断**：UMO / Conversation ID / turn_id / 昵称全部显示完整值（徽章可换行、`title` 为完整值），引用原文去掉 300 字符上限，列表正文去掉 180px 高度裁剪与引用块 84px 裁剪，数据库路径与分布图标签不再省略号截断。仅保留列表正文 4000 字符预览上限（带明确提示，详情 20 万字符）。
- **`/reset` 语义保持不变**：仍只清当前 CID（与 AstrBot 一致），不跨 CID、不跨 UMO；媒体文件与归档仍按原逻辑级联/保留。软删除是查询台的独立能力，不参与 `/reset`。
- 软删除不动媒体文件：记录与媒体的物理删除仍走原有配置（记录按 `auto_cleanup_days`，媒体按 `media_archive` 保留期/总量上限）。

测试：行为级 46 项 + UI 20 项 + 存储集成。

## 1.5.2 — 2026-09-09

### 变更
- **内容白名单拆回细粒度**：`include_content_kinds` 取值改为与存储 `content_kind` 一一对应的 `text/image/video/voice/emoji/file/forward/event`，删除 `media` 大类与 `detail_control`/`detail_kinds` 两层间接（配置与存储/查询台/徽章口径一致）。旧配置里的 `media`、`media-*`、`event-*` 值**不做兼容映射、直接忽略**，升级后请重新勾选具体类型。
- **事件子类独立配置**：新增 `event_kinds`（ban/member/title/recall/resource/poke/reaction/system，默认全选，不再用开关隐藏），仅当白名单含 `event` 时生效，清空 = 全部放行。
- **上下文媒体标签按类型拆分**：`<cm_media type="…"/>` 拆为与 `content_kind` 同名的 `<cm_image/>` / `<cm_video/>` / `<cm_voice/>` / `<cm_file/>` / `<cm_forward/>`（`<cm_emoji>名称</cm_emoji>` 不变），类型无法识别时用 `<cm_unknown/>` 兜底——配置取值、存储 `content_kind`、查询台筛选、上下文标签四处统一为同一套词汇。
- **文件占位带文件名**：`<cm_file name="报告.pdf"/>`（XML 转义，无文件名回退 `<cm_file/>`），让 LLM 知道发送了什么文件。

### 修复
- **未知媒体类型不再谎报图片**：未知 `kind` 或 token 越界的占位由 `<cm_media type="image"/>` 改为 `<cm_unknown/>`，LLM 规则与 README 同步。
- **旧 poke 占位对齐**：纯媒体路径的戳一戳占位与混合消息路径统一，带目标昵称（旧 `kind=["poke"]` 数据兜底）。

测试：行为级 42 项 + UI 18 项 + 存储集成。

## 1.5.1 — 2026-09-08

### 修复
- **查询台事件子类过滤失效**：前端 `collectQuery` 未发送 `event_type`，选了子类查询结果无变化（后端与测试早已支持），现补上并加回归断言。
- **详情提及昵称 XSS**：`mention.nickname` 未转义即拼入 DOM，现统一转义；详情抽屉加请求 token，避免慢响应覆盖新打开的记录。
- **媒体端点 Range 整读大文件**：Range 分支改为只读请求区间（回归测试锁定「不得整读」），补归档文件名 basename 校验；`file_token` 分支先判依赖再嗅探编码。
- **不兼容旧库异常**：`sender_nickname` 改安全取值、`export_snapshot` 缺库抛 `DatabaseUnavailableError`（与查询 503 语义一致）；概览每日趋势按插件时区分桶。
- **事件与分类边界**：纯 poke 回退渲染为事件占位（不再出现幽灵 `<cm_media type="image"/>`）；`OtherMessage` 仅在未识别出内容时兜底；纯引用回复的 assistant 消息不再漏存；reset/new 改词边界匹配（`renew`/`news` 不再误判）；媒体归档日志不再落绝对路径。
- **前端资源回收**：列表空结果与切换数据源时停止语音播放并回收 objectURL；原图加载失败给出提示、撤销职责单一化。

### 变更
- **移除 1.4.0 之前的兼容代码**：删除 `face→emoji` 一次性数据迁移与旧 schema 增量迁移/重建路径（市场首发 1.4.0）；打开 1.4.0 之前的旧库时 `init_db` 直接给出可诊断的失败提示（列出缺失列，引导先用 1.4.0 升级或移走旧库），不再静默迁移。`system_event`/`poke` 等 1.5.0 才改名的旧值读兼容保留（1.4.x 市场用户数据仍需要）。

### 新增
- **撤回消息跳过上下文（`recall_skip_context`）**：撤回 notice 命中本地记录时标记 `recalled_at`（schema v6 主表新增列，幂等，另补 `(umo, message_id)` 索引）；配置开启后被撤回的原消息与撤回事件（`<cm_event type="recall">`）都不再进入 LLM 上下文，查询台仍可见并显示「已撤回」徽章。默认关闭。
- **assistant 侧入口测试**：补 `capture_bot` 四态（proactive/orphan/llm_success/default + cm 标签清洗）、`mark_send_attempted`、`inject_current_turn_focus`、reset/new 词边界共 3 项行为级测试；测试夹具中的疑似真实 QQ 号替换为虚构占位。

测试：行为级 41 项 + UI 18 项 + 存储集成。

## 1.5.0 — 2026-09-07

### 新增
- **system_event 未知类型诊断日志**：泛化兜底（`[系统事件]`/`[系统通知]`）落库时输出 DEBUG 日志，附 post_type/notice_type/sub_type，用于识别 NapCat/Lagrange 等实现的扩展 notice 类型；默认 INFO 下不显示，需在 WebUI 插件详情页把日志等级调为 DEBUG 后可见。
- **媒体播放与占位增强**：媒体端点支持 HTTP Range（206），查询台视频改直连流式播放（支持拖动、不受 base64 16MB 上限，失败自动回退 base64）；正文中的已归档图片占位改为内联缩略图（双击看原图），文件占位直接显示净化后的文件名。
- **纯挂载表情消息漏存修复**：mface（表情挂件）被平台适配器丢弃成空链导致整条消息漏存；现在按 emoji 归档（content 为 `[emoji]`），不再丢失。
- **上下文协议收敛（`<cm_event>` / `<cm_media>`）**：系统事件与动作统一为单一事件标签 `<cm_event type="大类">自然语言事件描述</cm_event>`（type 枚举 ban/member/title/recall/resource/poke/reaction/system，附时间元数据），描述按 QQ 群聊系统消息风格组装（如「长风 被 小明 禁言了 10分钟」），角色昵称内嵌 `<cm_nickname>`（账号 ID 的显示昵称，QQ 号不进上下文）；撤回同时落存原始 message_id 供查询台查看；好友添加/请求类通知取消归档；媒体占位统一为 `<cm_media type="image|voice|video|file|emoji|forward"/>`；未知 notice 类型兜底 type=system 并打诊断日志（每种组合首次 WARNING 带原始 JSON 截断，重复 DEBUG 防刷屏）；通用规则（默认 user 均为成员发言、仅 cm_event 包裹为事件）与输出清洗同步覆盖。
- **内容大类重构**：`include_content_kinds` 收敛为 `text` / `media`（视听媒体：image/video/voice/emoji）/ `file`（文件附件）/ `forward`（转发消息）/ `event` 五类。
- **细粒度控制**：`detail_control` 开启后显示单项「细类」配置（`detail_kinds`，`media-*` 与 `event-*` 前缀项，默认全选，随开关条件显隐），media 与 event 按勾选放行；关闭时均为整个大类。
- **查询台事件子类过滤**：新增「事件子类」下拉（ban/member/title/recall/resource/poke/reaction/system），按 `relation_data.event.type` 过滤记录。
- **事件句子列表展示与子类徽章**：event 类记录在列表直接展示与上下文一致的自然语言句子（不再只显示 chip）；媒体/事件记录显示子类徽章（`event·poke`、`media·image` 等，完全动态读取无硬编码清单），详情页同步展示。
- **表情回应事件（reaction）**：识别 NapCat 扩展 notice `group_msg_emoji_like` 并归档为事件子类 `reaction`（添加/取消表情回应），按 message_id 关联被回应消息的作者昵称；配置细类、查询台「事件子类」过滤与徽章同步支持。
- **QQ 表情中文名（`<cm_emoji>`）**：内置 NapCat 官方 face 表（329 条），正文里的 QQ 表情在上下文中渲染为 `<cm_emoji>名称</cm_emoji>`（如「问号脸」，未知 id 回退 `<cm_media type="emoji"/>`），表情回应句子带表情名（如「阿明 用 问号脸 回应了 小虎 的消息」）；查询台正文占位同步显示中文名（`⟦表情:问号脸⟧`，媒体栏 chip 保持简洁的 emoji），LLM 规则补充 cm_emoji 语义。
- **列表语音气泡**：纯语音消息在列表正文位置显示 QQ 风格语音气泡，点击即播（bridge base64 通道 → blob → Audio，带播放动画，再次点击停止），未归档显示「语音（未归档）」。

### 修复
- **纯图片消息列表不显示缩略图**：纯图片消息的正文是 `⟦图片⟧` 占位，现在对已归档图片输出缩略图标记，列表与详情均可内联预览（未归档/旧数据保持占位文本）。
- **媒体图展示去重**：详情页媒体栏不再重复挂图（原先有拉伸），看图统一走正文内联缩略图（详情内可双击看原图）；列表缩略图仅作预览，不再支持打开原图。
- **视频播放失败可见化**：直连 Range 与 base64 兜底都失败时，播放器旁显示原因提示（编码不支持/媒体过大）并引导下载本地观看，不再静默黑屏；媒体归档完成时输出 DEBUG 日志（kind/ext/size）。
- **视频播放鉴权回退链**：查询台 iframe 直连媒体端点会被 dashboard 鉴权中间件 401（拿不到 JWT cookie/header）；且页面 iframe 是不透明源（sandbox 无 allow-same-origin），直接 fetch `/api/file` 会被 CORS 拦截——媒体字节统一走 bridge 的 base64 通道，file_token 仅作 8ms 的编码/大小元数据探测；带 HEVC/16MB 闸门，超限直接提示下载，不再整段拉取（几十 MB 视频会让页面内存暴涨卡死）；token 模式后端不整读文件（只嗅探编码，实测 35MB 视频响应 8ms），base64 模式按归档大小先拒（不再为一条 413 整读大文件）、未知 as 模式返回 400。
- **poke 事件 UI 身份回退**：操作者/目标昵称缺失时查询台回退显示账号 ID（如「10001 戳了戳 10002」），仅查询台展示，上下文协议仍不含账号。

### 变更
- **事件 kind 更名为 `event`**：新数据 `content_kind` 写 `event`（旧值 `system_event` 仅兼容读取/筛选/渲染）。
- **事件大类 `resource`**：群文件上传/离线文件的 type 由 `media` 更名为 `resource`（与媒体大类区分）。
- **戳一戳并入事件协议**：user 戳一戳捕获时归一为 `event`（type=poke，operator=发言者昵称、target=被戳者昵称），旧 poke 数据兼容渲染为事件占位。

测试：行为级 37 项 + UI 17 项 + 存储集成。

## 1.4.1 — 2026-09-04

### 新增
- **导出快照**：查询台新增导出按钮，用 SQLite backup API 生成点击时刻的一致快照并下载，源库读写不阻塞；快照留存 24 小时后自动清理。
- **system_event 摘要增强**：禁言/解禁带时长、群管理员设置/取消、设置群头衔带新头衔文本、群文件上传带文件名、群运气王/荣誉变更、戳一戳、群名片变更、离线文件、客户端状态等可读摘要；撤回消息按 message_id 本地关联原文，摘要附发送者昵称与内容片段（未命中回落纯 `[撤回消息]`）。
- **内置群聊上下文感知共存警告**：接管真正注入时检测 AstrBot 内置「群聊上下文感知」是否开启，开启则提示一次 WARNING（始终启用、进程内仅提示一次），避免两种格式的群聊历史重复注入。

### 变更
- **移除 log_config 死代码迁移**：1.2.x 旧组继承/写回实际未生效，删除迁移方法与旧组读取兜底，`log_with_bot_id` 仅读顶层配置。
- **测试精简**：按「只测关键节点与易错函数」原则合并同域变体、删除低价值测试。

测试：行为级 34 项 + UI 15 项 + 存储集成。

## 1.4.0 — 2026-08-31

### 新增
- **查询台并入**：原 chat_memory_ui 的只读查询台并入本插件（`ui/` 子包 + Plugin Page），记录检索筛选、工具调用、历史媒体回看与数据库健康诊断随包分发；chat_memory_ui 停止单独维护，升级后请停用并移除旧插件。

### 修复
- **BOT 被禁言等 notice 漏存**：notice 的 sender 取被操作对象（自己=bot），BOT 自身守卫不再吞掉这类系统事件，仍按 `system_event` 归档。

### 变更

- 市场发布准备：metadata 补齐 `desc`/`short_desc`/`tags`，requirements 补 `aiohttp`、`Pillow` 与 Windows `tzdata`，logo 缩至 256px。

测试：行为级 54 项 + UI 27 项 + 存储集成。

## 1.3.1 — 2026-08-23

### 修复
- **空消息链崩溃**：notice/request 转成的空消息在捕获时崩溃，必现于部分空消息。
- **system_event 捕获崩溃**：notice/request 事件在捕获时因包装方法缺失报错，补齐类内包装。
- **媒体归档健壮性**：MP4 magic 嗅探按 box 偏移判定（此前无文件名视频落成 .bin）；下载取消/超时不再残留 .part；孤儿文件扫描统一 UTC 时间并分批查询；落盘后登记失败回删文件；配置布尔/整数安全解析（`"false"` 不再误判 True）。
- **正文转义与截断**：转义简化为单级哨兵对（用户文本中的 `⟦`/`⟧` 统一替换为更罕见的 `⦑`/`⦒`，展示侧还原），任意含 `⟦`/`⟧` 的字面输入均可双射还原；按字符预算截断不再残留 `⟦` 残片、不误剥手造数据里的裸 `⟦`。
- **日志分级**：周期维护统计与 proactive 标记降为 DEBUG（用户操作结果仍 INFO）。

测试：行为级 53 项 + 存储集成。

## 1.3.0 — 2026-08-21
- **媒体归档（落盘）**：user 侧 image/video/voice/file 落盘到插件数据目录（schema v5 新增归档表），本地源同步接管（毫秒级）、远程 URL 后台下载（2 worker，短超时），管线零阻塞、失败不影响存档；保留期/总量上限自动清理，`/reset` 与自动清理级联删除。供 ChatMemory UI 回看历史媒体（缩略图/播放/下载）。
- **媒体元信息**：media 条目统一带归档 id；file 存 sanitize 后文件名，poke 存戳一戳类型；不存 URL/绝对路径/base64。
- **统一媒体位置 token 与纯媒体判定**：与文字同现的全部媒体/动作类型（image/video/voice/file/emoji/forward/poke）按原始位置写类型化 token；纯媒体/纯动作（消息链中无文本组件，判定看组件 kind）正文存英文占位（`[image]` 等），元信息保留在 media 数组。
- **捕获面扩展**：notice/request 空消息归类 `system_event` 并存可读摘要（如 `[撤回消息]`）；Unknown 段带文本按正文保留；assistant 链补 poke 分类；存量 `face` 数据迁移更名为 `emoji`。
- 测试：行为级 49 项 + 存储集成（v2→v5 迁移、归档表 CRUD、级联清理）。

## 1.2.4 — 2026-08-19
- **工具段严格跟随实际调用轮**：轮次不在当前上下文中的工具段直接丢弃（如配置 300 轮、工具调用发生在 400 轮之前——不存在的轮次不配拥有工具上下文），不再贴尾部、不按时间猜测；CM 无历史时不回放工具段。修复旧工具返回文案（如"正在拍摄"指令）被长期回放的问题。
- **keep_tool_turns 允许填 0**：0 = 关闭工具回放（不查库、不注入工具段），负值按 0。
- 测试：行为级 44 项 + 存储集成。

## 1.2.3 — 2026-08-18
- **工具段按轮次插入**：接管回放的工具调用不再追加尾部，改为插入对应轮次内部（该轮 user 之后、最终回复之前），与 AstrBot 原生历史顺序一致；轮次无匹配（主动消息自造轮次）回退尾部。
- **媒体占位**：白名单放行后纯媒体消息不再被丢弃，以 `<cm_image/>` / `<cm_voice/>` 等 cm 媒体标签进入上下文；混合消息在文本末尾补缺失标签。占位归入 cm_ 注入协议：通用规则已禁止输出任何 cm_ 标签，输出侧清洗会整元素删除 LLM 模仿的媒体标签残留。
- **日志配置迁移写回**：旧 `log_config.log_with_bot_id` 在顶层键未写入时自动继承，初始化时把旧组并入顶层并写回删除旧组，此后仅顶层键生效。
- 测试：行为级 43 项 + 存储集成（含工具段轮内插入与迁移验证）。

## 1.2.2 — 2026-08-17
- **工具调用入库与接管回放**：新增 `on_llm_tool_respond` 捕获，工具调用写入独立表 `chat_memory_tool_records`（schema v4），接管时按 `keep_tool_turns`（默认 2）回放为 OpenAI 格式追加到 contexts——修复启用接管后 LLM 跨轮看不到工具调用与返回、重复调用绘图工具的问题。二进制结果不入库，参数超长替换为合法 JSON 占位；`/reset` 与自动清理联动。
- **隐私**：昵称缺失时 `<cm_nickname>` / `<cm_reply target>` 不再回退 user_id（改用 `?` / `未知成员`），账号 ID 不进 LLM 上下文。
- **热路径小优化**：tool loop 重复 llm_status 升级改为幂等早退；接管复用 capture_user 缓存的 persona；删除冗余排序；`/reset` 合并 COUNT+DELETE。
- **日志配置**：删除 `log_config` 组与"日志提级"（改由 AstrBot WebUI 运行期调整插件日志等级，见知识库 `plugin-log-level.md`）；`log_with_bot_id` 保留为顶层配置项。
- 测试：行为级 38 项 + 存储集成（含 v2→v4 迁移与工具表生命周期）。

## 1.2.1 — 2026-08-04

### cm_ 标签彻底清理与输入侧注入防御
- **cm_ 泄漏清理改为整元素删除**：`strip_cm_xml_tags` 不再保留标签内文，所有 `<cm_*>` 标签连同其包裹的文本（时间戳/昵称/回复原文等）整体删除——cm_ 是注入元数据，任何形式的复现都不应进入存档。新增 `CM_ELEMENT_FULL_RE`（同名闭合标签后向引用 + DOTALL，迭代处理嵌套）、`CM_TIME_UNCLOSED_RE`（漏写闭合标签的 `<cm_time>` + 固定格式时间戳一并删除，末尾紧跟的第二个开口标签也配对吞掉），删除后合并句中多余空格。
- **发送消息同步清理**：`capture_bot` 对 LLM 回复只清洗一次——就地改写 `result.chain` 的 Plain 组件文本，落库与发送共用清理后的数据，用户看到的回复与存档一致，都不会带泄漏的 `<cm_*>` 结构（此前仅清理存档文本、不修改发送内容的旧行为由此变更）。
- **移除 builder 层全局 XML 转义**：无 relation_data 的消息正文不再 `xml_escape(quote=True)`，消除上下文中的 `&quot;` / `&lt;` 等实体污染，LLM 不再模仿输出实体。
- **输入侧注入防御**：用户侧伪造的 cm_ XML 在进上下文前清除（无 relation_data 的原始正文先过 `strip_cm_xml_tags`），伪造的 `<cm_source>` / `<cm_reply>` 等元数据不会以权威标签身份被 LLM 采信；有 relation_data 的正文仍由 storage 渲染可信 `<cm_mention>`，不受影响。
- **通用规则强化**：明确"不得在回复中输出任何 cm_ 标签或其内容"；`cm_mention` 补充"如需 @ 请检查工具列表是否提供对应工具，而非仿照该标签"。
- 测试新增 `user_forged_cm_tags_stripped`，共 31 个行为级用例。

## 1.2.0 — 2026-08-04

### 全量 XML 化、拆分模式与提示词体系重构
- **结构化标签全统一**为 `<cm_*>` 前缀：`cm_time`（所有消息统一时间元数据）/ `cm_speaker`（当前交互对象）/ `cm_nickname`（发送者昵称，原 `昵称:` 文本前缀）/ `cm_reply`（整条消息是对"某人"的回复事件，**仅出现在 user 消息**，assistant 的 Reply/At 关系暂未捕获）/ `cm_mention`（正文中 At 占位符，不等于回复）/ `cm_solo`（主动/未配对）/ `cm_current`（当前轮 Reply/At 结构锚）/ `cm_source`（跨会话来源，原 `<cm s="N"/>`）。历史 `[提及:昵称]` / `[回复 → 某人 | 原文: ...]` / `[当前发言者]` / `[主动]` 文本格式全部退役；标签仅在注入层生成，数据库 schema 与历史记录零迁移。
- **提示词重构为三条**：新增无条件注入的 `[ChatMemory 通用规则]`（cm_ 标签语义 + 时间说明 + 当前轮焦点 + 闲聊触发判定 + cm_ 标签不得学习/输出禁令）；`[群聊历史解释规则]` 只保留群聊归因（区分带 `<cm_source>` 的跨会话 user 与当前会话其他成员，消除 cross_session × full_group 归因冲突）；`[跨会话来源规则]` 保留来源语义与跨会话整合目的（**不同会话的事实/承诺/关系可合并参考**）。独立的当前轮焦点指令并入通用规则后删除。
- **拆分模式为唯一行为**（不再提供合并配置）：不做任何 pop/合并，仅"从尾取 N 条 + 字符上限"；字符预算以 `[user, assistant]` 为不可拆分单元（配对 assistant 必须连同其 user 一起取/舍），最新一轮本身超预算时允许整体保留，proactive/orphan（solo）可单个取；允许连续 user，由通用规则提示"最后一条 user 才是当前请求"。
- **删除 `<cm_history_tail/>` 标记机制**：仅由通用规则提示"contexts 均为历史，最后一条 user 才是当前请求"。
- **防强化与防注入**：storage 层 `render_content_template` 只对正文做 XML 转义、At 渲染 `<cm_mention>` 保持结构标签（避免二次转义破坏标签）；无 relation_data 的消息正文在 builder 层转义，防用户伪造 `<cm_*>` 标签（提示注入）；context_builder 仅转义自身引入的昵称/回复目标；提示词明确 cm_ 标签是结构化元数据、不得学习其格式或输出；闲聊触发判定（当前消息无 `cm_reply`/`cm_mention` 指向 assistant 时视为群聊闲聊触发）。
- **跨会话 Reply 回填修正**：跨群/私聊历史中的本地 Reply 按该历史消息原本所在的 UMO 分组查询目标，不再误用当前请求的 UMO；仍严格遵守平台 Reply 只能指向同一会话消息的边界。
- **assistant 入库防污染**：BOT 捕获 Hook 提升到 `priority=10000`，尽量在其他结果装饰插件之前读取原始 LLM 回复；落库前移除其中泄漏的 `<cm_*>` XML 标签，但不修改实际发送结果。
- 测试重写为行为级（30 个，本地无 AstrBot/sqlalchemy 依赖可跑）；storage 纯函数（`_scope_filter` / `_normalize_dt` / `_row_to_dict`）改为真执行验证而非源码文本断言；测试运行器失败时返回非零退出码。

## 1.1.4 — 2026-08-01

### 发言者标记 XML 化与归因强化

- 历史 contexts 中交互对象标记从方括号文本 `[当前发言者]` 改为 XML `<cm_speaker current="1"/>`（无 `user_id` 时的中性标记为 `<cm_speaker/>`），与 `<cm_current>` / `<cm s="N"/>` 风格统一；仅在注入层拼接，数据库 schema 与历史记录零迁移。
- 当前轮焦点锚 `<cm_current>` 内部新增 `<cm_speaker current="1">昵称</cm_speaker>`，与历史标记形成"同一人"绑定，缓解当前发言者被误当待续写角色、他人陈述被默认归给当前用户两类问题。
- full-group 固定解释规则补操作性归因约束：转述任何历史陈述必须注明发送者；不带 `<cm_speaker current="1"/>` 标记的陈述一律不得以"你/您/用户"转述，归属一律以昵称为准。
- `build_current_turn_xml()` 新增 `speaker_nickname` 参数；`inject_current_turn_focus` 从 event 取发送者昵称传入。

## 1.1.3 — 2026-07-29

### 中性来源 XML、当前轮 Reply/At 焦点与历史尾部隔离

- contexts 接管继续保持 `priority=-100`；新增 `priority=-1000` 的晚阶段焦点钩子，仅在 CM takeover 实际生效时运行，并在现有 `extra_user_content_parts` 末尾追加临时 `<cm_current>`。
- 普通消息使用最短 `<cm_current/>`；Reply/At 消息以 XML mixed content 保留 At 原始位置，指向 Bot 时统一写 `target="assistant"`，其他目标只写昵称，不泄露账号 ID。
- 不重复 AstrBot 已提供的 `<Quoted Message>` 引用全文，不给当前消息增加时间；当前锚使用 `mark_as_temp()`，不写入 native history 或 CM 数据库。
- 当最终历史 contexts 以 `role=user` 结尾时，临时追加 `<cm_history_tail/>`，固定规则明确最终 role=user 才是当前请求，禁止改答历史尾部或把交互对象续写/扮演成 assistant 自身。
- 仅在请求展示层清理旧 assistant 正文开头的 `[群N]` / `[私N]` / `[会N]` / `[未知]` 污染；不修改数据库，也不清理 user 原文或正文中间的同形文本。
- 数据库 schema 仍为 v3，无迁移、无新增配置；公开 `build_takeover_contexts()` 继续只返回历史 contexts，当前轮锚只由真实 LLM Hook 临时注入。
- 将重复且带场景语义的 `[群N]` / `[私N]` / `[会N]` 替换为中性 `<cm s="N"/>`；所有外部来源统一按首次出现顺序编号，不再向 LLM 暴露来源类型。
- 当前会话仍不增加来源元数据，字段不足时使用 `<cm s="?"/>`；paired assistant 继续继承紧邻 user，只有混合或独立 assistant 显式携带元数据。
- 固定规则明确 N 只是单次请求内的来源等价编号，不代表人物、时间顺序、重要性、场景焦点或回复目标，回答重点始终由当前请求决定。
- 数据库 schema 仍为 v3，不迁移、不重写既有记录；`build_takeover_contexts()` 的来源展示协议发生变化，调用方应把返回值视为 LLM-ready contexts，不应解析旧方括号标签。

## 1.1.2 — 2026-07-28

### 群聊交互对象与 assistant 视角切割

- 压缩并强化 full-group 固定解释规则：`[当前发言者]` 只解释一次，仅表示本轮交互对象过去说过的话，不是 assistant 的身份、视角或应续写扮演的角色。
- 对方即使是带角色扮演口吻的 Bot，也不得被 assistant 当作自己的姓名、自称、身份设定或第一人称视角；assistant 身份与口吻继续以原有 `system_prompt` 为准。
- 明确 `role=assistant` 是 assistant 自己的历史回复，应从中延续自身视角；不改 contexts 标签、消息正文或时间前缀格式。

## 1.1.1 — 2026-07-24

### 查询 API、群聊身份与跨会话来源精简

- `query_history()` / `query_rounds()` 返回只读 `record_id`，并新增 `after_id`；与 `since` 同时使用时按 `(created_at, id)` 严格向前分页，避免同时间戳跨页漏记录。
- 新增 `content_kind_all_match`，将 ALL 内容白名单直接下推 SQLite，第三方消费者无需全量扫描后再二次过滤。
- API 变更向后兼容，数据库 schema 仍为 v3，不迁移、不重写既有记录。
- 已知当前用户时，仅其历史发言保留 `[当前发言者]`；其他群成员不再重复添加 `[其他发言者]`，仍保留时间与实际昵称。
- 固定群聊解释规则明确“无当前标记即其他成员”，减少大量整群历史中的重复 token，同时继续防止把其他成员事实归到当前用户。
- 公开 `build_takeover_contexts(..., user_id="")` 无当前用户参照时仍给所有发言者使用中性 `[发言者]`，保持自描述性。
- `cross_session` 查询新增请求内匿名来源：当前会话零标记，其他群聊/私聊/其他会话分别使用 `[群N]` / `[私N]` / `[会N]`；同一来源在单次请求内稳定复用，不向 LLM 暴露群号、群名或 UMO。
- 其他来源的 user 使用短来源标记；paired assistant 由 role 与前一条 user 继承来源，不再重复添加来源 token；只有主动/孤立 assistant 保留来源标记。来源审计字段不足时使用 `[未知]`，不做昵称、时间或正文启发式归并。
- 仅在规整结果实际出现其他或未知来源时追加紧凑的跨会话解释规则；只有当前会话数据时不增加提示词 token。该增强不改数据库 schema、不迁移历史数据，也不增加配置项。

## 1.1.0 — 2026-07-22

### 群聊 Reply / At 关系与最旧端查询

- 数据库 schema 升至 v3，仅新增 nullable `relation_data`；旧记录不回填、不猜测，关系增强只对升级后的新数据生效。
- At 按 MessageChain 原始位置写入带索引模板，完整有序参数存入 `relation_data.mentions`；查询对外继续返回渲染后的 `content`，并新增 `content_template`。`at_id` 标记弃用，但 1.x 继续双写第一个普通 At 保持兼容。
- 普通成员 Reply 仅通过同一平台实例、同一 UMO 下唯一的 `reply_id → user.message_id → target_turn_id` 精确关联；引用 Bot 或无法唯一解析时保存最小快照，不使用 timestamp/content 启发式匹配。
- takeover 增加明确的 Reply/At 关系转录与防指令混淆说明，引用目标按 turn 批量读取，避免 N+1 查询。
- `query_history()` / `query_rounds()` 末尾新增 `from_oldest=False`；显式设为 `true` 时从最旧记录/轮次开始截取，返回结果仍保持时间正序。

## 1.0.1 — 2026-07-17

### 群聊身份与公开接管 API

- 新增只读公开 API `build_takeover_contexts()`，供主动消息等独立 LLM 调用完整复用当前 takeover 的 scope、过滤、固定身份前缀与预算配置；接管关闭返回 `None`，启用但无可用上下文返回 `[]`。
- 实际 `on_llm_request` 接管路径改为调用同一公开 API，避免内部接管与外部消费者行为分叉。
- 修复 `cross_session + full_group + 空 user_id` 会退化为整个平台 scope 的 P1：公开 API 强制限制为当前 UMO + CID，storage 层同时禁止空用户进入跨 UMO 查询。
- 删除可关闭身份信息的 `prefix_enhance` 配置，takeover user 历史统一强制使用时间 + 发送者前缀；旧配置字段直接忽略。
- full-group 群聊历史增加 `[当前发言者]` / `[其他发言者]` 标记；当前用户未知时使用 `[发言者]`。ChatMemory 自身接管同时向 `system_prompt` 幂等追加群聊归因规则。
- 本地回归测试扩充至 40 项，并通过 AstrBot 自带 SQLAlchemy/aiosqlite 的临时数据库集成验证。

## 1.0.0 — 2026-07-17

### 最终验证版

- 使用旧生产数据库的一致性快照完成真实增量迁移验证：原有行数及全部旧字段逐行一致，`PRAGMA integrity_check=ok`。
- 数据库 schema version 固定为 `2`，增量补齐 `persona_id`、`turn_id`、`send_status`，并校验所有索引绑定主表。
- 实时 user/assistant 状态流统一使用内部 `turn_id`；删除仅服务旧运行流程的 `message_id` 状态更新回退。
- 历史数据查询继续支持 `message_id` / `pair_id` 配对，确保现有数据库中的旧对话可读。
- assistant 写入与 user `llm_success` 升级处于同一事务；同一 `(turn_id, role)` 重放保持幂等。
- `send_status` 使用 `prepared → send_attempted`，仅表达发送流程，不宣称平台送达。
- takeover 默认严格接管，支持字符预算、persona 隔离、跨会话/整群 scope 和内容白名单。
- `max_content_length` 默认改为 `0`（不截断）；takeover 的 `limit_rounds` 只钳下限 1，不再限制上限。
- `query_rounds` 收紧为严格完整配对；persona / 时间窗口条件会同步约束 assistant，历史重复 assistant 取最早一条。
- 数据库迁移改为在 `Star.initialize()` 阶段执行，失败时释放连接并让 AstrBot 将插件标记为加载失败。
- 数据目录改用 `StarTools.get_data_dir("chat_memory")`，不再依赖工作目录或手工拼接宿主路径。
- takeover 混合状态模式排除当前 `turn_id`，避免本轮 user 同时进入历史 contexts 与当前 prompt。
- 完成模块拆分、配置说明、依赖声明、README、38 项本地回归测试，以及使用 AstrBot 自带 SQLAlchemy/aiosqlite 的临时数据库集成验证。

## 0.9.0 — 2026-07-15

### Persona、时间与一致性

- 新增 `persona_id` 存储与严格过滤，支持与 `cross_session` 组合使用。
- 查询 API 增加 `persona_id`、`since`、`until`，并提供明确的 `created_at_utc`。
- 存储统一使用 UTC naive；查询时按 AstrBot 配置时区转换。
- 修复跨 CID 配对、assistant 配对 key、reasoning 前缀污染和组件链读取问题。
- 增加 WAL `busy_timeout`、limit 钳制、`/reset` 审计和生命周期资源释放。

## 0.7.0 — 2026-07-12

### 查询与上下文范围

- `cross_session` 升级为跨 UMO 的群私聊互通，并与 `full_group` 形成四种 scope 组合。
- 内容白名单下沉 SQL，支持 ANY / ALL 两种匹配语义。
- `limit_rounds` 在纯配对模式表示轮数，在混合状态模式表示消息数。
- assistant 端补齐图片、视频、语音、文件等内容分类。

## 0.5.0 — 2026-07-09

### 上下文接管

- 增加可选 `context_takeover`，覆盖 LLM contexts 并按配置清理 native history。
- 支持状态过滤、时间/发送者前缀、主动消息和 orphan 标记。
- 接管结果执行配对、规整、纯媒体过滤及边界裁剪。
- SQLite 启用 WAL 与 `synchronous=NORMAL`。

## 0.3.0 — 2026-07-03

### 全量捕获与双列状态

- 从仅记录 LLM 对话扩展为捕获所有进入 ProcessStage 的 user 消息及 BOT 回复。
- 增加消息配对字段和平台审计字段。
- 将早期单一 tag 拆为 `llm_status` 与 `content_kind`，修正命令、纯媒体、At、Reply 等分类。
- 群聊查询支持混合返回当前会话中的多用户记录。

## 0.1.0 — 2026-06-13

### 初始测试版

- 以 `UMO + conversation_id + user_id` 为维度，将对话异步存入 SQLite。
- 提供基础历史查询接口。
