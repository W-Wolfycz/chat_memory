#!/usr/bin/env python3
"""修复 chat_memory.db 中 created_at 的时区偏移。

背景：v2.3.3 之前 insert 用 datetime.now()（系统本地时区 UTC+8 naive），
v2.3.3 起才改用 datetime.now(timezone.utc)。历史数据大部分是 UTC+8 naive。

此脚本用 raw_timestamp（Unix 秒，真正的 UTC）重算 created_at，校准为 UTC naive。
所有有 raw_timestamp 的行都会被修正为正确值（无论原来偏 0 还是 +8）。

幂等：raw_timestamp 不变，多次运行结果一致。
安全：先备份，事务内执行，失败自动回滚。

用法：
    python fix_created_at_utc.py              # 自动探测 db 路径，交互确认
    python fix_created_at_utc.py <db_path>    # 指定路径
    python fix_created_at_utc.py --yes        # 跳过确认
"""
import sqlite3
import sys
import shutil
from datetime import datetime
from pathlib import Path

DEFAULT_PATHS = [
    Path.home() / ".astrbot/data/plugin_data/chat_memory/chat_memory.db",
    Path("/mnt/c/Users/Administrator/.astrbot/data/plugin_data/chat_memory/chat_memory.db"),
]


def find_db(custom=None):
    if custom:
        p = Path(custom)
        if p.exists():
            return p.resolve()
        die(f"指定路径不存在: {custom}")
    for p in DEFAULT_PATHS:
        if p.exists() and p.stat().st_size > 0:
            return p.resolve()
    die(
        "未自动找到 chat_memory.db，请指定路径：\n"
        "  python fix_created_at_utc.py <db_path>"
    )


def show_stats(conn, label):
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM chat_memory_records").fetchone()[0]
    if total == 0:
        print(f"\n[{label}] 表为空，无需迁移")
        return False
    has_raw = c.execute(
        "SELECT COUNT(*) FROM chat_memory_records WHERE raw_timestamp IS NOT NULL"
    ).fetchone()[0]
    no_raw = total - has_raw
    print(f"\n{'='*55}")
    print(f"  {label}")
    print(f"{'='*55}")
    print(f"  总行数:           {total}")
    print(f"  有 raw_timestamp: {has_raw} ({has_raw*100//total}%)")
    if no_raw:
        print(f"  无 raw_timestamp: {no_raw}（将跳过，保持原样）")
    print(f"\n  created_at 与 raw_timestamp(UTC) 的偏差分布:")
    for row in c.execute(
        "SELECT CAST((julianday(created_at) - julianday("
        "datetime(raw_timestamp, 'unixepoch'))) * 24 AS INT) as hr, COUNT(*) "
        "FROM chat_memory_records WHERE raw_timestamp IS NOT NULL "
        "GROUP BY hr ORDER BY hr"
    ):
        tag = "正确 UTC" if row[0] == 0 else f"偏 {row[0]:+d} 小时"
        mark = "  " if row[0] == 0 else "!!"
        print(f"    {mark} {tag}: {row[1]} 条")
    r = c.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM chat_memory_records"
    ).fetchone()
    print(f"\n  created_at 范围: {r[0]} ~ {r[1]}")
    return has_raw > 0


def backup_db(db_path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db_path.with_name(f"{db_path.name}.bak_{ts}")
    # 用 SQLite backup API 做一致性备份（包含 WAL 未 checkpoint 的数据）
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(bak))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"\n备份完成: {bak}")
    print(f"  (如需回滚：把这个文件名改回 {db_path.name} 覆盖即可)")
    return bak


def migrate(conn):
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat_memory_records "
        "SET created_at = datetime(raw_timestamp, 'unixepoch') "
        "WHERE raw_timestamp IS NOT NULL"
    )
    affected = cur.rowcount
    conn.commit()
    return affected


def die(msg, code=1):
    print(f"\n错误: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    args = [a for a in sys.argv[1:] if a != "--yes"]
    skip_confirm = "--yes" in sys.argv
    db_path = find_db(args[0] if args else None)
    print(f"数据库: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        if not show_stats(conn, "迁移前"):
            print("\n无需迁移，退出。")
            return

        if not skip_confirm:
            print("\n" + "-" * 55)
            print("即将用 raw_timestamp 重算 created_at（UTC naive）。")
            print("会先备份 db。此操作不可逆（但有备份可回滚）。")
            ans = input("\n确认执行？ [y/N] ").strip().lower()
            if ans != "y":
                print("已取消。")
                return

        print("\n正在备份...")
        backup_db(db_path)

        print("\n正在迁移...")
        affected = migrate(conn)
        print(f"  更新了 {affected} 行")

        show_stats(conn, "迁移后")
        print("\n迁移完成。偏差应全为 0。")
        print("如果 CM 正在运行，建议重启让它重新打开 db 连接。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
