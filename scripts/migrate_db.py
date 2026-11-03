# -*- coding: utf-8 -*-
"""对数据库跑一次 schema 迁移 / 自愈，不需要启动服务。

服务启动时 `init_db()` 会自动做这件事；这个脚本只是为了**不重启服务**也能立刻修库。
做的事和启动时完全一样：
  · 补缺失的列（db._MIGRATIONS）
  · 清掉"父行已不存在"的孤儿记录（purge_orphans）
  · 把 outfit_items.item_id 补成 ON DELETE CASCADE
动数据之前会自动备份到 data/closet.db.bak-YYYYmmdd-HHMMSS。

    python scripts/migrate_db.py            # 直接跑（会自动备份）
    python scripts/migrate_db.py --dry-run  # 只看会做什么，不改
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改库")
    ap.add_argument("--db", default=str(db.DB_PATH))
    args = ap.parse_args()

    if Path(args.db) != db.DB_PATH:
        db.DB_PATH = Path(args.db)

    if not db.DB_PATH.exists():
        print(f"找不到数据库 {db.DB_PATH}")
        return 1

    print(f"数据库：{db.DB_PATH}")

    if args.dry_run:
        conn = db.get_conn()
        try:
            n_orphan = db._count_orphans(conn)
            need_fk = db._fk_needs_cascade(conn, "outfit_items", "items")
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            print(f"  孤儿记录            {n_orphan} 行")
            print(f"  outfit_items 外键   {'需要补 ON DELETE CASCADE' if need_fk else '正常'}")
            print(f"  foreign_key_check   {len(bad)} 条违规")
            print("  （--dry-run，什么都没改）")
        finally:
            conn.close()
        return 0

    conn = db.get_conn()
    try:
        n_before = db._count_orphans(conn)
        # 走的就是 init_db 里那条路径
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
        db._migrate(conn)
        conn.commit()
        n_after = db._count_orphans(conn)
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  孤儿记录 {n_before} → {n_after} 行")
        print(f"  foreign_key_check {len(bad)} 条违规")
        print(f"  outfit_items 外键 "
              f"{'仍需修（没成功）' if db._fk_needs_cascade(conn, 'outfit_items', 'items') else '已是 CASCADE'}")
        print("  完成。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
