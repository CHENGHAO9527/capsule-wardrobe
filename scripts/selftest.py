"""算法自检：不经过 HTTP，直接把引擎跑一遍看输出是否合理。

用法：python scripts/selftest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.engine import graph, prebuy  # noqa: E402

LINE = "─" * 62


def main() -> None:
    db.init_db()
    conn = db.get_conn()
    items = db.load_items(conn, "owned")
    conn.close()

    print(LINE)
    print(f"在库单品 {len(items)} 件")
    from collections import Counter
    print("槽位分布：", dict(Counter(it["slot"] for it in items)))
    print(LINE)

    t0 = time.time()
    closet = graph.Closet(items)

    print("\n【各温度带的搭配空间】")
    for t in graph.TEMP_BANDS:
        cores, variants = closet.core_and_variant(t)
        cap = closet.core_capacity(t)
        layers = graph.layers_for_temp(t)
        dens = (cores / cap * 100) if cap else 0.0
        print(f"  {t:>3}℃  {len(layers)} 层  核心 {cores:>5} 个 "
              f"（变体 {variants:>6}）  上限 {cap:>5}  密度 {dens:>5.1f}%  "
              f"效率 {round(cores / len(items), 2):>5} 个/件")
    print(f"  （耗时 {time.time() - t0:.2f}s，成对缓存 {len(closet._pcache)} 条）")

    temp = 20
    print(f"\n【{temp}℃ 衣橱体检】")
    t0 = time.time()
    stats = graph.wardrobe_stats(closet, temp)
    print(f"  核心组合 {stats['n_cores']} 个（叠穿变体 {stats['n_variants']} 套）"
          f" · 理论上限 {stats['core_capacity']} · 密度 {stats['density'] * 100:.0f}%")
    print(f"  在库 {stats['n_items']} 件 · 效率 {stats['efficiency']} 个/件"
          f" · 全年累计 {stats['total_all_season']} 个核心")
    print(f"  （耗时 {time.time() - t0:.2f}s）")

    print(f"\n【全季孤岛件】(所有温度都搭不上的，这些才该扔)")
    if stats["islands"]:
        for it in stats["islands"]:
            print(f"  · {it['name']}  ({it['category']})")
    else:
        print("  无")

    print(f"\n【当季不参与】(只是这个温度穿不上，换季再收起来)")
    if stats["inactive"]:
        for it in stats["inactive"]:
            print(f"  · {it['name']}  ({it['slot']} · {it['category']})")
    else:
        print("  无")

    print(f"\n【枢纽件排行】")
    t0 = time.time()
    for row in graph.hinge_ranking(closet, temp, top=6):
        print(f"  · {row['name']:<14} 删掉少 {row['delta_n']} 个核心")
    print(f"  （耗时 {time.time() - t0:.2f}s）")

    # ── 买前模拟：一件卡其直筒裤 ──
    candidate = {
        "id": 90001, "name": "卡其色直筒裤（候选）", "slot": "L4",
        "category": "直筒裤", "color_main": "卡其", "color_hex": "#B9A17A",
        "is_neutral": 1, "formality": 3, "temp_min": 5, "temp_max": 30,
        "thickness": 3, "collar": "", "length_band": "长", "sleeve": "",
        "fit": "合身", "material": "棉", "stage": "candidate",
    }

    print(f"\n【买前模拟】{candidate['name']}")
    t0 = time.time()
    r = prebuy.evaluate(candidate, items)
    print(f"  base {r['base_n']} → with {r['with_n']}    ΔN = {r['delta_n']} "
          f"（+{round(r['growth'] * 100)}%）")
    print(f"  覆盖季节 {r['seasons']} · 替代率 {r['replace_rate']} "
          f"· 救活 {r['revived_count']} · 净收益 {r['net_gain']} · 空洞 {r['hole_flag']}")
    print(f"  判定 [{r['verdict']}] {r['verdict_text']}")
    for n in r.get("notes") or []:
        print(f"  ! {n}")
    print(f"  搭档 {r['partner_total']} 件，前 8：")
    for p in r["partners"][:8]:
        print(f"    +{p['count']:>3} 套  {p['name']}")
    if r["revivals"]:
        print("  救活：", "、".join(v["name"] for v in r["revivals"]))
    if r["conflicts"]:
        print("  搭不上（前 6）：")
        for c in r["conflicts"][:6]:
            print(f"    × {c['name']:<14} {c['reason']}")
    print(f"  （耗时 {time.time() - t0:.2f}s）")

    # ── 空洞效应验证：一件衣橱里没有的大衣 ──
    coat = {
        "id": 90002, "name": "驼色大衣（候选）", "slot": "L3",
        "category": "大衣", "color_main": "驼色", "color_hex": "#B08968",
        "is_neutral": 1, "formality": 3, "temp_min": -5, "temp_max": 15,
        "thickness": 4, "collar": "翻领", "length_band": "长", "sleeve": "长袖",
        "fit": "宽松", "material": "羊毛", "stage": "candidate",
    }
    print(f"\n【买前模拟】{coat['name']}")
    t0 = time.time()
    r2 = prebuy.evaluate(coat, items)
    print(f"  ΔN = {r2['delta_n']} · 覆盖 {r2['seasons']} · 替代率 {r2['replace_rate']} "
          f"· 救活 {r2['revived_count']} · 空洞 {r2['hole_flag']}")
    print(f"  判定 [{r2['verdict']}] {r2['verdict_text']}")
    print(f"  主评估温度 {r2['temp']}℃ · 各温度带增量 {r2['delta_by_temp']}")
    for n in r2.get("notes") or []:
        print(f"  ! {n}")
    print(f"  （耗时 {time.time() - t0:.2f}s）")

    print(LINE)


if __name__ == "__main__":
    out_path = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]
    if out_path:
        fh = open(out_path, "w", encoding="utf-8")
        sys.stdout = fh
    try:
        main()
    finally:
        if out_path:
            fh.close()
