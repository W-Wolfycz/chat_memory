"""chat_memory — 多 BOT 合并存储（阶段一：影子双写，只写不读）。

问题：同一 AstrBot 进程里多个 BOT（platform 实例）都会收到同一条群消息，
旧表 ``chat_memory_records`` 按 (umo, cid) 存，于是同一条消息被每个实例各存一份
（线上实测：33.9 万行里 97.6% 落在多实例会话，平均 2.48 份/条）。

本模块把"客观的消息本体"与"主观的观察关系"拆成两层，**消息本体以平台共享 id 为主键**：

- ``chat_memory_messages``：物理消息，主键 ``msg_uid``。
  - 平台提供共享 id 时（NapCat OB11 事件的 ``real_seq``＝服务器 ``msgSeq``）：
    ``p:{platform_name}:{message_type}:{session_id}:{real_seq}`` —— **各实例算出同一个值**。
  - 客户端不提供时退化为本地确定性键（内容指纹 + 同秒出现序号），前缀 ``l:``。
  - 其余列为消息客观属性（发送者、平台时间、正文、媒体关系…）。
- ``chat_memory_message_bots``：参与行，一行 = 某实例在某会话里看到/发出了这条消息，
  外键 ``msg_uid``；每实例状态（cid、role、llm_status、send_status、turn_id…）在此。
- ``chat_memory_tool_calls``：工具调用，锚到 ``msg_uid``（可空）。
- ``chat_memory_shadow_meta``：影子表自身 schema 版本。

阶段一：旧表照旧写，同时尽力镜像到新表，不参与任何读路径；失败隔离在调用方
（``DBManager._shadow_write``）。切换读取（阶段二）前必须先用
``integration/shared_store_audit.py`` 对拍确认零差异。

字段命名约定：``msg_uid`` 是消息主键（跨实例共享或本地确定性）；``platform_message_id``
是该实例眼中的平台消息 id（实测跨实例一致率 0.03%，只用于撤回/引用定位）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from astrbot.api import logger

from . import shadow_diag

# 影子表自身的 schema 版本（记在 chat_memory_shadow_meta，不动旧库 user_version）。
#
# 它是**内部结构线**，从 `1` 起算：合并存储随本版第一次发布，之前那些数字只是开发期在
# 测试端来回改了十几次的内部编号，没有对外含义，也不该让第一个正式版本背着一串历史。
# 以后每次改影子层结构就 +1（**不要跳号**）；与插件版本号、旧表 `PRAGMA user_version`
# 都不对齐。结构变更走 `SHADOW_MIGRATIONS`（累积应用），**不再丢弃重建**。
# 6：工具表唯一键收窄到 `(turn_id, call_index)`、计划表主键收窄到 `turn_id`、
#    删掉 `messages.reply_group` 与两张无匹配查询的索引、给计划表/工具表/元信息表加 CHECK。
SHADOW_SCHEMA_VERSION = 6

# 单条 SQL 的绑定参数上限（SQLite 默认 999），孤儿回收按批处理。
_SQL_PARAM_CHUNK = 500

# 无平台共享 id 时，同秒同文最多派生多少个序号位（超出退回 message_id 派生，宁多不丢）。
_OCCURRENCE_LIMIT = 8

# 撤回通知常早于目标消息落库：当场标不上由 main 的退避重试兜住（0.5/1.5/3/8 秒），
# **不再**在加载/日清时扫事件行补标（那是"扫历史"，线上 415 个事件要 4 分钟，且只能
# 补救"撤回先到"这一种时序，退避重试覆盖得更好）。

# 群名快照表：每个 (平台, 会话) 一行，值是**最近一次**从平台读到的群名。
# 它不是消息事实——消息表不存群名，历史消息靠 session_id 关联这张表拿显示名；
# 也不保留改名历史（改名直接覆盖 updated_at）。离线库/快照库里也能把群号翻成名字。
_GROUP_NAMES_TABLE = """CREATE TABLE IF NOT EXISTS chat_memory_group_names (
    platform_name TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    group_name    TEXT NOT NULL DEFAULT '',
    updated_at    DATETIME NOT NULL,
    PRIMARY KEY (platform_name, session_id)
)"""

UPSERT_GROUP_NAME_SQL = (
    "INSERT INTO chat_memory_group_names "
    "(platform_name, session_id, group_name, updated_at) "
    "VALUES (:platform_name, :session_id, :group_name, :updated_at) "
    "ON CONFLICT(platform_name, session_id) DO UPDATE SET "
    "group_name = excluded.group_name, updated_at = excluded.updated_at "
    "WHERE excluded.group_name <> chat_memory_group_names.group_name"
)

SELECT_GROUP_NAMES_SQL = (
    "SELECT session_id, group_name FROM chat_memory_group_names "
    "WHERE group_name <> ''"
)

_TABLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS chat_memory_messages (
    msg_uid             TEXT PRIMARY KEY,
    fingerprint         TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    platform_name       TEXT NOT NULL,
    message_type        TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    sender_nickname     TEXT NOT NULL DEFAULT '',
    platform_timestamp  INTEGER NOT NULL,
    content             TEXT NOT NULL DEFAULT '',
    content_kind        TEXT NOT NULL DEFAULT '[]',
    relation_data       TEXT,
    recalled_at         DATETIME,
    deleted_at          DATETIME,
    platform_seq        INTEGER,
    created_at          DATETIME NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS chat_memory_message_bots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid             TEXT NOT NULL,
    platform_id         TEXT NOT NULL,
    self_id             TEXT NOT NULL DEFAULT '',
    conversation_id     TEXT NOT NULL,
    umo                 TEXT NOT NULL,
    role                TEXT NOT NULL,
    platform_message_id TEXT,
    turn_id             TEXT,
    llm_status          TEXT NOT NULL DEFAULT '',
    send_status         TEXT NOT NULL DEFAULT '',
    persona_id          TEXT,
    target_id           TEXT,
    raw_timestamp       INTEGER,
    created_at          DATETIME NOT NULL,
    UNIQUE (msg_uid, platform_id, self_id, conversation_id)
)""",
    """CREATE TABLE IF NOT EXISTS chat_memory_tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid         TEXT,
    umo             TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    call_index      INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_args       TEXT NOT NULL DEFAULT '',
    tool_result     TEXT NOT NULL DEFAULT '',
    created_at      DATETIME NOT NULL,
    deleted_at      DATETIME,
    -- 幂等键＝「哪一轮的第几次调用」。turn_id 是发送侧生成的 uuid4（全局唯一），
    -- 不含 conversation_id：一轮的调用不可能跨 cid 重复，把 cid 放进来只会让索引更宽、
    -- 并让 `(umo, turn_id, call_index)` 这种查询无法命中（cid 在键中间）。
    -- 按会话取工具改用 ix_cmtc_umo_turn。
    UNIQUE (turn_id, call_index),
    CHECK (call_index >= 1)
)""",
    """CREATE TABLE IF NOT EXISTS chat_memory_shadow_meta (
    -- 只允许已知的键：以前是无约束 KV，键名拼错会静默插入一个新键
    -- （症状：双写窗口"未满"、切读永远不生效，而日志里一切正常）。
    key   TEXT PRIMARY KEY CHECK (key IN ('schema_version', 'dual_write_started_at',
                                          'shadow_migrate')),
    value TEXT NOT NULL
)""",
)

_SESSION_SEQ_TABLE = """CREATE TABLE IF NOT EXISTS chat_memory_session_seq (
    platform_name TEXT NOT NULL,
    message_type  TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    updated_at    DATETIME NOT NULL,
    PRIMARY KEY (platform_name, message_type, session_id)
)"""

# 会话序号分配：只增不减（会话被删空后不回到 1，外部读者的游标永远向前）。
SEQ_ALLOCATE_SQL = (
    "INSERT INTO chat_memory_session_seq "
    "(platform_name, message_type, session_id, last_seq, updated_at) "
    "VALUES (:platform_name, :message_type, :session_id, 1, :created_at) "
    "ON CONFLICT (platform_name, message_type, session_id) DO UPDATE SET "
    "last_seq = last_seq + 1, updated_at = excluded.updated_at "
    "RETURNING last_seq"
)

# 物理消息插入（seq 由调用方先分配好再传进来）。
INSERT_MESSAGE_SQL = (
    "INSERT OR IGNORE INTO chat_memory_messages "
    "(msg_uid, fingerprint, seq, platform_name, message_type, session_id, "
    "sender_id, sender_nickname, platform_timestamp, content, content_kind, "
    "relation_data, platform_seq, created_at) "
    "VALUES (:msg_uid, :fingerprint, :seq, "
    ":platform_name, :message_type, :session_id, :sender_id, "
    ":sender_nickname, :platform_timestamp, :content, :content_kind, "
    ":relation_data, :platform_seq, :created_at)"
)

_INDEX_STATEMENTS = (
    # 会话内序号：排序/游标的公共键，也承担"按会话取消息"的扫描前缀。
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cmm_session_seq "
    "ON chat_memory_messages (platform_name, message_type, session_id, seq)",
    # 段序：同一会话按平台序列号（real_seq）排序＝平台投递顺序（三条到货路径同一个值）。
    "CREATE INDEX IF NOT EXISTS ix_cmm_session_pseq "
    "ON chat_memory_messages (platform_name, message_type, session_id, platform_seq)",
    # 内容指纹：只在平台共享 id 缺失时用于归并。**不建索引**：唯一真实的查询形态是
    # `WHERE fingerprint=? AND platform_name=? AND message_type=? AND session_id=?`，
    # 优化器会选 ix_cmm_session_sender（前 3 列命中），单列 fingerprint 索引从未被用上
    # （结构线 6 删除，只留注释说明为什么不加回来）。
    # 时间序：撤回重放、保留期清理、`dual_write_started_at` 的 MIN(created_at)
    # 都按时间扫；没有这条索引等于每次启动/日清都全表扫 + 排序。
    "CREATE INDEX IF NOT EXISTS ix_cmm_created ON chat_memory_messages (created_at)",
    # 会话内按时间排序：查询台列表、去重与分页都按 created_at 倒序取页。
    # ux_cmm_session_seq 的前缀是 (pn,mt,sid) 但末列是 seq，服务不了 created_at 排序
    # （实测走 TEMP B-TREE），ix_cmm_created 的前导列又不是会话——所以需要这条。
    "CREATE INDEX IF NOT EXISTS ix_cmm_session_created "
    "ON chat_memory_messages (platform_name, message_type, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_cmmb_created "
    "ON chat_memory_message_bots (created_at)",
    # 出站行昵称补齐：按会话+发送者取最近昵称。
    "CREATE INDEX IF NOT EXISTS ix_cmm_session_sender "
    "ON chat_memory_messages (platform_name, message_type, session_id, sender_id)",
    "CREATE INDEX IF NOT EXISTS ix_cmmb_view "
    "ON chat_memory_message_bots (platform_id, self_id, conversation_id, msg_uid)",
    # 按 `(platform_id, self_id, platform_message_id)` 查在代码里**没有消费者**（结构线 6
    # 删除）：撤回/软删定位走的是 `(umo, platform_message_id)` → 命中 ix_cmmb_umo_turn；
    # 精确参与行定位走 `(msg_uid, platform_id, self_id, conversation_id)` → 命中唯一键。
    "CREATE INDEX IF NOT EXISTS ix_cmmb_umo_turn "
    "ON chat_memory_message_bots (umo, conversation_id, turn_id)",
    # 按 mid 定位参与行：撤回（实时 + 启动补标）、软删除镜像、表情回应注释都走
    # `WHERE umo = ? AND platform_message_id = ?`。没有这条索引时每次要扫该 umo 的全部
    # 参与行（实测线上 415 个撤回事件累计 4 分钟，把插件加载卡死——这就是"升级卡在更新中"
    # 的真正原因）。部分索引：mid 有 13% 为空，空值不参与查找。
    "CREATE INDEX IF NOT EXISTS ix_cmmb_umo_mid "
    "ON chat_memory_message_bots (umo, platform_message_id) "
    "WHERE platform_message_id IS NOT NULL",
    # 注意：这条名义上是唯一键 `(msg_uid, platform_id, self_id, conversation_id)` 的前缀冗余，
    # 但它目前是 `_VIEW_CTE` 里 `ROW_NUMBER() OVER (PARTITION BY msg_uid, umo …)` 的扫描
    # 顺序来源（SCAN b USING INDEX ix_cmmb_msg）。删掉不会算错，但会多一层 B-TREE 排序，
    # 所以在"窗口查询下推"重写之前先保留。
    "CREATE INDEX IF NOT EXISTS ix_cmmb_msg "
    "ON chat_memory_message_bots (msg_uid)",
    # 「这一轮有没有 assistant 行」是所有配对判断的核心查询：没有这个索引时，
    # 配对筛选会退化成 81k × 30k 的相关子查询（实测单次 195 秒）。
    "CREATE INDEX IF NOT EXISTS ix_cmmb_turn_role "
    "ON chat_memory_message_bots (turn_id, role)",
    "CREATE INDEX IF NOT EXISTS ix_cmtc_msg ON chat_memory_tool_calls (msg_uid)",
    # 工具页/回放按「会话 + 轮次」取（`(umo, conversation_id, turn_id)` 前缀），
    # 原来靠唯一键的前 3 列；唯一键收窄到 `(turn_id, call_index)` 后由这条接上。
    "CREATE INDEX IF NOT EXISTS ix_cmtc_umo_turn "
    "ON chat_memory_tool_calls (umo, conversation_id, turn_id)",
)


# 历史上出现过、现已从代码里删掉的影子表：每次加载**无条件清理**。
#
# 为什么需要：结构升级时的"丢弃重建"只会删**当前**表清单里的表；一张表被从代码里
# 删掉后，它的名字也从 drop 清单里消失了 → 旧库会永远留着这张无人维护的表（实测线上
# 留了 314 行陈旧的 send_intents + 10 行 conversation_bots），导出的 DB 看起来像
# 还有两套事实源。以后删表时，把表名加到这里即可。
_OBSOLETE_TABLES = (
    "chat_memory_send_intents",       # 轮次状态改由 reply_messages 派生
    "chat_memory_conversation_bots",  # 会话×实例改由参与行派生
    "chat_memory_shadow_seen",        # 到达侧普查与 shadow_events 重复，已删
    "chat_memory_shadow_logs",        # 插件日志落库（调试用）已删
    "chat_memory_shadow_stats",       # 写入计数（纯诊断）已删
    "chat_memory_shadow_events",      # 事件形态聚合（纯诊断）已删
    "chat_memory_echo_channels",      # 供货状态（纯诊断）已删
    "chat_memory_shadow_anomalies",   # 撤回补标改由事件行派生，表已删
)


def _obsolete_table_statements() -> list[str]:
    return [f"DROP TABLE IF EXISTS {name}" for name in _OBSOLETE_TABLES]


# 影子层结构**迁移**（数据保留）：认识的旧编号 → 幂等 SQL 步骤。
#
# 合并存储随本版第一次发布，内部结构线从 `1` 起算——没有"从很远的旧编号跳过来"的历史，
# 也不为开发期编号留兼容。以后每次改结构：版本 +1，能 ALTER / 建索引解决的都往这里加一条，
# 不要让用户丢数据；不认识的编号才回落到"丢弃重建"（见 `storage.init_db`，重建也会沿用
# 切读窗口起点）。
SHADOW_MIGRATIONS: dict[str, tuple[str, ...]] = {
    # 5 → 6：收窄两个唯一键、删掉只写不读的列与无消费者的索引、给三张表补 CHECK。
    # 全部走"建新表 → 搬 → 改名"（SQLite 改不了主键/唯一键/约束），数据一行不丢：
    # 重复 turn_id 保留最早那条；非法 JSON / 非法状态值归一成默认值再搬（不是丢弃）。
    "5": (
        # messages.reply_group 只写不读（读路径不依赖它，组键由 rg:<turn_id> 现推），
        # 先删它的索引再删列；DROP COLUMN 不重建表，`rowid` 保持不变
        # （查询台的 `record_id = rowid + 1e9` 契约因此不受影响，已实测）。
        "DROP INDEX IF EXISTS ix_cmm_reply_group",
        "ALTER TABLE chat_memory_messages DROP COLUMN reply_group",
        # 两条与代码里真实查询形态不匹配的索引（理由见 _INDEX_STATEMENTS 的注释）。
        "DROP INDEX IF EXISTS ix_cmm_fingerprint",
        "DROP INDEX IF EXISTS ix_cmmb_platform_mid",
        # 工具表唯一键 (umo, cid, turn_id, call_index) → (turn_id, call_index)：
        # turn_id 是 uuid4，一轮的调用不可能跨 cid 重复。显式带 id 搬迁，保住工具页的
        # tool_id（AUTOINCREMENT 序列会从 max(id) 自动接续）。
        """CREATE TABLE IF NOT EXISTS chat_memory_tool_calls_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_uid         TEXT,
            umo             TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            turn_id         TEXT NOT NULL,
            call_index      INTEGER NOT NULL,
            tool_name       TEXT NOT NULL,
            tool_args       TEXT NOT NULL DEFAULT '',
            tool_result     TEXT NOT NULL DEFAULT '',
            created_at      DATETIME NOT NULL,
            deleted_at      DATETIME,
            UNIQUE (turn_id, call_index),
            CHECK (call_index >= 1)
        )""",
        "INSERT OR IGNORE INTO chat_memory_tool_calls_new "
        "(id, msg_uid, umo, conversation_id, turn_id, call_index, tool_name, "
        " tool_args, tool_result, created_at, deleted_at) "
        "SELECT id, msg_uid, umo, conversation_id, turn_id, call_index, tool_name, "
        "       tool_args, tool_result, created_at, deleted_at "
        "  FROM chat_memory_tool_calls WHERE call_index >= 1 ORDER BY id",
        "DROP TABLE IF EXISTS chat_memory_tool_calls",
        "ALTER TABLE chat_memory_tool_calls_new RENAME TO chat_memory_tool_calls",
        # 计划表主键 (umo, turn_id) → turn_id（单列）：turn_id 全局唯一，主键自带索引，
        # 省掉一份 umo 的重复键；顺带删掉只写不读的 instance_ts。
        """CREATE TABLE IF NOT EXISTS chat_memory_reply_messages_new (
            turn_id         TEXT PRIMARY KEY,
            umo             TEXT NOT NULL,
            conversation_id TEXT NOT NULL DEFAULT '',
            persona_id      TEXT NOT NULL DEFAULT '',
            created_at      DATETIME NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            content_kind    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(content_kind)),
            llm_status      TEXT NOT NULL DEFAULT '',
            send_status     TEXT NOT NULL DEFAULT ''
                            CHECK (send_status IN ('', 'prepared', 'send_attempted')),
            deleted_at      DATETIME
        )""",
        "INSERT OR IGNORE INTO chat_memory_reply_messages_new "
        "(turn_id, umo, conversation_id, persona_id, created_at, content, content_kind, "
        " llm_status, send_status, deleted_at) "
        "SELECT turn_id, umo, conversation_id, persona_id, created_at, content, "
        "       CASE WHEN json_valid(content_kind) THEN content_kind ELSE '[]' END, "
        "       CASE WHEN llm_status IN ('', 'llm_pending', 'llm_success', 'proactive', "
        "                               'orphan') THEN llm_status ELSE '' END, "
        "       CASE WHEN send_status IN ('', 'prepared', 'send_attempted') "
        "            THEN send_status ELSE '' END, "
        "       deleted_at "
        "  FROM chat_memory_reply_messages ORDER BY created_at",
        "DROP TABLE IF EXISTS chat_memory_reply_messages",
        "ALTER TABLE chat_memory_reply_messages_new RENAME TO chat_memory_reply_messages",
        # 元信息表补键白名单：拼错键名以后会被库直接拒绝（原来是静默新增一个键 →
        # "双写窗口未满"、切读永远不生效，而日志里一切正常）。
        """CREATE TABLE IF NOT EXISTS chat_memory_shadow_meta_new (
            key   TEXT PRIMARY KEY CHECK (key IN ('schema_version', 'dual_write_started_at',
                                                  'shadow_migrate')),
            value TEXT NOT NULL
        )""",
        "INSERT OR IGNORE INTO chat_memory_shadow_meta_new (key, value) "
        "SELECT key, value FROM chat_memory_shadow_meta "
        " WHERE key IN ('schema_version', 'dual_write_started_at', 'shadow_migrate')",
        "DROP TABLE IF EXISTS chat_memory_shadow_meta",
        "ALTER TABLE chat_memory_shadow_meta_new RENAME TO chat_memory_shadow_meta",
    ),
    # 2 → 3：`seq` 从"会话内 MAX+1"改成"会话计数器只增不减"。
    # 不重写已有 seq（外部读者的游标继续有效），只把计数器种到当前最大值。
    # 3 → 4：补齐按时间扫描的索引（只建索引，不动数据、不重置切读窗口）。
    # 4 → 5：新增群名快照表；顺手删掉与唯一键重复的 ix_cmtc_turn 与
    # ix_cmm_reply_group 的部分索引（该列在 5 → 6 里被删掉）。只改索引/建表，不动数据。
    "4": (
        _GROUP_NAMES_TABLE,
        "DROP INDEX IF EXISTS ix_cmtc_turn",
        "DROP INDEX IF EXISTS ix_cmm_reply_group",
        # 消息成员行（认领行）不再单独存：归属就在 assistant 参与行的 turn_id 上，
        # 这些行里 content/relation_data/llm_status/send_status/deleted_at 全为空。
        "DELETE FROM chat_memory_reply_messages WHERE msg_uid IS NOT NULL",
        "DROP INDEX IF EXISTS ux_cmrm_owner",
        "DROP INDEX IF EXISTS ix_cmrm_msg",
        # 计划表改成「一轮一条」：主键 (umo, turn_id)，去掉恒为 NULL 的 msg_uid 与
        # 从来没人写的 relation_data。SQLite 改不了主键，走建新表 → 搬 → 改名。
        """CREATE TABLE IF NOT EXISTS chat_memory_reply_messages_new (
            umo             TEXT NOT NULL,
            turn_id         TEXT NOT NULL,
            conversation_id TEXT NOT NULL DEFAULT '',
            persona_id      TEXT NOT NULL DEFAULT '',
            instance_ts     INTEGER NOT NULL DEFAULT 0,
            created_at      DATETIME NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            content_kind    TEXT NOT NULL DEFAULT '[]',
            llm_status      TEXT NOT NULL DEFAULT '',
            send_status     TEXT NOT NULL DEFAULT '',
            deleted_at      DATETIME,
            PRIMARY KEY (umo, turn_id)
        )""",
        "INSERT OR REPLACE INTO chat_memory_reply_messages_new "
        "(umo, turn_id, conversation_id, persona_id, instance_ts, created_at, "
        " content, content_kind, llm_status, send_status, deleted_at) "
        "SELECT umo, turn_id, conversation_id, persona_id, instance_ts, created_at, "
        "       content, content_kind, llm_status, send_status, deleted_at "
        "FROM chat_memory_reply_messages",
        "DROP TABLE IF EXISTS chat_memory_reply_messages",
        "ALTER TABLE chat_memory_reply_messages_new "
        "RENAME TO chat_memory_reply_messages",
        # 消息表：origin 恒为 inbound、读侧也没人用，删列（7 条开发期残留不再区分）
        "ALTER TABLE chat_memory_messages DROP COLUMN origin",
    ),
    "3": (
        "CREATE INDEX IF NOT EXISTS ix_cmm_created "
        "ON chat_memory_messages (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_cmmb_created "
        "ON chat_memory_message_bots (created_at)",
    ),
    "2": (
        _SESSION_SEQ_TABLE,
        "INSERT OR IGNORE INTO chat_memory_session_seq "
        "(platform_name, message_type, session_id, last_seq, updated_at) "
        "SELECT platform_name, message_type, session_id, MAX(seq), "
        "       strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "  FROM chat_memory_messages GROUP BY platform_name, message_type, session_id",
    ),
}


def shadow_migration_statements(recorded_version: str) -> Optional[tuple[str, ...]]:
    """按库上记录的版本给出**到当前版本为止的完整**迁移步骤；``None`` = 不认识。

    必须累积：``SHADOW_MIGRATIONS`` 每个键只存"该版到下一版"的增量，只执行一层就把
    ``schema_version`` 写成当前值，会让从结构线 2/3 升上来的库跳过 3→4、4→5
    （``reply_messages`` 不重建 → 随后所有 ``ON CONFLICT (umo, turn_id)`` 写都失败）。
    """
    try:
        start = int(str(recorded_version or ""))
        target = int(SHADOW_SCHEMA_VERSION)
    except (TypeError, ValueError):
        return None
    if start > target:
        # 记录的是**比当前代码更新**的结构线（回滚到旧版本代码）：不认识就丢弃重建，
        # 不能返回空步骤集——那会把版本号直接写小、留下一张半新不旧的表。
        return None
    if start == target:
        return ()
    steps: list[str] = []
    for version in range(start, target):
        step = SHADOW_MIGRATIONS.get(str(version))
        if step is None:
            # 链断了（中间某个版本的迁移没写）就当作不认识：调用方丢弃重建，
            # 而不是"跳过一层继续退"——那会留下半新半旧的结构。
            return None
        steps.extend(step)
    return tuple(steps)


def shadow_schema_statements() -> list[str]:
    """建表在前、建索引在后（旧库教训：索引不得早于其列存在，有单测守卫）。"""
    return [
        *_obsolete_table_statements(),
        *_TABLE_STATEMENTS,
        _SESSION_SEQ_TABLE,
        _GROUP_NAMES_TABLE,
        *shadow_diag.diag_schema_statements(),
        *_INDEX_STATEMENTS,
    ]


def drop_shadow_statements() -> list[str]:
    """影子表结构升级时丢弃重建（阶段一明确不迁移影子数据）。"""
    return [
        *shadow_diag.diag_drop_statements(),
        "DROP TABLE IF EXISTS chat_memory_tool_calls",
        "DROP TABLE IF EXISTS chat_memory_message_bots",
        "DROP TABLE IF EXISTS chat_memory_messages",
        "DROP TABLE IF EXISTS chat_memory_session_seq",
        "DROP TABLE IF EXISTS chat_memory_shadow_meta",
    ]


def split_umo(umo: str) -> tuple[str, str, str]:
    """拆 ``platform_id:message_type:session_id``；缺段返回空串。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", ""


def fingerprint(
    platform_name: str,
    message_type: str,
    session_id: str,
    sender_id: str,
    timestamp: int,
    content: str,
) -> str:
    """内容指纹：平台会话 + 发送者 + **平台**秒级时间 + 正文。

    实测依据（线上 33.9 万行）：平台 ``message_id`` 跨实例一致率仅 0.03%，不能做主键；
    仅用 (会话, 发送者, 秒) 时 8.54% 的键含多种正文，会误并。
    时间必须用平台自带时间：AstrBot 的 ``AstrMessageObj.timestamp`` 是本地接收时刻，
    实测 1.4% 的多实例消息跨秒不同，会拆行。
    """
    raw = "\x1f".join(
        [
            str(platform_name or ""),
            str(message_type or ""),
            str(session_id or ""),
            str(sender_id or ""),
            str(int(timestamp)),
            str(content or ""),
        ]
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def platform_msg_uid(
    platform_name: str, message_type: str, session_id: str, platform_uid: str
) -> str:
    """平台共享 id 构成的主键（各实例相同）。"""
    return f"p:{platform_name}:{message_type}:{session_id}:{platform_uid}"


def local_msg_uid(fingerprint_value: str, occurrence: int) -> str:
    """无平台共享 id 时的本地确定性主键（同指纹 + 出现序号）。"""
    return f"l:{fingerprint_value}:{occurrence}"


# 到货渠道（push / pull / 观察）**不落任何字段**：三条通道写的是同一条平台行
# （同 real_seq）。"这条是发送前抓到的计划正文"只作为写入前的路由标记（is_plan），
# 计划正文进轮次表，不进消息表——所以消息表里没有"收/发"之分，看参与行的 role 即可。


def normalize_record(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把旧表 ``insert`` 的绑定参数归一化为 ``{"message": …, "participation": …}``。

    返回 ``None`` 表示缺少物理身份（platform_name / message_type / session_id /
    platform_id / 发送者 / 平台时间戳），无法合并——调用方计入 skipped 而不是猜测。
    出站（assistant）的发送者是 ``self_id``，入站是 ``user_id``。
    """
    role = str(params.get("role") or "")
    user_id = str(params.get("uid") or "")
    self_id = str(params.get("self_id") or "")
    raw_platform_uid = str(params.get("puid") or "")
    umo_platform_id, umo_message_type, umo_session_id = split_umo(params.get("umo"))
    platform_id = str(params.get("pid_plat") or umo_platform_id or "")
    platform_name = str(params.get("pname") or "")
    message_type = str(params.get("mtype") or umo_message_type or "")
    session_id = str(params.get("sid") or umo_session_id or "")
    sender_id = self_id if role == "assistant" else user_id
    raw_timestamp = params.get("rts")
    if not (platform_id and platform_name and message_type and session_id and sender_id):
        return None
    try:
        receipt_ts = int(raw_timestamp)
    except (TypeError, ValueError):
        return None
    # 归一用的时间优先取平台自带时间（pts，跨实例一致）；取不到才回落本地接收时刻。
    try:
        platform_ts = int(params["pts"]) if params.get("pts") is not None else receipt_ts
    except (TypeError, ValueError):
        platform_ts = receipt_ts
    content = str(params.get("content") or "")
    is_echo = str(params.get("post_type") or "") == "message_sent"
    # 「这一条是不是计划正文」只是**写入前的路由标记**，不落库：发送路径的 assistant
    # 正文是计划（进轮次表），其余（入站观察 / push 回显 / pull 拉回）都是平台消息。
    is_plan = role == "assistant" and not is_echo
    # 出站行的 sender_id 是 BOT 自己，但旧表 audit 里的 sender_nickname 取的是 event
    # 发送者（＝被回复的用户）昵称，不能搬成"发送者昵称"。留空，等观察侧补真实昵称。
    nickname = "" if role == "assistant" else str(params.get("nick") or "")
    # 出站（发送路径）行的时间用**本次发送尝试时刻**（本地写入时刻），不用触发它的那条
    # 用户消息时间：出站行没有平台 id，它的时间只服务合并视图排序与"这一轮是什么时候
    # 说出口的"。观察侧分段在发送后几秒到达，锚点必须是发送时刻才套得住
    # （线上实测：297 条观察副本里 103 条因为锚点提前 20~30 秒而排到别的轮次前面）。
    # 本地键指纹仍用触发侧平台时间：同一轮回复重复镜像时才能算出同一个 uid（幂等）。
    is_outbound = role == "assistant" and not is_echo
    send_ts = receipt_ts
    if is_outbound:
        now = params.get("now")
        if isinstance(now, datetime):
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            send_ts = int(now.timestamp())
    return {
        "message": {
            "fingerprint": fingerprint(
                platform_name, message_type, session_id, sender_id, platform_ts, content
            ),
            # 平台共享 id（各实例相同）；客户端不提供时为空，归一时退化到内容指纹。
            # 注意 payload 键名用 puid：旧表 payload 的 uid 是"发送者 user_id"，不能复用。
            #
            # **出站（assistant）行绝不采用它**：发送路径上 event 是"触发这条回复的用户消息"，
            # 其 real_seq 属于那条用户消息。照搬会让回复与用户消息共享 msg_uid，从而折进同一行
            # （role 被升级成 assistant、回复正文因"只补空"被丢弃，user 行 llm_status 也再
            # 改不到）。线上实测：777 条回复正文丢失 + 777 条 user llm_success 未镜像。
            # 出站行里只有**平台回显**（post_type=message_sent）才带自己真实的 real_seq；
            # 发送路径（on_decorating）拿不到平台 id，其 event 上的 real_seq 属于触发它的
            # 用户消息，必须丢弃（见上方长注释的线上事故）。
            "platform_uid": (
                (str(params.get("puid") or "") or None)
                if (role != "assistant" or is_echo)
                else None
            ),
            # inbound = 平台上真实存在的消息；outbound = 发送路径的计划正文（不进消息表）。
            "is_plan": is_plan,
            # 平台序列号（real_seq＝服务器 msgSeq）：**段序的唯一依据**。平台共享 id 本身就是
            # 它，所以直接由 puid 派生（push / pull / 普通消息三条来路都带同一个值）；
            # 本地键（计划全文）没有平台号 → NULL。
            "platform_seq": (
                int(raw_platform_uid) if raw_platform_uid.isdigit() else None
            ),
            "platform_name": platform_name,
            "message_type": message_type,
            "session_id": session_id,
            "sender_id": sender_id,
            "sender_nickname": nickname,
            "platform_timestamp": send_ts if is_outbound else platform_ts,
            "content": content,
            "content_kind": str(params.get("ckind") or "[]"),
            "relation_data": _merge_link_ids(params),
            "created_at": params.get("now"),
        },
        "participation": {
            "platform_id": platform_id,
            "self_id": self_id,
            "conversation_id": str(params.get("cid") or ""),
            "umo": str(params.get("umo") or ""),
            "role": role or "user",
            "platform_message_id": params.get("mid"),
            "turn_id": params.get("turn_id"),
            "llm_status": str(params.get("lstatus") or ""),
            "send_status": str(params.get("send_status") or ""),
            "persona_id": params.get("per_id"),
            # 出站行的 user_id 是"被回复/目标用户"：主动消息没有同轮 user 行可 join，
            # 故按老语义落在参与行（入站行的对方就是 messages.sender_id，不重复存）。
            "target_id": user_id if role == "assistant" else None,
            "raw_timestamp": receipt_ts,
            "created_at": params.get("now"),
        },
    }


def _merge_link_ids(params: dict[str, Any]) -> Optional[str]:
    """把旧表的三列引用 id 并进 ``relation_data``（影子层不建这三列）。

    V1 的 ``query_history`` 会带回 ``at_id`` / ``reply_id`` / ``forward_id``；影子层不存
    独立列，所以写进 ``relation_data``，读侧再取回来——否则切读后回显段的 @ 目标与
    引用目标直接丢失（对拍时表现为一大片"假差异"）。
    只在有值时写；已有同名字段不覆盖（观察侧先写的那份更权威）。
    """
    raw = params.get("relation_data")
    extras = {}
    for key, param in (("at_id", "at_id"), ("reply_id", "reply_id"),
                       ("forward_id", "fwd_id")):
        value = str(params.get(param) or "")
        if value:
            extras[key] = value
    if not extras:
        return raw
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    # relation 编码约定必须有 "v": 1，否则读侧 parse_relation_data 直接判无效
    # （历史 370 条 reply_id 就是这么写进去却读不出来的）。
    data.setdefault("v", 1)
    for key, value in extras.items():
        data.setdefault(key, value)
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return raw


def _sanitize_relation_for_merge(raw: Optional[str]) -> Optional[str]:
    """写前剔除"没有信息"的键：``null`` 与空数组。

    ``_refresh_message`` 用 ``json_patch`` 合并两侧 relation_data，而 RFC 7396 的语义是
    "``null`` 删键、数组整体替换"。``relation_codec`` 恒定输出 ``"reply": null``，空
    ``mentions``/``media`` 也很常见——原样参与合并会把对侧已经写进来的 @ 目标与引用
    整段抹掉。这些空值本身不携带任何事实，提前剔除即可（有值时照常参与合并）。
    """
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    cleaned = {
        key: value
        for key, value in data.items()
        if value is not None and not (isinstance(value, list) and not value)
    }
    if cleaned == data:
        return raw
    try:
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return raw


async def mirror_record(session: AsyncSession, params: dict[str, Any]) -> bool:
    """镜像一条记录：消息主键（必要时新建）+ 意图/回显配对 + 参与行 upsert。

    返回 False 表示数据不足（缺 platform_id / session_id / sender 等键）。
    """
    normalized = normalize_record(params)
    if normalized is None:
        return False
    message = normalized["message"]
    participation = dict(normalized["participation"])
    is_plan = bool(message.get("is_plan"))
    # 轮次归属由**发送侧记账**给（push 到货时带上本轮 turn_id；pull 兜底也带）——
    # 不再有"正文 + 时间窗"的配对启发式。没有 turn_id 的平台行就是一条普通消息。
    claims_turn = bool(str(params.get("turn_id") or "")) and bool(
        params.get("claim_turn")
    )
    stale_owner = ""      # 认领被拒时记下真正的归属轮，参与行落库后改回来
    msg_uid = await upsert_message(session, message, participation)
    if not msg_uid:                    # 计划成员行：没有物理消息，后续链路全不执行
        return True
    if not is_plan and claims_turn:
        owner = await shadow_diag.link_owner_turn(
            session,
            umo=str(participation.get("umo") or ""),
            msg_uid=msg_uid,
        )
        if owner and owner != str(participation.get("turn_id") or ""):
            # 这条消息已经属于另一轮（push 先认领、pull 回看窗口后到）：先认领者赢，
            # 不抢归属；参与行 turn_id 等落库后改回去（见 stale_owner）。
            stale_owner = owner
    await upsert_participation(session, msg_uid, participation)
    if stale_owner:
        # 一条物理消息属于先认领的那一轮：参与行的 turn_id 刚被后一次认领覆写成后一轮，
        # 改回归属轮——读侧与 send_status/llm_status 回填都按 turn_id 找行，否则这一行
        # 会挂在错轮上，而链接表说的是另一轮（两套事实源互相矛盾）。
        await session.execute(
            text(
                "UPDATE chat_memory_message_bots SET turn_id = :owner "
                "WHERE msg_uid = :msg_uid AND umo = :umo AND turn_id = :mine"
            ),
            {
                "owner": stale_owner,
                "msg_uid": msg_uid,
                "umo": str(participation.get("umo") or ""),
                "mine": str(participation.get("turn_id") or ""),
            },
        )
    return True


async def lookup_sender_nickname(session: AsyncSession, message: dict[str, Any]) -> str:
    """查同平台会话下该发送者的最近非空昵称（用于补齐出站行：BOT 自己的名字）。

    出站行的昵称在旧表里存的是"被回复者昵称"，不能搬；但同一进程里其它实例观察
    该 BOT 发言时会存下它真实的昵称，落在同一张表的其它物理消息上，这里取回来。
    """
    result = await session.execute(
        text(
            "SELECT sender_nickname FROM chat_memory_messages "
            "WHERE platform_name = :platform_name AND message_type = :message_type "
            "AND session_id = :session_id AND sender_id = :sender_id "
            "AND sender_nickname <> '' "
            # 昵称就是号码（通知事件的适配器兜底）不能当名字抄下去，否则会一直传染
            "AND TRIM(sender_nickname) <> sender_id "
            "ORDER BY seq DESC LIMIT 1"
        ),
        message,
    )
    row = result.first()
    return str(row[0]) if row else ""


async def _participation_mid(
    session: AsyncSession, msg_uid: str, participation: Optional[dict[str, Any]]
) -> Optional[str]:
    """取该实例在该消息上的参与行的 ``platform_message_id``；无参与行返回 None。"""
    if not participation:
        return None
    result = await session.execute(
        text(
            "SELECT platform_message_id FROM chat_memory_message_bots "
            "WHERE msg_uid = :msg_uid AND platform_id = :platform_id "
            "AND self_id = :self_id AND conversation_id = :conversation_id"
        ),
        {"msg_uid": msg_uid, **participation},
    )
    row = result.first()
    if row is None:
        return None
    return str(row[0] or "")


async def _message_exists(session: AsyncSession, msg_uid: str) -> bool:
    result = await session.execute(
        text("SELECT 1 FROM chat_memory_messages WHERE msg_uid = :msg_uid"),
        {"msg_uid": msg_uid},
    )
    return result.first() is not None


async def _resolve_msg_uid(
    session: AsyncSession,
    message: dict[str, Any],
    participation: Optional[dict[str, Any]],
) -> str:
    """定出这条消息的主键：平台共享 id 优先，缺失时用内容指纹 + 出现序号。

    同秒同文的第 n 条用**出现序号**（``l:指纹:n``）而不是本实例的 message_id：
    各实例对同一批消息独立算出同一序号，仍然共享同一行；序号位已被别的实例占着
    （自己没有参与行）就并进去。序号超上限时退回 message_id 派生（宁可多一行不丢消息）。
    """
    platform_uid = str(message.get("platform_uid") or "")
    fingerprint_value = str(message["fingerprint"])
    platform_message_id = str((participation or {}).get("platform_message_id") or "")
    if platform_uid:
        candidate = platform_msg_uid(
            message["platform_name"],
            message["message_type"],
            message["session_id"],
            platform_uid,
        )
        if await _message_exists(session, candidate):
            return candidate
        # 平台 id 缺失的客户端可能已建过本地键的行：指纹命中且不冲突时**升级主键**，
        # 避免同环境混用不同客户端时产生重复行（参与行/工具行同步迁移）。
        local_uid = await _find_local_uid(
            session, fingerprint_value, participation, platform_message_id
        )
        if local_uid is not None and local_uid != candidate:
            await _promote_msg_uid(session, local_uid, candidate)
        return candidate
    for occurrence in range(_OCCURRENCE_LIMIT + 1):
        candidate = local_msg_uid(fingerprint_value, occurrence)
        if not await _message_exists(session, candidate):
            return candidate
        existing_mid = await _participation_mid(session, candidate, participation)
        if existing_mid is None:
            return candidate      # 别的实例建的同一出现序号 → 并进去
        if not platform_message_id or existing_mid == platform_message_id:
            return candidate      # 本实例已记过（幂等）或本实例没有平台 id 可用
    return local_msg_uid(f"{fingerprint_value}|{platform_message_id}", 0)


async def _find_local_uid(
    session: AsyncSession,
    fingerprint_value: str,
    participation: Optional[dict[str, Any]],
    platform_message_id: str,
) -> Optional[str]:
    """按指纹找本地键行（供主键升级用）；冲突（同实例不同 mid）时不升级。"""
    result = await session.execute(
        text(
            # 只找**本地键**行：平台键行是平台权威事实，不能被"提升"成另一个平台 uid
            # （否则同一秒同文的另一条消息会把已有平台键行改名，两条消息并成一条）。
            "SELECT msg_uid FROM chat_memory_messages WHERE fingerprint = :fingerprint "
            "AND msg_uid LIKE 'l:%' ORDER BY seq"
        ),
        {"fingerprint": fingerprint_value},
    )
    for row in result.fetchall():
        candidate = str(row[0])
        existing_mid = await _participation_mid(session, candidate, participation)
        if existing_mid is None:
            return candidate
        if platform_message_id and existing_mid == platform_message_id:
            return candidate
    return None


async def _promote_msg_uid(session: AsyncSession, old_uid: str, new_uid: str) -> None:
    """把本地键行升级为平台共享主键（参与行与工具行同步迁移）。"""
    for statement in (
        "UPDATE chat_memory_message_bots SET msg_uid = :new_uid WHERE msg_uid = :old_uid",
        "UPDATE chat_memory_tool_calls SET msg_uid = :new_uid WHERE msg_uid = :old_uid",
        "UPDATE chat_memory_messages SET msg_uid = :new_uid WHERE msg_uid = :old_uid",
    ):
        await session.execute(
            text(statement), {"new_uid": new_uid, "old_uid": old_uid}
        )


async def upsert_message(
    session: AsyncSession,
    message: dict[str, Any],
    participation: Optional[dict[str, Any]] = None,
) -> str:
    """写入或取回消息主键；只补空字段，绝不覆盖已有内容。

    计划全文（发送前抓到的正文）**不进 messages**：它没有平台事实，
    改写进轮次表的"计划"行（主键 umo+turn_id）并返回空 uid，由调用方短路。
    """
    if bool(message.get("is_plan")):
        await shadow_diag.note_reply_plan(
            session,
            participation=participation or {},
            content=str(message.get("content") or ""),
            content_kind=str(message.get("content_kind") or "[]"),
            llm_status=str((participation or {}).get("llm_status") or ""),
            send_status=str((participation or {}).get("send_status") or ""),
            persona_id=str((participation or {}).get("persona_id") or ""),
        )
        return ""
    nickname = str(message.get("sender_nickname") or "")
    if not nickname:
        message = {**message, "sender_nickname": await lookup_sender_nickname(session, message)}
    # 不再顺手回填"同会话同发送者的空昵称行"：用户明确要求昵称是写入当时的快照，
    # 旧数据一个字节都不改（HANDOFF / CHANGELOG 的决策）。空昵称由读侧按
    # `<cm_nickname>` 缺失兜底成「未知成员」，不靠改历史行修补。
    msg_uid = await _resolve_msg_uid(session, message, participation)
    if await _message_exists(session, msg_uid):
        await _refresh_message(session, msg_uid, message)
    else:
        await _insert_message(session, msg_uid, message)
    return msg_uid


async def _refresh_message(
    session: AsyncSession, msg_uid: str, message: dict[str, Any]
) -> None:
    """补空：正文/昵称/content_kind/relation_data/平台号只在原来为空时填。

    @ 目标不单独存列：``relation_data.mentions`` 已完整覆盖（实测 619/619 行都有）。
    ``platform_seq`` 也要补：一行可能先以本地键写入（当时没有平台号）、后来被平台键行
    升级/合并——段序靠它，缺了就只能靠时间兜底。
    """
    await session.execute(
        text(
            "UPDATE chat_memory_messages SET "
            "content = CASE WHEN content = '' THEN :content ELSE content END, "
            "sender_nickname = CASE WHEN sender_nickname = '' THEN :sender_nickname "
            "ELSE sender_nickname END, "
            "content_kind = CASE WHEN content_kind IN ('', '[]') THEN :content_kind "
            "ELSE content_kind END, "
            # relation_data 要**合并**而不是"先写的赢"：观察侧先写的是 mentions，
            # 回显/拉取后来带来 at_id/reply_id/forward_id，COALESCE 会把后者整份丢掉。
            # json_patch 是 RFC 7396 语义（有则覆盖、无则保留），但 null 会**删键**、
            # 数组会**整体替换**——所以 :relation_data 先剔掉 null 与空数组
            # （见 _sanitize_relation_for_merge），否则对侧已写的 @/引用会被抹掉。
            # 任一侧不是合法 JSON 就保持原值。
            "relation_data = CASE "
            "WHEN :relation_data IS NULL OR :relation_data = '' THEN relation_data "
            "WHEN relation_data IS NULL OR relation_data = '' THEN :relation_data "
            "WHEN json_valid(relation_data) AND json_valid(:relation_data) "
            "THEN json_patch(relation_data, :relation_data) "
            "ELSE relation_data END, "
            "platform_seq = COALESCE(platform_seq, :platform_seq) "
            "WHERE msg_uid = :msg_uid"
        ),
        {
            **message,
            "msg_uid": msg_uid,
            "relation_data": _sanitize_relation_for_merge(
                message.get("relation_data")
            ),
        },
    )


async def _insert_message(
    session: AsyncSession, msg_uid: str, message: dict[str, Any]
) -> None:
    """插入物理消息。

    ``seq`` 是**会话内**递增序号（同一物理消息只有一行 → 各实例看到同一个 seq），
    既用于稳定排序/游标，也是"人类可读的公共键"。序号由 ``chat_memory_session_seq``
    计数器分配（``SEQ_ALLOCATE_SQL``，只增不减）：会话被删空后不会回到 1，外部读者的
    游标永远向前；并发重复观察只留下空洞，不会撞键。
    """
    # 已经见过这条（三个实例看同一条消息）：直接返回，不浪费一个序号。
    exists = await session.execute(
        text("SELECT 1 FROM chat_memory_messages WHERE msg_uid = :msg_uid"),
        {"msg_uid": msg_uid},
    )
    if exists.first() is not None:
        return
    # 序号来自**只增计数器**：会话被删空后不会回到 1，外部读者的游标永远向前。
    # 极端并发下两个实例可能各领一个号，只有一个插得进去——留空洞，不影响游标语义。
    allocated = await session.execute(text(SEQ_ALLOCATE_SQL), message)
    seq = int(allocated.scalar() or 0)
    await session.execute(
        text(INSERT_MESSAGE_SQL), {**message, "msg_uid": msg_uid, "seq": seq}
    )


async def upsert_participation(
    session: AsyncSession, msg_uid: str, participation: dict[str, Any]
) -> None:
    """写入参与行；重复观察只补空字段，``role`` 单向升级为 assistant（发送方视角优先）。"""
    await session.execute(
        text(
            "INSERT INTO chat_memory_message_bots "
            "(msg_uid, platform_id, self_id, conversation_id, umo, role, "
            "platform_message_id, turn_id, "
            "llm_status, send_status, persona_id, target_id, raw_timestamp, created_at) "
            "VALUES (:msg_uid, :platform_id, :self_id, :conversation_id, :umo, :role, "
            ":platform_message_id, :turn_id, "
            ":llm_status, :send_status, :persona_id, :target_id, :raw_timestamp, :created_at) "
            "ON CONFLICT (msg_uid, platform_id, self_id, conversation_id) DO UPDATE SET "
            "role = CASE WHEN excluded.role = 'assistant' THEN 'assistant' "
            "ELSE chat_memory_message_bots.role END, "
            "platform_message_id = COALESCE(NULLIF(excluded.platform_message_id, ''), "
            "chat_memory_message_bots.platform_message_id), "
            "turn_id = COALESCE(NULLIF(excluded.turn_id, ''), "
            "chat_memory_message_bots.turn_id), "
            "llm_status = CASE WHEN excluded.llm_status <> '' THEN excluded.llm_status "
            "ELSE chat_memory_message_bots.llm_status END, "
            "send_status = CASE WHEN excluded.send_status <> '' THEN excluded.send_status "
            "ELSE chat_memory_message_bots.send_status END, "
            "persona_id = COALESCE(NULLIF(excluded.persona_id, ''), "
            "chat_memory_message_bots.persona_id), "
            "target_id = COALESCE(NULLIF(excluded.target_id, ''), "
            "chat_memory_message_bots.target_id)"
        ),
        {"msg_uid": msg_uid, **participation},
    )


async def mirror_llm_status(
    session: AsyncSession, umo: str, conversation_id: str, turn_id: str, status: str
) -> int:
    """镜像 user 侧 llm_status 更新（每实例状态，落在参与行上）。

    返回命中行数；0 说明该轮没有（或已删除）user 行可落状态。
    """
    result = await session.execute(
        text(
            "UPDATE chat_memory_message_bots SET llm_status = :status "
            "WHERE umo = :umo AND conversation_id = :cid AND turn_id = :turn_id "
            "AND role = 'user' AND EXISTS (SELECT 1 FROM chat_memory_messages m "
            "WHERE m.msg_uid = chat_memory_message_bots.msg_uid "
            "AND m.deleted_at IS NULL)"
        ),
        {"status": status, "umo": umo, "cid": conversation_id, "turn_id": turn_id},
    )
    return int(result.rowcount or 0)


async def mirror_send_status(
    session: AsyncSession, umo: str, conversation_id: str, turn_id: str, status: str
) -> int:
    """镜像 assistant 侧 send_status 更新（返回命中行数）。"""
    result = await session.execute(
        text(
            "UPDATE chat_memory_message_bots SET send_status = :status "
            "WHERE umo = :umo AND conversation_id = :cid AND turn_id = :turn_id "
            "AND role = 'assistant' AND EXISTS (SELECT 1 FROM chat_memory_messages m "
            "WHERE m.msg_uid = chat_memory_message_bots.msg_uid "
            "AND m.deleted_at IS NULL)"
        ),
        {"status": status, "umo": umo, "cid": conversation_id, "turn_id": turn_id},
    )
    return int(result.rowcount or 0)


async def mirror_recall(
    session: AsyncSession, umo: str, platform_message_id: str, now: Any
) -> int:
    """镜像撤回：由该实例的平台 message_id 定位物理消息并标记 recalled_at。

    返回命中的行数（0 = 目标还没落库）；补标路径靠它判断"这次到底标上了没有"。

    实现特意分成"先按 umo+mid 查主键、再按主键 UPDATE"两步：写成
    ``UPDATE … WHERE msg_uid IN (SELECT …)`` 时优化器会**全表扫 messages**
    （实测单条 85ms，线上 415 个撤回事件累计 4 分钟，把插件加载卡死）；
    拆开之后每次只扫该 umo 的参与行 + 一次主键更新（亚毫秒）。
    """
    uids = (
        await session.execute(
            text(
                "SELECT msg_uid FROM chat_memory_message_bots "
                "WHERE umo = :umo AND platform_message_id = :mid"
            ),
            {"umo": umo, "mid": platform_message_id},
        )
    ).fetchall()
    affected = 0
    for (msg_uid,) in uids:
        if not msg_uid:
            continue
        result = await session.execute(
            text(
                "UPDATE chat_memory_messages SET recalled_at = :now "
                "WHERE msg_uid = :uid AND recalled_at IS NULL"
            ),
            {"now": now, "uid": msg_uid},
        )
        affected += int(result.rowcount or 0)
    return affected


async def backfill_turn_persona(session: AsyncSession) -> int:
    """把"同一轮里别的行有 persona、这一行空着"的行补上（返回补齐的行数）。

    回显事件（``message_sent``）里没有 persona，只能靠发送侧记账带；历史空缺按
    ``(turn_id, umo, conversation_id)`` 就地回填——同一轮的计划行一定有 persona，
    不按 persona 过滤的读路径看不出差别，过滤时会整段消失（线上 111 行 / 47 轮）。
    加载时与每日清理各跑一次。
    """
    filled = 0
    for table in ("chat_memory_message_bots", "chat_memory_reply_messages"):
        result = await session.execute(
            text(
                f"UPDATE {table} SET persona_id = ("
                f"  SELECT src.persona_id FROM {table} AS src "
                f"  WHERE src.turn_id = {table}.turn_id AND src.umo = {table}.umo "
                f"    AND src.conversation_id = {table}.conversation_id "
                f"    AND src.persona_id <> '' LIMIT 1) "
                f"WHERE COALESCE(persona_id, '') = '' AND COALESCE(turn_id, '') <> '' "
                f"  AND EXISTS (SELECT 1 FROM {table} AS s2 "
                f"    WHERE s2.turn_id = {table}.turn_id AND s2.umo = {table}.umo "
                f"      AND s2.conversation_id = {table}.conversation_id "
                f"      AND s2.persona_id <> '')"
            )
        )
        filled += int(result.rowcount or 0)
    # 参与行没有同表兄弟可借（计划成员只活在链接表里）：从本轮的**计划成员**补，
    # 否则回显行按 persona 过滤时整段消失——而计划成员一定带 persona。
    result = await session.execute(
        text(
            "UPDATE chat_memory_message_bots SET persona_id = ("
            "  SELECT src.persona_id FROM chat_memory_reply_messages AS src "
            "  WHERE src.turn_id = chat_memory_message_bots.turn_id "
            "    AND src.umo = chat_memory_message_bots.umo "
            "    AND src.conversation_id = chat_memory_message_bots.conversation_id "
            "    AND src.persona_id <> '' LIMIT 1) "
            "WHERE COALESCE(persona_id, '') = '' AND COALESCE(turn_id, '') <> '' "
            "  AND EXISTS (SELECT 1 FROM chat_memory_reply_messages AS s2 "
            "    WHERE s2.turn_id = chat_memory_message_bots.turn_id "
            "      AND s2.umo = chat_memory_message_bots.umo "
            "      AND s2.conversation_id = chat_memory_message_bots.conversation_id "
            "      AND s2.persona_id <> '')"
        )
    )
    filled += int(result.rowcount or 0)
    return filled


# 切读门槛：双写必须跑满这么多天，UI / 上下文 / 公开 API 才一起读合并存储。
# 放在这里是因为三个读侧（main 的上下文、ui.repository 的查询台）都要看同一个数。
SHADOW_READ_MIN_DAYS = 7




async def mirror_tool_call(
    session: AsyncSession,
    *,
    umo: str,
    conversation_id: str,
    turn_id: str,
    call_index: int,
    tool_name: str,
    tool_args: str,
    tool_result: str,
    now: Any,
) -> None:
    """镜像工具调用；``msg_uid`` 尽力锚到该轮参与行（工具循环早于最终回复，常锚到 user 行）。"""
    result = await session.execute(
        text(
            "SELECT msg_uid FROM chat_memory_message_bots "
            "WHERE umo = :umo AND conversation_id = :cid AND turn_id = :turn_id "
            "ORDER BY (role = 'assistant') DESC, id DESC LIMIT 1"
        ),
        {"umo": umo, "cid": conversation_id, "turn_id": turn_id},
    )
    row = result.first()
    await session.execute(
        text(
            "INSERT OR IGNORE INTO chat_memory_tool_calls "
            "(msg_uid, umo, conversation_id, turn_id, call_index, tool_name, "
            "tool_args, tool_result, created_at) "
            "VALUES (:msg_uid, :umo, :cid, :turn_id, :call_index, :tool_name, "
            ":tool_args, :tool_result, :now)"
        ),
        {
            "msg_uid": str(row[0]) if row else None,
            "umo": umo,
            "cid": conversation_id,
            "turn_id": turn_id,
            "call_index": int(call_index),
            "tool_name": tool_name,
            "tool_args": tool_args or "",
            "tool_result": tool_result or "",
            "now": now,
        },
    )


async def mirror_delete_conversation(
    session: AsyncSession, umo: str, conversation_id: str
) -> None:
    """镜像 /reset：删该实例该会话的参与行与归属行，物理消息仅在无参与时回收。

    归属行（``chat_memory_reply_messages``）必须跟着删：计划成员行没有物理消息可回收，
    留着会让被 reset 掉的那一轮在切读后**重新出现**（读侧拿计划成员兜底）。
    """
    result = await session.execute(
        text(
            "SELECT DISTINCT msg_uid FROM chat_memory_message_bots "
            "WHERE umo = :umo AND conversation_id = :cid"
        ),
        {"umo": umo, "cid": conversation_id},
    )
    msg_uids = [str(row[0]) for row in result.fetchall()]
    await session.execute(
        text(
            "DELETE FROM chat_memory_message_bots "
            "WHERE umo = :umo AND conversation_id = :cid"
        ),
        {"umo": umo, "cid": conversation_id},
    )
    await session.execute(
        text(
            "DELETE FROM chat_memory_reply_messages "
            "WHERE umo = :umo AND conversation_id = :cid"
        ),
        {"umo": umo, "cid": conversation_id},
    )
    await session.execute(
        text(
            "DELETE FROM chat_memory_tool_calls "
            "WHERE umo = :umo AND conversation_id = :cid"
        ),
        {"umo": umo, "cid": conversation_id},
    )
    await _drop_orphan_messages(session, msg_uids)


async def mirror_delete_before(session: AsyncSession, before: Any) -> None:
    """镜像保留期清理（auto_cleanup_days）：按参与行 created_at 删，再回收孤儿消息。

    计划成员行同样按 ``created_at`` 清掉——它们没有物理消息，孤儿回收覆盖不到。
    """
    result = await session.execute(
        text(
            "SELECT DISTINCT msg_uid FROM chat_memory_message_bots "
            "WHERE created_at < :before"
        ),
        {"before": before},
    )
    msg_uids = [str(row[0]) for row in result.fetchall()]
    await session.execute(
        text("DELETE FROM chat_memory_message_bots WHERE created_at < :before"),
        {"before": before},
    )
    await session.execute(
        text(
            "DELETE FROM chat_memory_reply_messages "
            "WHERE created_at < :before"
        ),
        {"before": before},
    )
    await session.execute(
        text("DELETE FROM chat_memory_tool_calls WHERE created_at < :before"),
        {"before": before},
    )
    await _drop_orphan_messages(session, msg_uids)


async def _drop_orphan_messages(session: AsyncSession, msg_uids: list[str]) -> int:
    """回收不再被任何参与行引用的物理消息；分批避开 SQLite 绑定参数上限。"""
    removed = 0
    for start in range(0, len(msg_uids), _SQL_PARAM_CHUNK):
        chunk = msg_uids[start : start + _SQL_PARAM_CHUNK]
        if not chunk:
            continue
        placeholders = ", ".join(f":uid{i}" for i in range(len(chunk)))
        params = {f"uid{i}": value for i, value in enumerate(chunk)}
        result = await session.execute(
            text(
                "DELETE FROM chat_memory_messages WHERE msg_uid IN (" + placeholders + ") "
                "AND NOT EXISTS (SELECT 1 FROM chat_memory_message_bots "
                "WHERE msg_uid = chat_memory_messages.msg_uid)"
            ),
            params,
        )
        removed += int(result.rowcount or 0)
        # 结构线 5 起不再有"链接行"：哪条消息属于哪一轮存在 assistant 参与行的
        # turn_id 上（删除参与行即等于解除归属），计划正文在 reply_messages 里
        # 由上面的按时间清理处理。这里只回收**本次参与行被删过**的物理消息；
        # 更早批次遗留的孤儿（参与行早已不在）不在这份 uid 列表里，走每日清理时
        # 由 `_drop_orphan_messages` 之外的扫描兜底（见 `mirror_delete_before` 调用方）。
    return removed


async def purge_orphan_rows(session: AsyncSession) -> tuple[int, int]:
    """回收跨表的孤儿行，返回 ``(删掉的物理消息数, 删掉的参与行数)``。

    两处不变量这里都不能靠 FK（本层没有外键）：
    - ``chat_memory_messages`` 没有任何参与行 → 没人引用它（旧版本按"本批参与行被删"
      才回收，更早批次遗留的孤儿永远留着）；
    - ``chat_memory_message_bots`` 指向的物理消息已不在 → 读侧 join 不到，只是占索引。

    由插件加载时与每日清理各跑一次；返回 0 也不打日志（正常空转）。
    """
    removed_messages = await session.execute(
        text(
            "DELETE FROM chat_memory_messages WHERE NOT EXISTS ("
            "  SELECT 1 FROM chat_memory_message_bots b "
            "   WHERE b.msg_uid = chat_memory_messages.msg_uid)"
        )
    )
    removed_parts = await session.execute(
        text(
            "DELETE FROM chat_memory_message_bots WHERE NOT EXISTS ("
            "  SELECT 1 FROM chat_memory_messages m "
            "   WHERE m.msg_uid = chat_memory_message_bots.msg_uid)"
        )
    )
    return int(removed_messages.rowcount or 0), int(removed_parts.rowcount or 0)


def log_shadow_failure(failures: int, exc: BaseException) -> None:
    """影子写失败只记异常类型（异常串可能带 SQL 参数＝聊天正文）；前 3 次 WARNING。"""
    detail = f"[ChatMemory] 合并存储影子写入失败（累计 {failures} 次）: {type(exc).__name__}"
    if failures <= 3:
        logger.warning(detail)
    else:
        logger.debug(detail)


__all__ = [
    "SELECT_GROUP_NAMES_SQL",
    "UPSERT_GROUP_NAME_SQL",
    "SHADOW_MIGRATIONS",
    "SHADOW_SCHEMA_VERSION",
    "SEQ_ALLOCATE_SQL",
    "INSERT_MESSAGE_SQL",
    "shadow_diag",
    "shadow_migration_statements",
    "shadow_schema_statements",
    "drop_shadow_statements",
    "split_umo",
    "fingerprint",
    "platform_msg_uid",
    "local_msg_uid",
    "normalize_record",
    "mirror_record",
    "upsert_message",
    "backfill_turn_persona",
    "upsert_participation",
    "mirror_llm_status",
    "mirror_send_status",
    "mirror_recall",
    "mirror_tool_call",
    "mirror_delete_conversation",
    "mirror_delete_before",
    "log_shadow_failure",
]
