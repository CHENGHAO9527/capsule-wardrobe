# -*- coding: utf-8 -*-
"""按「名字里的颜色词」修正那些测错了的颜色。

**只用于一种情况：上传的是手机截图。** 截图顶部是状态栏、底部是导航栏，
四角取背景色会取到那两条 UI 栏，导致颜色整个测反（实测 21 张里 18 张是反的）。
这时模型给出的命名（「黑色皮质夹克」「米白阔腿长裤」）反而更可靠 ——
它是"看过图之后"给的，而像素测量在这个场景下是已知错误的。

原图上传的衣服不会走这条路：本地测色是准的，脚本会跳过（它只在
图片被判定为截图、或测色不可信时才动手）。

    python scripts/fix_colors_from_name.py            # 干跑，只看会改什么
    python scripts/fix_colors_from_name.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import catalog, colorlab, db  # noqa: E402
from PIL import Image  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="连原图也一起按命名修正（不建议，原图的本地测色更准）")
    args = ap.parse_args()

    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()

    changes, skipped = [], 0
    for r in rows:
        if not r["img_raw"]:
            skipped += 1
            continue
        p = ROOT / "data" / "images" / Path(r["img_raw"]).name
        if not p.exists():
            skipped += 1
            continue

        with Image.open(p) as im:
            col = colorlab.dominant_color(im.convert("RGB"))

        # 只在"像素测色不可信"时才修：截图，或测色本身就不自信
        unreliable = col["framed"] or not col["confident"]
        if not unreliable and not args.force:
            skipped += 1
            continue

        pal = catalog.color_from_name(r["name"])
        if not pal or pal == r["color_main"]:
            skipped += 1
            continue

        hexv, neutral = next((h, n) for nm, h, n in catalog.COLORS if nm == pal)
        changes.append((r, pal, hexv, neutral, col))

    print(f"共 {len(rows)} 件，需要修正 {len(changes)} 件，跳过 {skipped} 件\n")
    for r, pal, hexv, neutral, col in changes:
        print(f"  id={r['id']:<4} {r['name']:<24}")
        print(f"        {r['color_main'] or '(空)':<6} {r['color_hex'] or '':<9}"
              f" →  {pal:<5} {hexv}"
              f"     （本地测到 {col['name']} {col['observed_hex']}"
              f"，截图={col['framed']}）")

    if not changes:
        print("没有要改的。")
        conn.close()
        return 0

    if not args.apply:
        print("\n这是干跑。确认无误后加 --apply。")
        conn.close()
        return 0

    backup = db.DB_PATH.with_name(
        f"closet.db.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db.DB_PATH, backup)
    print(f"\n已备份 → {backup.name}")

    for r, pal, hexv, neutral, _ in changes:
        conn.execute(
            "UPDATE items SET color_main = ?, color_hex = ?, is_neutral = ?, "
            "notes = COALESCE(notes,'') || ' ｜ 颜色按识别命名推断（原图是截图）', "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (pal, hexv, neutral, r["id"]),
        )
    conn.commit()
    conn.close()
    print(f"已修正 {len(changes)} 件的颜色。")
    print("这些是**推断值**，不是测量值 —— 传原图重录能得到真正准确的颜色。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
