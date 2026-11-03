# -*- coding: utf-8 -*-
"""清理无引用的图片文件。

删除单品时不会连带删掉 data/images 里的三份图，用久了目录会堆积孤儿文件。

**默认只报告，不动文件。** 要真删加 --apply。

    python scripts/clean_orphans.py
    python scripts/clean_orphans.py --apply
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
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只报告）")
    ap.add_argument("--db", default=str(ROOT / "data" / "closet.db"))
    args = ap.parse_args()

    if Path(args.db) != db.DB_PATH:
        db.DB_PATH = Path(args.db)

    img_dir = ROOT / "data" / "images"
    if not img_dir.exists():
        print("没有 data/images 目录")
        return 0

    conn = db.get_conn()   # 不用裸 sqlite3.connect：它默认 foreign_keys=OFF，
                           # 哪天这里加个写操作就会静默留下孤儿（apitest.py 栽过）
    rows = conn.execute(
        "SELECT id, name, img_display FROM items "
        "WHERE img_display IS NOT NULL AND img_display <> ''"
    ).fetchall()
    conn.close()

    referenced = {Path(r[2]).name.split("_")[0] for r in rows}
    uids = {r[2].split("/")[-1].split("_")[0] for r in rows}

    files = sorted(p for p in img_dir.iterdir() if p.is_file())
    orphans = [p for p in files if p.name.split("_")[0] not in uids]

    print(f"库里有图的单品：{len(rows)} 件（{len(referenced)} 个 uid）")
    print(f"图片文件总数：{len(files)}，其中无引用：{len(orphans)}")

    if not orphans:
        print("干净。")
        return 0

    if not args.apply:
        for p in orphans[:20]:
            print(f"  待删 {p.name}  {p.stat().st_size // 1024} KB")
        if len(orphans) > 20:
            print(f"  …还有 {len(orphans) - 20} 个")
        print("\n确认无误后加 --apply 真删。")
        return 0

    total = sum(p.stat().st_size for p in orphans)
    for p in orphans:
        p.unlink()
    print(f"已删除 {len(orphans)} 个文件，释放 {total // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
