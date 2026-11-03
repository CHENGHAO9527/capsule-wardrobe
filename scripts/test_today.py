# -*- coding: utf-8 -*-
"""今日推荐 + 反馈学习自检。不经过 HTTP。

    python scripts/test_today.py --out today.out.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, outfits  # noqa: E402
from app.engine import graph  # noqa: E402

LINES: list[str] = []


def say(s: str = "") -> None:
    LINES.append(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()

    conn = db.get_conn()
    items = db.load_items(conn, "owned")
    say(f"在库单品 {len(items)} 件")

    closet = graph.Closet(items)
    by_id = {it["id"]: it for it in items}
    weights = outfits.load_weights(conn)
    worn = outfits.recent_wear_map(conn)

    fails = 0

    for temp, occ in ((20, "通勤"), (8, "通勤"), (16, None), (22, None), (30, "休闲")):
        res = outfits.recommend(closet, temp, occ, weights, worn, n=3, seed=7)
        say()
        say(f"══ {temp}℃ {occ or '不限'} ══  核心组合 {res['cores']} 个"
            f"（叠穿变体 {res['variants']} 套），选出 {len(res['outfits'])} 套")

        if not res["outfits"]:
            # ⚠️ **"这个温度排不出来"是允许的**（2026-09-23「26℃穿衣法则」之后）：
            # 这台衣橱顶不住那个温度时，引擎宁可空着也不"凑一堆勉强合法"。
            # 但**必须说得出为什么** —— 空态不说清楚，用户只会以为程序坏了。
            d = closet.diagnose(temp)
            if d["kind"] in ("missing_layer", "too_cold"):
                say(f"  （{temp}℃ 组合不出搭配 —— {d['kind']}：{d['hint']}）")
            else:
                fails += 1
                say(f"  FAIL 这个温度排不出来，但原因说不清：{d['kind']} / {d['hint']}")
            continue

        # 校验：每套都必须是通过硬规则的合法路径，且不重复
        seen_sets = []
        for i, o in enumerate(res["outfits"], 1):
            ids = [it["id"] for it in o["items"]]
            names = " ＋ ".join(it["name"] for it in o["items"])
            say(f"  {i}. [{o['score']} 分] {names}")
            say(f"     {o['reason']}")
            for n in o.get("notes") or []:
                say(f"     ! {n}")

            err = graph.rules.outfit_ok(o["items"], temp, occ)
            if err:
                fails += 1
                say(f"     FAIL 这套居然不合法：{err}")
            if len(set(ids)) != len(ids):
                fails += 1
                say("     FAIL 同一件出现了两次")

            # 温度不变量：任何一件的上限都不能低于当天温度（30℃ 不该穿厚毛衣）。
            # 下限只对暴露层成立 —— 内搭被盖住时冷不该由它负责。
            # 注意：推荐结果里的单品是精简过的，温度区间要回衣橱取。
            for it in o["items"]:
                full = by_id.get(it["id"]) or {}
                tmax, tmin = full.get("temp_max"), full.get("temp_min")
                if tmax is not None and tmax < temp:
                    fails += 1
                    say(f"     FAIL「{it['name']}」最高只到 {tmax}℃，"
                        f"不该出现在 {temp}℃")
                if (full.get("slot") in ("L3", "L4", "L5")
                        and tmin is not None and tmin > temp):
                    fails += 1
                    say(f"     FAIL「{it['name']}」最低 {tmin}℃，"
                        f"不该出现在 {temp}℃")

            # 多样性：跟已选的每一套都要至少差 2 件
            for prev in seen_sets:
                inter = len(set(ids) & set(prev))
                if inter > len(ids) - 2:
                    fails += 1
                    say(f"     FAIL 与上一套只差 {len(ids) - inter} 件，太像了")
            seen_sets.append(ids)

    # ── 空态必须能自证 ──
    say()
    say("══ 空态诊断（算不出搭配时，要说清卡在哪一层）══")
    for temp in (0, -8, 40):
        n = closet.count_cores(temp)
        d = closet.diagnose(temp)
        say(f"  {temp}℃ → {n} 套 · {d['kind']} · 叠层上限 {d['max_stack']}")
        say(f"     {d['hint']}")
        for l in d["layers"]:
            flag = "必需" if l["required"] else "可选"
            say(f"       {l['name']:<4} {flag}  合格 {l['eligible']}/{l['pool']}")
        if n == 0 and not d["hint"]:
            fails += 1
            say("     FAIL 空态没有给出诊断")
        if n > 0 and d["kind"] == "missing_layer":
            fails += 1
            say("     FAIL 有搭配却报告缺层")

    # ── 反馈学习闭环 ──
    say()
    say("══ 反馈学习闭环 ══")
    res = outfits.recommend(closet, 20, "通勤", weights, worn, n=1, seed=7)
    if not res["outfits"]:
        say("  20℃ 没有搭配，跳过")
    else:
        o = res["outfits"][0]
        ids = outfits.save_outfits(conn, [o], 20, "通勤")
        oid = ids[0]
        conn.commit()

        before = o["score"]
        before_w = outfits.load_weights(conn)
        targets = [it["id"] for it in o["items"]]
        b0 = [round(before_w["item"].get(i, 0.5), 3) for i in targets]

        say(f"  被否掉的这套（outfit_id={oid}，{before} 分）："
            + " ＋ ".join(it["name"] for it in o["items"]))
        say(f"  反馈前权重的后验均值：{b0}")

        outfits.apply_feedback(conn, oid, "rejected")

        after_w = outfits.load_weights(conn)
        a0 = [round(after_w["item"].get(i, 0.5), 3) for i in targets]
        say(f"  反馈后权重的后验均值：{a0}")
        if all(a < b for a, b in zip(a0, b0)):
            say("  ok   所有单品权重都下降了（拒绝 → beta +1）")
        else:
            fails += 1
            say("  FAIL 权重没下降")

        # 再算一次，被否的那套应该不再排在第一位
        res2 = outfits.recommend(closet, 20, "通勤", after_w, worn, n=3, seed=7)
        if res2["outfits"]:
            top = res2["outfits"][0]
            same = {it["id"] for it in top["items"]} == set(targets)
            say(f"  重新推荐第 1 名：{top['score']} 分，"
                f"{'还是被否的那一套' if same else '＋'.join(it['name'] for it in top['items'])}")
            rejected_back = next((o for o in res2["outfits"]
                                  if {it["id"] for it in o["items"]} == set(targets)), None)
            if same:
                fails += 1
                say("  FAIL 被否的组合还排第一")
            elif rejected_back is not None:
                say(f"  ok   被否的组合掉到第 {res2['outfits'].index(rejected_back) + 1} 名，"
                    f"分数 {rejected_back['score']} < {before}")
            else:
                say("  ok   被否的组合直接被挤出前三")
        else:
            fails += 1
            say("  FAIL 反馈之后推荐不出任何搭配")

        # 清理测试产生的记录
        conn.execute("DELETE FROM wear_log WHERE outfit_id = ?", (oid,))
        conn.execute("DELETE FROM outfits WHERE id = ?", (oid,))
        conn.execute("DELETE FROM w_item WHERE item_id IN (%s)"
                     % ",".join("?" * len(targets)), targets)
        conn.execute("DELETE FROM w_pair WHERE item_a_id IN (%s)"
                     % ",".join("?" * len(targets)), targets)
        conn.commit()
        say("  （测试写入的反馈与权重已清理，不影响真实数据）")

    conn.close()

    say()
    say("=" * 58)
    say(f"失败项：{fails}" + ("  全部通过" if fails == 0 else "  ← 需要修"))

    text = "\r\n".join(LINES)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}  ({fails} failures)")
    else:
        print(text)


if __name__ == "__main__":
    main()
