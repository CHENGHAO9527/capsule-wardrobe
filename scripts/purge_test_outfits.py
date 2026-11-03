# -*- coding: utf-8 -*-
"""清掉「测试顶高 id」留下的那批搭配记录。

    python scripts/purge_test_outfits.py            # 只报告（默认）
    python scripts/purge_test_outfits.py --apply    # 真的删

背景：`apitest.py` 早先显式插过 `outfits.id=900777`，把 SQLite 的
`sqlite_sequence` 永久顶高 —— 之后**真实保存的搭配**都拿到 900778+ 这种假 id。
（`_clamp_outfit_seq()` 已经堵住了源头，但库里还留着历史遗留。）

只删**确实没有价值**的那些，三条同时成立才动：
  · id >= --min-id（默认 900000，只有测试污染过这一段）
  · `is_worn = 0`（没被穿过）
  · `feedback` 表里没有指向它的记录（没有被接受/跳过/拒绝过）
有任何一条不成立就留着 —— 那是用户真实的反馈历史。

删完把 `sqlite_sequence` 夹回 `MAX(id)`，让以后的新记录拿到正常 id。
动数据前先备份，`--apply` 才真的删。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

L: list[str] = []


def say(s: str = "") -> None:
    print(s)
    L.append(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删（默认只报告）")
    ap.add_argument("--min-id", type=int, default=900000)
    ap.add_argument("--out", help="把报告也写进这个文件（UTF-8，绕开 PowerShell 的乱码）")
    args = ap.parse_args()

    conn = db.get_conn()
    try:
        seq_before = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'outfits'").fetchone()
        seq_before = seq_before[0] if seq_before else None
        mx = conn.execute("SELECT COALESCE(MAX(id), 0) FROM outfits").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM outfits").fetchone()[0]

        rows = conn.execute(
            "SELECT o.id, o.date, o.occasion, o.source, o.is_worn, o.created_at, "
            "  (SELECT COUNT(*) FROM feedback f WHERE f.outfit_id = o.id) AS n_fb, "
            "  (SELECT COUNT(*) FROM outfit_items oi WHERE oi.outfit_id = o.id) AS n_items "
            "FROM outfits o WHERE o.id >= ? ORDER BY o.id", (args.min_id,)).fetchall()

        say(f"outfits 共 {total} 行；id >= {args.min_id} 的有 {len(rows)} 行")
        say(f"sqlite_sequence.outfits = {seq_before}，当前 MAX(id) = {mx}")
        say()

        victims, keep = [], []
        for r in rows:
            why = []
            if r["is_worn"]:
                why.append("穿过")
            if r["n_fb"]:
                why.append(f"有 {r['n_fb']} 条反馈")
            (keep if why else victims).append((r, why))

        if victims:
            say(f"会删这 {len(victims)} 条（没穿过、没有任何反馈）：")
            for r, _ in victims:
                say(f"  id={r['id']:<8} {r['date'] or '—':<12} "
                    f"source={r['source'] or '—':<6} 单品 {r['n_items']} 件 "
                    f"{r['created_at']}")
        if keep:
            say()
            say(f"会留着这 {len(keep)} 条（有使用痕迹）：")
            for r, why in keep:
                say(f"  id={r['id']:<8} {r['date'] or '—':<12} —— {'、'.join(why)}")

        if not victims:
            say("\n没有可删的。")
            return 0

        if not args.apply:
            say("\n（干跑。确认后加 --apply 真删。）")
            return 0

        # ── 备份 ──
        src = Path(db.DB_PATH) if hasattr(db, "DB_PATH") else ROOT / "data" / "closet.db"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = src.with_name(f"{src.name}.bak-{stamp}")
        shutil.copy2(src, bak)
        say(f"\n已备份 → {bak.name}")

        ids = [r["id"] for r, _ in victims]
        q = ",".join("?" * len(ids))
        conn.execute("PRAGMA foreign_keys = ON")
        n_oi = conn.execute(
            f"SELECT COUNT(*) FROM outfit_items WHERE outfit_id IN ({q})", ids).fetchone()[0]
        conn.execute(f"DELETE FROM outfits WHERE id IN ({q})", ids)
        conn.commit()

        # 夹回正常区间，让以后的新记录不再拿 900xxx
        mx2 = conn.execute("SELECT COALESCE(MAX(id), 0) FROM outfits").fetchone()[0]
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'outfits'", (mx2,))
        conn.commit()

        left = conn.execute("SELECT COUNT(*) FROM outfits").fetchone()[0]
        seq_after = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='outfits'").fetchone()
        orphan = conn.execute(
            "SELECT COUNT(*) FROM outfit_items WHERE outfit_id NOT IN "
            "(SELECT id FROM outfits)").fetchone()[0]
        say(f"删掉 {len(ids)} 条搭配（连带 {n_oi} 行 outfit_items）")
        say(f"outfits 现在 {left} 行；sqlite_sequence = "
            f"{seq_after[0] if seq_after else None}（MAX(id)={mx2}）")
        say(f"孤儿 outfit_items：{orphan} 行（应为 0）")
        return 0
    finally:
        conn.close()
        if args.out:
            (ROOT / args.out).write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
