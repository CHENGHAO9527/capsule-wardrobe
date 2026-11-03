# -*- coding: utf-8 -*-
"""把 seed.py 写进去的种子数据从库里清掉，只留你自己录的衣服。

为什么要单独写个脚本而不是直接 DELETE：
  1. **靠完整字段签名匹配，不靠名字。** 只比名字的话，你自己加了一件同名的
     「白衬衫」就会被一起删掉。签名是 (名称+槽位+品类+色值+正式度+温度区间+厚度+购入价)，
     八个字段全中才算种子数据。
  2. **先备份数据库**再动手。
  3. 默认**干跑**，`--apply` 才真删。

    python scripts/drop_seed_items.py            # 干跑，只报告
    python scripts/drop_seed_items.py --apply    # 备份 + 真删
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
import seed as seedmod  # noqa: E402


def signature(row) -> tuple:
    return (row["name"], row["slot"], row["category"], row["color_hex"],
            row["formality"], row["temp_min"], row["temp_max"], row["thickness"],
            row["purchase_price"])


SEED_SIGS = set()
for r in seedmod.SEED:
    d = dict(zip(seedmod.FIELDS, r))
    SEED_SIGS.add((d["name"], d["slot"], d["category"], d["color_hex"],
                   d["formality"], d["temp_min"], d["temp_max"],
                   d["thickness"], d["purchase_price"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（先自动备份）")
    args = ap.parse_args()

    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    seed_rows = [r for r in rows if signature(r) in SEED_SIGS]
    user_rows = [r for r in rows if signature(r) not in SEED_SIGS]

    print(f"items 共 {len(rows)} 件 → 种子 {len(seed_rows)} 件，你自己的 {len(user_rows)} 件")
    print()

    if not seed_rows:
        print("没有识别出种子数据，没什么可删的。")
        conn.close()
        return 0

    print("将删除（种子数据）：")
    for r in seed_rows:
        print(f"  id={r['id']:<4} {r['name']:<16} {r['slot']}/{r['category']}")

    print()
    print(f"将保留（你自己录的 {len(user_rows)} 件）：")
    for r in user_rows:
        print(f"  id={r['id']:<4} {r['name']:<22} {r['slot']}/{r['category']:<6}"
              f" {r['color_main'] or '-':<4} 图={'有' if r['img_display'] else '无'}")

    ids = [r["id"] for r in seed_rows]
    ph = ",".join("?" * len(ids))
    counts = {}
    for table, col in (("outfit_items", "item_id"), ("wear_log", "item_id"),
                       ("w_item", "item_id"), ("prebuy_evals", "candidate_id")):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE {col} IN ({ph})", ids).fetchone()["c"]
    n_outfits = conn.execute("SELECT COUNT(*) c FROM outfits").fetchone()["c"]
    n_feedback = conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]

    print()
    print("关联数据：")
    for k, v in counts.items():
        print(f"  {k:<14} {v} 行（随单品一起删）")
    print(f"  outfits        {n_outfits} 行 —— **全部删除**")
    print(f"  feedback       {n_feedback} 行 —— **全部删除**")
    print()
    print("  为什么 outfits 全删：现有的搭配都是「26 件种子 + 你的衣服」混着算出来的，")
    print("  把种子件抽掉只会留下一堆残缺记录，没有保留价值。删掉之后「今日」会重新生成。")
    print("  权重表 w_item / w_pair 里属于你那些衣服的记录会保留，属于种子的随单品删除。")

    if not args.apply:
        print()
        print("这是干跑。确认无误后加 --apply。")
        conn.close()
        return 0

    # ── 先备份 ──
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.DB_PATH.with_name(f"closet.db.bak-{stamp}")
    shutil.copy2(db.DB_PATH, backup)
    print()
    print(f"已备份数据库 → {backup.name}")
    if not backup.exists():
        print("备份失败，中止。")
        conn.close()
        return 1

    try:
        conn.execute("DELETE FROM outfit_items")
        conn.execute("DELETE FROM feedback")
        conn.execute("DELETE FROM outfits")
        conn.execute(f"DELETE FROM items WHERE id IN ({ph})", ids)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        print(f"删除失败，已回滚：{exc}")
        print(f"备份还在：{backup}")
        return 1

    left = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    print(f"删除完成。items 剩 {left} 件。")
    print()
    print("权重表：")
    for t in ("w_item", "w_pair"):
        print(f"  {t}: {conn.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c']} 行")
    conn.close()
    print()
    print("刷新 http://127.0.0.1:8077/ 就是纯你自己的衣橱了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
