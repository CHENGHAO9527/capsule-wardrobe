# -*- coding: utf-8 -*-
"""把 data/images 里已经上传、但没挂到任何单品上的照片，按识别结果批量建档。

用途：你之前传过一批照片但没入库（或入库时字段不对被删了），图片还留在磁盘上。
这个脚本把它们捞回来 —— 颜色本地测、语义字段交给模型，直接写成正式单品。

    python scripts/import_photos.py              # 干跑，只报告，不动数据库
    python scripts/import_photos.py --apply      # 真的建
    python scripts/import_photos.py --apply --no-dedup

去重规则：按**图片指纹**（缩到 16×16 灰度取哈希）判重，只合并真正重复上传的同一张照片。
不用「槽位+品类+主色」判重 —— 那太粗，会把两件不同的白T恤当成一件。
语义上相似的会在报告里列出来给你看，但不会自动跳过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import colorlab, db, vision  # noqa: E402
from PIL import Image  # noqa: E402

CACHE = ROOT / "data" / "_vision_cache.json"


def phash(path: Path) -> str:
    """感知指纹：缩到 16×16 灰度后取哈希。同一张照片重复上传必然撞上，
    不同衣服不会撞（哪怕品类和颜色一样）。"""
    with Image.open(path) as im:
        small = im.convert("L").resize((16, 16), Image.BILINEAR)
        return hashlib.md5(small.tobytes()).hexdigest()


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    ap.add_argument("--no-dedup", action="store_true", help="不去重")
    ap.add_argument("--min-conf", type=float, default=0.5, help="低于此置信度不导入")
    args = ap.parse_args()

    conn = db.get_conn()
    conn.row_factory = sqlite3.Row

    used = set()
    for r in conn.execute("SELECT img_display, img_raw, img_llm FROM items").fetchall():
        for v in r:
            if v:
                used.add(Path(v).name.split("_")[0])

    files = sorted((ROOT / "data" / "images").glob("*_raw.jpg"))
    todo = [p for p in files if p.name.split("_")[0] not in used]
    print(f"磁盘上共 {len(files)} 张原图，其中 {len(todo)} 张还没挂到任何单品上")
    if not todo:
        print("没有要导入的。")
        conn.close()
        return 0

    cached = load_cache()
    rows = []
    for p in todo:
        uid = p.name.split("_")[0]
        with Image.open(p) as im:
            col = colorlab.dominant_color(im.convert("RGB"))

        if uid in cached:
            res = cached[uid]
            src = "缓存"
        else:
            res = vision.analyze(p)
            cached[uid] = res
            src = "模型"
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8")

        rows.append({
            "uid": uid,
            "raw": f"/img/{uid}_raw.jpg",
            "display": f"/img/{uid}_display.webp",
            "llm": f"/img/{uid}_llm.jpg",
            "color": col,
            "rec": res,
            "src": src,
            "hash": phash(p),
        })

    print(f"（识别结果已缓存到 {CACHE.name}，重跑不再重复调用模型）")
    print()

    # ── 判定哪些是真衣服 ──
    keep, skipped = [], []
    for r in rows:
        rec = r["rec"]
        if not rec.get("ok"):
            skipped.append((r, f"识别失败：{rec.get('error', '')[:50]}"))
            continue
        name = (rec.get("name") or "").strip()
        conf = rec.get("_confidence") or 0
        # 没有名字 = 模型没看出这是件衣服（纯色方块、截图、空白图都会这样）
        if not name:
            skipped.append((r, "模型没给出名字（看着不像一件衣服）"))
            continue
        if conf < args.min_conf:
            skipped.append((r, f"置信度 {conf} 低于 {args.min_conf}"))
            continue
        keep.append(r)

    # ── 去重：只按图片指纹，避免把两件不同的白T恤合成一件 ──
    if not args.no_dedup:
        seen, deduped = {}, []
        for r in keep:
            first = seen.get(r["hash"])
            if first is not None:
                skipped.append((r, f"和 {first['uid'][:8]}… 是同一张照片（指纹相同）"))
                continue
            seen[r["hash"]] = r
            deduped.append(r)
        keep = deduped

    # ── 语义上像同一件的，只提示，不自动跳过 ──
    buckets = defaultdict(list)
    for r in keep:
        rec = r["rec"]
        buckets[(rec["slot"], rec["category"], r["color"]["hex"] or "")].append(r)
    alike = {k: v for k, v in buckets.items() if len(v) > 1}

    print(f"准备导入 {len(keep)} 件，跳过 {len(skipped)} 张：")
    print()
    for r in keep:
        rec, col = r["rec"], r["color"]
        print(f"  + {rec['slot']}/{rec['category']:<6} 「{rec['name']}」"
              f"  {col['name']}{col['hex']}  正式{rec['formality']} 厚{rec['thickness']}"
              f"  {rec['temp_min']}~{rec['temp_max']}℃  conf={rec['_confidence']}")
    for r, why in skipped:
        print(f"  - {r['uid'][:12]} 跳过：{why}")

    if alike:
        print()
        print("提醒：下面这些在「槽位+品类+主色」上完全一样，可能真的是同款两件，也可能是识别不准。")
        print("      脚本不会自动跳过它们 —— 觉得重复的话导入后自己删：")
        for (slot, cat, hexv), v in alike.items():
            print(f"      {slot}/{cat} {hexv}: " + "、".join(x["rec"]["name"] for x in v))

    if not args.apply:
        print()
        print("这是干跑。确认无误后加 --apply 真写入。")
        conn.close()
        return 0

    today = date.today().isoformat()
    made = 0
    for r in keep:
        rec, col = r["rec"], r["color"]
        body = {
            "name": rec["name"], "slot": rec["slot"], "category": rec["category"],
            "stage": "owned", "status": "in_wardrobe",
            "color_main": col["name"], "color_hex": col["hex"], "is_neutral": col["neutral"],
            "formality": rec["formality"], "thickness": rec["thickness"],
            "temp_min": rec["temp_min"], "temp_max": rec["temp_max"],
            "collar": rec["collar"], "sleeve": rec["sleeve"],
            "length_band": rec["length_band"], "fit": rec["fit"],
            "material": rec["material"], "pattern": rec["pattern"],
            "img_raw": r["raw"], "img_display": r["display"], "img_llm": r["llm"],
            "notes": f"由照片自动识别导入（{today}），字段可改",
        }
        cols = ", ".join(body)
        ph = ", ".join("?" * len(body))
        conn.execute(f"INSERT INTO items ({cols}) VALUES ({ph})", list(body.values()))
        made += 1
    conn.commit()
    conn.close()
    print()
    print(f"已导入 {made} 件。打开 http://127.0.0.1:8077/wardrobe 看「已入库」，"
          f"不对的每行右边有「删」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
