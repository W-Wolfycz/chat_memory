# Changelog

ChatMemory 在 `1.0.0` 前均视为内部测试版。以下版本号是对原开发历史的重新压缩，不对应旧仓库曾使用的版本号；数据库 schema 版本独立维护，当前为 `3`。

> 下方各版本记录的是该版本发布时的实际格式。旧条目中的 `[当前发言者]`、`<cm s="N"/>`、`<cm_history_tail/>` 等均为已经退役的历史协议，不代表当前版本仍会生成或接受这些标签；当前格式以最新版本条目和 README 为准。

## 1.2.1 — 2026-08-04

### cm_ 标签彻底清理与输入侧注入防御

- **cm_ 泄漏清理改为整元素删除**：`strip_cm_xml_tags` 不再保留标签内文，所有 `<cm_*>` 标签连同其包裹的文本（时间戳/昵称/回复原文等）整体删除——cm_ 是注入元数据，任何形式的复现都不应进入存档。新增 `CM_ELEMENT_FULL_RE`（同名闭合标签后向引用 + DOTALL，迭代处理嵌套）、`CM_TIME_UNCLOSED_RE`（漏写闭合标签的 `<cm_time>` + 固定格式时间戳一并删除，末尾紧跟的第二个开口标签也配对吞掉），删除后合并句中多余空格。
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
