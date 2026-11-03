# -*- coding: utf-8 -*-
"""重新生成展示图：对已经录进库的照片，重跑一遍「拆边框 + 4:5 合成 + 重测颜色」。

用途：早期版本的展示图是把竖长图缩进正方形再补白边，还带着截图的状态栏和导航栏，
缩略图看起来又脏又小。这个脚本用原图（img_raw 一直都在）重新生成一遍。

关键点：
  · **只动几何，不动内容。** 不抠图、不重绘、不调色。
  · 颜色在**拆完边框**之后重测。拆掉黑边后四角的背景估计才成立，
    这时测出来的才是真颜色 —— 比上一轮「从命名里推断」靠谱得多。
  · 只有新测色**可信**时才覆盖；不可信就保留原值（可能是上一轮推断的）。

    python scripts/renormalize_images.py            # 干跑
    python scripts/renormalize_images.py --apply
    python scripts/renormalize_images.py --apply --no-color   # 只重出图，不动颜色
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import colorlab, db, imgs  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-color", action="store_true",
                    help="只重新生成展示图，不覆盖已测出的颜色。"
                         "颜色是用户见过、甚至手动改过的值 —— 只想修几何时用这个。")
    args = ap.parse_args()

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM items WHERE img_raw IS NOT NULL AND img_raw <> '' ORDER BY id"
    ).fetchall()

    print(f"有图单品 {len(rows)} 件\n")
    plan = []
    for r in rows:
        raw = ROOT / "data" / "images" / Path(r["img_raw"]).name
        if not raw.exists():
            print(f"  id={r['id']} 原图丢失，跳过")
            continue
        with Image.open(raw) as f:
            im = ImageOps.exif_transpose(f).convert("RGB")
        clean, box, trim = imgs.strip_bands(im)
        col = colorlab.dominant_color(clean)
        plan.append((r, clean, box, col, raw))

        keep = f"{clean.width}×{clean.height}"
        pct = round(clean.width * clean.height / (im.width * im.height) * 100)
        cs = f"{col['name']} {col['hex']}" if col["confident"] else f"{col['name']}（不可信）"
        print(f"  id={r['id']:<4} {r['name']:<24} 原图{im.width}×{im.height}"
              f"  →  {keep:<12} 留{pct:>3}%  重测色 {cs}")

    if args.apply:
        n_img = n_col = 0
        for r, clean, box, col, raw in plan:
            uid = Path(r["img_raw"]).name.split("_")[0]
            imgs.compose(clean, 1080).save(
                ROOT / "data" / "images" / f"{uid}_display.webp", "WEBP", quality=88)
            imgs.compose(clean, 512).save(
                ROOT / "data" / "images" / f"{uid}_llm.jpg", "JPEG", quality=80)
            n_img += 1
            # 只有可信的测量才覆盖颜色；不可信就保留原值
            if (not args.no_color) and col["confident"] and col["name"] and col["name"] != r["color_main"]:
                conn.execute(
                    "UPDATE items SET color_main=?, color_hex=?, is_neutral=?, "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (col["name"], col["hex"], col["neutral"], r["id"]))
                n_col += 1
        conn.commit()
        tail = "" if args.no_color else f"，其中修正颜色 {n_col} 件"
        print(f"\n已重新生成 {n_img} 张展示图{tail}。")
    else:
        print("\n这是干跑。确认无误后加 --apply。")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
