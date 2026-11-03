# -*- coding: utf-8 -*-
"""L4I（内穿下装 / 秋裤）槽位自检。**离线，不需要 Key。**

    python scripts/test_l4i.py --out test_l4i.out.txt

背景（用户原话）：秋裤按 L4 录进来，30℃ 那天被排出来了。
根子是**只有 L1–L5 五个槽位，没有"裤子里再穿一件"这个概念** ——
它不是数据错误，是缺一个层。2026-09-21 加了 L4I。

这个槽位有三条"不许破"的性质，缺一条它就会污染整个引擎：

  ① **14℃ 以下才出现**（`graph.layer_plan`）。14℃ 以上完全不进候选，
     于是"30℃ 排出一条秋裤"从根上不可能 —— 不需要去改用户那条 -5~34℃ 的数据。
  ② **加不加它，核心组合数一个都不变**。核心 = 最外层上装×下装×鞋，
     秋裤被外裤盖住，不改变"穿出门看起来是什么样"。这条破了，
     衣橱体检的密度、买前模拟的 ΔN 全部失真。
  ③ **它永远被外裤盖住**，不单独出现；颜色和正式度也不参与判定
     （秋裤是高饱和色也不该跟别人"撞色"）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, plan  # noqa: E402
from app.engine import graph, rules  # noqa: E402

L: list[str] = []
FAILS: list[str] = []


def say(s: str = "") -> None:
    print(s)
    L.append(s)


def ok(cond: bool, label: str) -> bool:
    if cond:
        say("  ✓ " + label)
    else:
        say("  ✗ " + label)
        FAILS.append(label)
    return bool(cond)


def head(s: str) -> None:
    say()
    say("=" * 64)
    say(s)
    say("=" * 64)


# 故意挑的：高饱和橙（考验撞色豁免）、厚 5、正式度 1、上限 14℃
QIUKU = {
    "id": 990001, "name": "测试秋裤", "slot": "L4I", "category": "秋裤",
    "temp_min": -10, "temp_max": 14, "formality": 1, "thickness": 5,
    "is_neutral": False, "color_hex": "#D97A3A", "color_main": "橙",
    "pattern": "纯色", "material": "混纺", "fit": "修身", "length_band": "长",
    "stage": "owned", "status": "in_wardrobe",
}


def test_layer_plan() -> None:
    head("① 温度决定它出不出现（graph.layer_plan）")
    say("· 14℃ 是分界线：以下可选，以上完全不进候选")
    for t, want in ((0, True), (5, True), (13, True), (13.9, True),
                    (14, False), (15, False), (20, False), (30, False)):
        opt = graph.layer_plan(float(t))[1]
        got = "L4I" in opt
        ok(got == want, f"{t:g}℃ 可选层={opt} → 含 L4I={got}（期望 {want}）")
    ok("L4I" not in graph.layer_plan(5.0)[0],
       "L4I 永远是「可选」—— 0℃ 不穿秋裤也是合法搭配")
    ok("L4" in graph.layer_plan(5.0)[0],
       "L4（外裤）在所有温度都是必需层 —— 所以秋裤不可能单独出现")


def test_rules() -> None:
    head("② 规则层：看不见的层不参与判定")
    pants = {"slot": "L4", "name": "西裤", "color_hex": "#4A6B8A", "color_main": "牛仔蓝",
             "formality": 3, "thickness": 3, "temp_min": 0, "temp_max": 32}
    ok(rules.r4_colors(QIUKU, pants) is None,
       "高饱和橙秋裤 + 牛仔蓝西裤 → 不判撞色（被盖住，看不见）")
    ok(rules.r3_formality_gap(QIUKU, pants) is None,
       "正式度 1 的秋裤配正式度 3 的西裤 → 不判正式度差")
    ok(rules.item_temp_ok(QIUKU, 0.0),
       "0℃ 可穿（只校验上限 —— 冷不该由它负责，跟内搭同理）")
    ok(rules.item_temp_ok(QIUKU, 20.0) is False, "20℃ 不可穿（上限 14℃）")
    ok(rules.item_temp_ok(QIUKU, 14.0), "14℃ 刚好卡在上限内")
    # 「26℃穿衣法则」之后没有 COVERED_SLOTS 这个概念了 ——
    # 被覆盖的层怎么算，直接写在 `item_temp_ok` 里。这里改成断言**那套口径本身**：
    ok(rules.warmth(QIUKU) == 0.0,
       "秋裤不参与保暖加总（下半身只考虑搭配）")
    ok(rules.item_temp_ok(QIUKU, -30.0),
       "零下 30℃ 也成立 —— L4I 的温度下限**从不**参与判定")
    ok("L4I" in rules.HIDDEN_SLOTS, "L4I 在 HIDDEN_SLOTS 里（颜色/正式度豁免）")
    ok(rules.LAYER_ORDER.index("L4I") < rules.LAYER_ORDER.index("L4"),
       "层序里 L4I 在 L4 之前（先内后外，『外面还有没有东西』靠这个顺序判）")


def test_engine_on_real_closet() -> None:
    head("③ 跑真实衣橱：核心数一个都不能变")
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()

    if len(items) < 6:
        say(f"在库只有 {len(items)} 件，跳过（先录几件）")
        return

    base = graph.Closet(items)
    withq = graph.Closet(items + [QIUKU])
    say(f"衣橱 {len(items)} 件；对照 = 同一批 + 一件秋裤")
    say()
    say(f"{'温度':>6} {'核心(无)':>9} {'核心(有)':>9} {'含秋裤':>8} {'单穿秋裤':>9}")

    for t in (0, 5, 10, 13, 14, 15, 20, 30):
        c1, _v1 = base.core_and_variant(float(t), None)
        c2, _v2 = withq.core_and_variant(float(t), None)
        stat = {"q": 0, "lonely": 0}

        def hook(p: list[dict]) -> None:
            slots = [x["slot"] for x in p]
            if "L4I" in slots:
                stat["q"] += 1
                if "L4" not in slots:
                    stat["lonely"] += 1

        withq.count_paths(float(t), None, path_hook=hook, limit=200000)
        say(f"{t:>5}℃ {c1:>9} {c2:>9} {stat['q']:>8} {stat['lonely']:>9}")

        ok(c1 == c2,
           f"{t:g}℃ 核心数不受秋裤影响（{c1} = {c2}）—— 它被盖住，不改变「看起来的样子」")
        ok(stat["lonely"] == 0,
           f"{t:g}℃ 没有「只穿秋裤不穿外裤」的路径")
        if t >= 14:
            ok(stat["q"] == 0, f"{t:g}℃ 一条含秋裤的路径都没有")
        elif c1 == 0:
            # 这个温度本来就没有任何合法搭配（衣橱缺能扛这个温度的层位单品），
            # 秋裤自然也无从谈起。**不是 L4I 的问题**，别让它变成假失败。
            say(f"     （{t:g}℃ 整套本来就排不出来 —— 衣橱缺能扛这个温度的件，"
                f"跳过这条断言）")
        else:
            ok(stat["q"] > 0, f"{t:g}℃ 秋裤真的被用上了（{stat['q']} 条路径）")


def test_plan_uses_it() -> None:
    head("④ 排布层：候选池里有它，而且勾成必带就一定会穿上")
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()
    if len(items) < 6:
        say("单品太少，跳过")
        return

    closet = graph.Closet(items + [QIUKU])
    weights = {"item": {}, "pair": {}}

    # ⚠️ 冷天那档取 **13℃** 不是 10℃：换到「26℃穿衣法则」之后，
    # 这套衣橱的保暖值上限是 9（T恤1 + 卫衣4 + 皮衣4），
    # 所以 13℃ 就是它能穿到的极限（22 − 9 = 13）—— 10℃ 压根枚举不出任何搭配，
    # 拿它测"秋裤在不在池子里"会得出假失败。
    COLD = 13.0

    for t in (COLD, 20.0):
        pools = [plan._day_pool(closet, t, None, weights, {}, 0)["outfits"]
                 for _ in range(3)]
        total = sum(len(p) for p in pools)
        hit = sum(1 for p in pools for o in p
                  if any(x["slot"] == "L4I" for x in o["items"]))
        say(f"     {t:g}℃：3 天的候选池共 {total} 套，含秋裤 {hit} 套")
        if t < 14:
            ok(hit > 0, f"{t:g}℃ 的候选池里有含秋裤的组合（这样才可能被排上）")
        else:
            ok(hit == 0, f"{t:g}℃ 的候选池里一套含秋裤的都没有")

    # ⚠️ **不要断言"10℃ 排布一定会用秋裤"** —— 那取决于衣橱里还有什么、
    # 以及打分偏好，用户随手录两件卫衣就会让它变（实测栽过一次：
    # 断言挂了，查半天发现是用户自己录了新衣服，秋裤本身没问题）。
    # 改测**硬性质**：勾成「必带」，10℃ 就必须穿上它。
    days = [{"date": f"2027-01-{i + 1:02d}", "temp": COLD, "tmin": COLD}
            for i in range(4)]
    r = plan.build_plan(closet, days, weights=weights, worn={}, mode="week",
                        seed=0, use_llm=False, must_ids=[990001])
    used = sum(1 for d in r["days"] if d["outfit"]
               and any(x["slot"] == "L4I" for x in d["outfit"]["items"]))
    say(f"     勾成必带之后：{used}/{r['n_planned']} 天穿上了秋裤")
    ok(used > 0, f"勾成必带后，{COLD:g}℃ 这几天一定穿上它")

    hot = [{"date": f"2027-07-{i + 1:02d}", "temp": 20.0, "tmin": 20.0}
           for i in range(4)]
    r2 = plan.build_plan(closet, hot, weights=weights, worn={}, mode="week",
                         seed=0, use_llm=False, must_ids=[990001])
    used2 = sum(1 for d in r2["days"] if d["outfit"]
                and any(x["slot"] == "L4I" for x in d["outfit"]["items"]))
    ok(used2 == 0,
       f"20℃ 就算勾成必带也穿不上（层位不允许），但它在清单里："
       f"{[x['name'] for x in r2['packing'] if x['slot'] == 'L4I']}")

    say()
    say("· 打包清单里 L4I 有独立分组（不能被折进「下装」）")
    groups = {g["slot"]: g["name"] for g in r["packing_groups"]}
    say(f"     清单分组：{groups}")
    if any(x["slot"] == "L4I" for x in r["packing"]):
        ok("L4I" in groups, "清单里出现了独立的「内穿下装」分组")
        ok(groups.get("L4I") != groups.get("L4"), "它跟「下装」是两个分组")
    else:
        say("     （这趟没带上秋裤，跳过分组断言）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()

    say("胶囊衣橱 · L4I（内穿下装 / 秋裤）自检")
    say("（全程离线：只读真实衣橱，另加一件内存里的假秋裤，不写库）")

    test_layer_plan()
    test_rules()
    test_engine_on_real_closet()
    test_plan_uses_it()

    head("结论")
    if FAILS:
        say(f"✗ {len(FAILS)} 项没过：")
        for f in FAILS:
            say("   - " + f)
    else:
        say("✓ 全部通过")

    if args.out:
        (ROOT / args.out).write_text("\n".join(L), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
