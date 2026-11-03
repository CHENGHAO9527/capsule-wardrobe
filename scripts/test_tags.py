"""穿搭标签自检：规则是不是真的生效。

验证五件事：
  1. 「只能单穿」的件不再出现在**被盖住**的位置上，它自己当最外层的位置一个不少
  2. 「只作内搭」的件不再出现在**最外层**的位置上，被盖住的位置一个不少
  3. 两个都打上 → 这件一条搭配都进不去（互斥标签会被夹死）
  4. 打完标签衣橱的总核心数真的变了，撤回后**精确复原** —— 这一条同时验缓存指纹
     有没有覆盖 tags（不覆盖的话撤回了也还是旧结果，而且不报错）
  5. D7「开领（亨利领那种）不能被套头外层盖住」—— 判据是 `collar == "开领"` ×
     外层 `category ∈ PULLOVER_CATS`；同时要确认它**没被拦成死件**

用真实衣橱的**内存副本**跑，绝不写数据库。
用法：python scripts/test_tags.py [--out tags.out.txt]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.engine import graph, rules  # noqa: E402

LINE = "─" * 62
FAIL = []


def check(cond: bool, msg: str) -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)
    return cond


def stack_top(path: list[dict]) -> dict | None:
    stack = [it for it in path if it.get("slot") in rules.STACK_LAYERS]
    if not stack:
        return None
    return max(stack, key=lambda it: rules.LAYER_ORDER.index(it["slot"]))


def roles(closet: graph.Closet, temp: float, item_id: int) -> tuple[int, int]:
    """这件在给定温度下：当最外层上装出现几次（top）/ 被盖住几次（covered）。"""
    top, covered = 0, 0

    def hook(path: list[dict]) -> None:
        nonlocal top, covered
        if not any(it["id"] == item_id for it in path):
            return
        if stack_top(path)["id"] == item_id:
            top += 1
        else:
            covered += 1

    closet.count_paths(temp, path_hook=hook)
    return top, covered


def with_tags(items: list[dict], item_id: int, *keys: str) -> list[dict]:
    """返回一份副本，其中指定单品的 tags 被替换。不碰原 dict。"""
    out = []
    for it in items:
        c = dict(it)
        if c["id"] == item_id:
            c["tags"] = json.dumps(list(keys), ensure_ascii=False) if keys else None
        out.append(c)
    return out


def main() -> None:
    db.init_db()
    conn = db.get_conn()
    items = db.load_items(conn, "owned")
    conn.close()

    print(LINE)
    print("① 解析：三种写法都要能读出同一个集合")
    print(LINE)
    forms = [
        ('["solo_only"]', "JSON 数组"),
        ("solo_only", "裸字符串"),
        ("solo_only, never_outer", "逗号分隔"),
        ("", "空值"),
        ("{坏掉的", "坏掉的值"),
    ]
    for raw, label in forms:
        got = rules.item_tags({"tags": raw})
        print(f"    {label:<10} {raw!r:<28} → {sorted(got)}")
    check(rules.item_tags({"tags": '["solo_only"]'}) == frozenset({"solo_only"}), "JSON 数组解析正确")
    check(rules.item_tags({"tags": "solo_only, never_outer"})
          == frozenset({"solo_only", "never_outer"}), "逗号分隔解析正确")
    check(rules.item_tags({}) == frozenset(), "字段缺失也是空集")
    # 真正的安全属性不是"脏值必须读成空集"，而是**它绝不能变成一条有效标签**。
    # 读不出来的词在规则里是惰性的（规则只认 TAG_SOLO / TAG_INNER 两个 key），
    # 所以既不会误判叠穿能力，也不会把衣服踢出搭配。
    dirty = rules.item_tags({"tags": "{坏掉的"})
    print(f"    脏值读出来是 {sorted(dirty)} —— 都不是有效标签，规则里惰性")
    check(not (dirty & {rules.TAG_SOLO, rules.TAG_INNER}),
          "脏值不会误判成任何有效标签（未知标签在规则里惰性）")

    print()
    print(LINE)
    print("② 挑一件又能单穿、又会被盖住的内搭来做实验")
    print(LINE)
    closet = graph.Closet(items)
    # ≥14 且 <18：L1/L4/L5 必需，L2/L3 可选 → 内搭既能当最外层，也能被盖住
    warmer = 17
    tgt, tgt_roles = None, (0, 0)
    for it in items:
        if it["slot"] != "L1":
            continue
        top, covered = roles(closet, warmer, it["id"])
        if top > 0 and covered > 0:
            tgt, tgt_roles = it, (top, covered)
            print(f"    {it['name']}（{it.get('category')}）：{warmer}℃ 下当最外层 {top} 次、"
                  f"被盖住 {covered} 次")
            break

    # ⚠️ **挑不到就造一个，绝不跳过 ③④⑤⑥。**
    # 原来这里是 `print("没找到…跳过 ③④")` —— 那正是"防御性跳过会造假绿"：
    # 报告照样印「全部通过」，而五条最核心的标签断言一条都没跑。
    # （真实衣橱的衣服是会变的：L1 全变成薄 T 恤之后，16℃ 下单穿就不够暖了，
    #   于是 top == 0，整个 ③–⑥ 静默消失。）
    # 合成件只活在**内存副本**里，不写数据库；warmth 按当天需求现算。
    if tgt is None:
        # `graph.Closet(items)` 不传 comfort 时用的是 `DEFAULT_COMFORT`，
        # 所以这里的需求也按同一个口径算 —— 两处口径要一致，否则合成件会
        # "看起来够暖、跑起来不够暖"，然后 ③④⑤⑥ 又变成 0 次。
        need = rules.comfort_of(None) - warmer
        probe = {
            "id": 900001, "name": "实验件（合成）", "slot": "L1", "category": "T恤",
            "collar": "圆领", "sleeve": "长袖", "length_band": "常规", "fit": "合身",
            "material": "棉", "pattern": "纯色", "color_hex": "#8A8A85", "is_neutral": 1,
            "formality": 2, "thickness": 2,
            "warmth": max(1, min(8, int(need + 0.999))),   # 单穿刚好够暖
            "temp_min": 0, "temp_max": 32,
            "tags": None, "status": "in_wardrobe", "stage": "owned",
        }
        items = items + [probe]
        closet = graph.Closet(items)
        top, covered = roles(closet, warmer, probe["id"])
        tgt, tgt_roles = probe, (top, covered)
        print(f"    真实衣橱里没有能同时单穿又被盖住的内搭 → 造一个合成件顶上"
              f"（warmth={probe['warmth']}、{warmer}℃）")
        print(f"    {probe['name']}：当最外层 {top} 次、被盖住 {covered} 次")
        check(top > 0 and covered > 0,
              "合成件在实验温度下确实既能单穿又能被盖住（否则后面的断言没有意义）")

    base_cores = closet.count_cores(warmer)
    print(f"    {warmer}℃ 衣橱总核心组合：{base_cores} 个")

    for key, label, drop in ((rules.TAG_SOLO, "只能单穿", "covered"),
                             (rules.TAG_INNER, "只作内搭", "top")):
        print()
        print(LINE)
        print(f"③ 标上「{label}」（期望：{drop} 的位置归零，另一个位置一个不少）")
        print(LINE)
        c2 = graph.Closet(with_tags(items, tgt["id"], key))
        top, covered = roles(c2, warmer, tgt["id"])
        keep = tgt_roles[0] if drop == "covered" else tgt_roles[1]
        gone = tgt_roles[1] if drop == "covered" else tgt_roles[0]
        print(f"    当最外层 {top} 次（原 {tgt_roles[0]}）· 被盖住 {covered} 次（原 {tgt_roles[1]}）")
        check((covered == 0) if drop == "covered" else (top == 0),
              f"「{label}」后 {drop} 的位置归零")
        check((top == keep) if drop == "covered" else (covered == keep),
              f"「{label}」后另一个位置保持 {keep} 次不变")
        check(gone > 0, f"确实砍掉了 {gone} 条（不是一条都没砍的空操作）")

    print()
    print(LINE)
    print("⑤ 两个标签同时打上 → 这件被夹死，一条搭配都进不去")
    print(LINE)
    c3 = graph.Closet(with_tags(items, tgt["id"], rules.TAG_SOLO, rules.TAG_INNER))
    top, covered = roles(c3, warmer, tgt["id"])
    print(f"    当最外层 {top} 次 · 被盖住 {covered} 次")
    check(top == 0 and covered == 0, "互斥标签：这件完全不参与搭配")

    # 规则的判据直接测一遍：确认拒绝理由来自标签，而不是被别的规则顺手拦掉的
    tee = dict(tgt, tags=json.dumps([rules.TAG_SOLO]), name="实验件")
    coat = {"slot": "L3", "name": "实验外套", "tags": None}
    inner_tee = dict(tgt, tags=json.dumps([rules.TAG_INNER]), name="实验件")
    r_solo = rules.tag_conflict([tee, coat])
    r_inner = rules.tag_conflict([inner_tee])
    print(f"    tag_conflict([只能单穿的T恤, 外套]) → {r_solo!r}")
    print(f"    tag_conflict([只作内搭的T恤])       → {r_inner!r}")
    check(bool(r_solo) and "只能单穿" in r_solo, "认出「只能单穿却被外套盖住」")
    check(bool(r_inner) and "只作内搭" in r_inner, "认出「只作内搭却当了最外层」")

    print()
    print(LINE)
    print("⑥ 撤回标签 → 核心数必须精确复原（同时验证缓存指纹覆盖了 tags）")
    print(LINE)
    c4 = graph.Closet(with_tags(items, tgt["id"]))
    back = c4.count_cores(warmer)
    print(f"    撤回后 {warmer}℃ 总核心：{back}（原 {base_cores}）")
    check(back == base_cores, "撤回后精确复原 —— 指纹覆盖了 tags，没有返回缓存的旧结果")

    print()
    print(LINE)
    print("⑦ D7 门襟 × 套头：开领（亨利领那种）不能被套头外层盖住")
    print(LINE)

    # 先钉"接没接上" —— 写了函数却忘了加进 STACK_CHECKS 是典型的静默失效
    check(rules.d7_placket in rules.STACK_CHECKS,
          "d7_placket 真的挂进了 STACK_CHECKS（不是写了没接上）")

    henleys = [it for it in items if (it.get("collar") or "") == "开领"]
    print(f"    真实衣橱里 开领 的件：{[it['name'] for it in henleys] or '（没有）'}")
    check(len(henleys) >= 1, "至少有一件开领（否则这条规则在真实数据上无从验证）")

    def _probe(inner_collar, outer_cat, outer_slot="L2"):
        return rules.d7_placket(
            {"slot": "L1", "name": "内", "collar": inner_collar, "category": "T恤"},
            {"slot": outer_slot, "name": "外", "category": outer_cat, "collar": "圆领"})

    # 用户原话："它可以作为内搭在外面穿个夹克；但是，它不能作为内搭在外面再穿个卫衣。"
    cases = [
        ("开领", "卫衣", True),      # 不能配卫衣（套头）
        ("开领", "毛衣", True),      # 同理，套头
        ("开领", "夹克", False),     # 可以配夹克（敞开）
        ("开领", "皮衣", False),     # 有拉链 = 敞开
        ("开领", "大衣", False),
        ("圆领", "卫衣", False),     # 不误伤：平领 T 恤配卫衣是常态
        ("翻领", "卫衣", False),     # 翻领会从套头领口露出来 —— 那正是穿法本意
    ]
    for collar, cat, want in cases:
        r = _probe(collar, cat)
        print(f"    内领={collar:<2} 外层={cat:<4} → {r or '放行'}")
        check(bool(r) == want, f"{collar} + {cat} 应当{'拦下' if want else '放行'}")

    # 端到端：拿真实衣橱数一遍 —— 开领那件不许再出现在"外层是套头"的组合里，
    # 但它"自己当最外层 / 配敞开外套"的位置必须还在（不能拦成一个死件）
    hen = henleys[0]
    total_bad, total_good = 0, 0
    for t in (12, 14, 16, 18, 20):
        closet = graph.Closet(items)
        seen = {"bad": 0, "good": 0}

        def hook(path, hen=hen, seen=seen):
            if not any(it["id"] == hen["id"] for it in path):
                return
            outsides = [it for it in path
                        if it.get("slot") in rules.STACK_LAYERS
                        and it["id"] != hen["id"]
                        and rules.LAYER_ORDER.index(it["slot"])
                        > rules.LAYER_ORDER.index(hen["slot"])]
            if not outsides:
                seen["good"] += 1                       # 它自己当最外层
                return
            # 只看**紧邻它外面**那一层：中间还隔着别的层时，由那一对去判
            nearest = min(outsides, key=lambda it: rules.LAYER_ORDER.index(it["slot"]))
            if (nearest.get("category") or "") in rules.PULLOVER_CATS:
                seen["bad"] += 1                        # 被套头盖住 = 漏网
            else:
                seen["good"] += 1
        closet.count_paths(t, path_hook=hook)
        print(f"    {t}℃：被套头盖住 {seen['bad']} 次 · 合法出现 {seen['good']} 次")
        total_bad += seen["bad"]
        total_good += seen["good"]

    check(total_bad == 0, "12–20℃ 下开领件一次都没被套头盖住")
    check(total_good > 0, "开领件仍然用得上（没被这条规则拦成死件）")

    print()
    print(LINE)
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for m in FAIL:
            print("  × " + m)
    else:
        print("全部通过。")
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
