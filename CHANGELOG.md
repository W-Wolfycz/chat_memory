# Changelog

ChatMemory 在 `1.0.0` 前均视为内部测试版。以下版本号是对原开发历史的重新压缩，不对应旧仓库曾使用的版本号；数据库 schema 版本独立维护，当前仍为 `2`。

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
