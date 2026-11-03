# -*- coding: utf-8 -*-
"""找重复件（命令行版）。检测逻辑在 `app/dupes.py`，录入页用的是同一个函数 ——
判据写两份必然漂移，这个项目在温度口径上已经栽过三次。

    python scripts/find_dupes.py                       # 报告
    python scripts/find_dupes.py --sheet _probe/d.jpg  # 顺便出一张左右对照图
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, dupes  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="", help="把可疑对子拼成一张对照图")
    args = ap.parse_args()

    conn = db.get_conn()
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM items WHERE stage='owned' AND status='in_wardrobe' ORDER BY slot, id"
    ).fetchall()]
    conn.close()

    pairs = dupes.find(items)
    print(f"在库 {len(items)} 件，其中带原图 {sum(1 for i in items if i.get('img_raw'))} 件\n")
    if not pairs:
        print("没有发现可疑的重复件。")
        return 0

    exact = sum(1 for p in pairs if p["exact"])
    print(f"可疑对子 {len(pairs)} 组（其中 {exact} 组是像素完全相同的同一张图）\n")
    for p in pairs:
        print(f"  [{p['verdict']}]  像素RMS {p['rms']} · 指纹 {p['hash_dist']}/256"
              + ("  · 同槽位同品类" if p["same_category"] else ""))
        for x in (p["a"], p["b"]):
            price = ('¥' + str(x["purchase_price"])) if x.get("purchase_price") else "无价"
            print(f"      id={x['id']:<4} {str(x['name'])[:18]:<20} {str(x.get('category') or ''):<6} "
                  f"{str(x.get('color_main') or ''):<4} 正式{x.get('formality')} 厚{x.get('thickness')} "
                  f"{x.get('temp_min')}~{x.get('temp_max')}℃ {price}")
        print()

    if args.sheet:
        CW, CH = 200, 250
        sheet = Image.new("RGB", (2 * CW * len(pairs), CH), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)
        for k, p in enumerate(pairs):
            for c, x in enumerate((p["a"], p["b"])):
                pth = ROOT / "data" / "images" / Path(x["img_display"] or "").name
                if pth.exists():
                    with Image.open(pth) as f:
                        cell = f.convert("RGB").resize((CW, CH))
                else:
                    cell = Image.new("RGB", (CW, CH), (240, 240, 240))
                sheet.paste(cell, ((k * 2 + c) * CW, 0))
                draw.rectangle([(k * 2 + c) * CW, 0, (k * 2 + c) * CW + CW - 1, 16], fill=(0, 0, 0))
                draw.text(((k * 2 + c) * CW + 3, 3), f"id{x['id']} {str(x['name'])[:12]}",
                          fill=(255, 255, 0))
            draw.line([(k * 2 * CW, 0), (k * 2 * CW, CH)], fill=(255, 0, 0), width=3)
        sheet.save(args.sheet, "JPEG", quality=88)
        print(f"对照图已写到 {args.sheet}（每组左右两件，红线分隔）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
