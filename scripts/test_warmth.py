"""「26℃穿衣法则」自检：温度模型换了之后，把它的性质钉住。

用法：python scripts/test_warmth.py

为什么单独一个文件：这次改的是**温度口径**，而这个项目的温度口径改过四次、
四次都出过事（放宽被静默抵消、冬天整套放不出来、试删砍光中间层……）。
所以要有一份文档化 + 可执行的清单，说清"这个模型到底是什么"。

覆盖：
  ① 法则本体：气温 + Σ保暖值 ≥ 舒适线（就是那道公式）
  ② 会不会太热：只看**最外层上装** —— 拿它去比被盖住那件是错的
  ③ **单调性**：固定一套衣服，合法温度必须是一个**连续区间**。
     第一版就是在这里翻车的（8℃ 三层非法、5℃ 反倒合法）。
  ④ 下半身只参与搭配，不参与保暖加总
  ⑤ 舒适线可调，而且改了真的会变（防"改了设置没变化"）
  ⑥ 保暖值落库 / 回填
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import catalog, db, prefs  # noqa: E402
from app.engine import graph, rules  # noqa: E402

FAILS: list[str] = []


def head(t: str) -> None:
    print()
    print("─" * 60)
    print(t)
    print("─" * 60)


def ok(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


def mk(iid: int, slot: str, warmth: float, tmin: float, tmax: float,
       thickness: int = 3, **kw) -> dict:
    it = {"id": iid, "name": f"{slot}-{iid}", "slot": slot, "category": "其他",
          "stage": "owned", "status": "in_wardrobe",
          "temp_min": tmin, "temp_max": tmax, "formality": 2,
          "thickness": thickness, "warmth": warmth, "is_neutral": 1,
          "color_main": "黑", "color_hex": "#333333"}
    it.update(kw)
    return it


# ══════════════════════════════════════════════
# ① 法则本体
# ══════════════════════════════════════════════

def test_formula() -> None:
    head("① 法则本体：气温 + Σ保暖值 ≥ 舒适线")

    t = mk(1, "L1", 1, -10, 38)          # T恤
    hood = mk(2, "L2", 4, 5, 20)         # 卫衣
    coat = mk(3, "L3", 5, 0, 22)         # 外套

    ok(rules.warmth(t) == 1, "T恤 保暖值 = 1℃（薄棉毛衫那一档）")
    ok(rules.stack_warmth([t, hood, coat]) == 10, "三层叠满 = 1 + 4 + 5 = 10℃")

    comfort = 22.0
    # 22 − 10 = 12℃：这是这套衣服能穿到的**最低**温度
    ok(rules.stack_warm_ok([t, hood, coat], 12.0, comfort),
       "12℃：10 + 12 = 22 ≥ 22 → 够暖")
    ok(not rules.stack_warm_ok([t, hood, coat], 11.0, comfort),
       "11℃：10 + 11 = 21 < 22 → 不够暖（差 1℃）")

    # 内搭自己不算"覆盖保暖"是刻意的：算了的话 18℃ 单穿短袖也会合法
    ok(not rules.stack_warm_ok([t], 18.0, comfort),
       "18℃ 只穿 T 恤 → 不够暖（用户原话：18 度怎么可能只穿短袖）")
    ok(rules.stack_warm_ok([t], 21.0, comfort), "21℃ 只穿 T 恤 → 够暖")

    # 舒适线的钳位：传进去一个荒唐的值不该把引擎带跑
    ok(rules.comfort_of(-999) == rules.COMFORT_MIN, "舒适线被钳到下界 18℃")
    ok(rules.comfort_of(999) == rules.COMFORT_MAX, "舒适线被钳到上界 28℃")
    ok(rules.comfort_of(None) == rules.DEFAULT_COMFORT, "没传就用默认值")


# ══════════════════════════════════════════════
# ② 会不会太热：只看最外层
# ══════════════════════════════════════════════

def test_too_hot() -> None:
    head("② 会不会太热：只比**最外层上装**的上限")

    t = mk(1, "L1", 1, -10, 38)
    hood = mk(2, "L2", 4, 5, 20)

    ok(rules.item_temp_ok(t, 25.0, [t, hood]), "被盖住的内搭，25℃ 也成立（不参与「太热」判定）")
    ok(not rules.item_temp_ok(hood, 25.0, [t, hood]),
       "卫衣是外层 → 25℃ > 上限 20 → 太热，拦住")

    # 单调性：温度越低，越不可能"太热"。
    # ⚠️ 注意两个方向的单调都是对的：**下限约束**（够不够暖）随温度降低是
    # 从"能"变"不能"（非增），**上限约束**（会不会太热）是从"不能"变"能"（非减）。
    # 所以这里只能断言"不来回跳"（序列单调），不能断言某个固定方向 ——
    # 第一版写死了方向，结果卫衣那条假失败。
    for it, path in ((t, [t, hood]), (hood, [t, hood])):
        seq = [rules.item_temp_ok(it, float(x), path) for x in range(40, -11, -1)]
        mono = seq == sorted(seq) or seq == sorted(seq, reverse=True)
        ok(mono, f"{it['slot']} 的「能不能穿」随温度单调（不来回跳）")


# ══════════════════════════════════════════════
# ③ 单调性：合法温度必须是连续区间
# ══════════════════════════════════════════════

def test_contiguous_range() -> None:
    """⚠️ 这一条是这个文件里最值钱的。

    `item_temp_ok` 是复合条件（够暖 + 不太热 + 下半身区间），
    第一版把"体感"拿去比被盖住那件的上限，结果出现
    **8℃ 三层不合法、5℃ 反倒合法** —— 合法温度集不是区间，是个带洞的集合。
    用户能感觉到的就是"差一度就差这么多"。
    """
    head("③ 固定一套衣服，合法的温度必须是**连续区间**（不是带洞的集合）")

    closet = graph.Closet([
        mk(1, "L1", 1, -10, 38), mk(2, "L1", 2, -10, 38),
        mk(11, "L2", 4, 5, 20), mk(12, "L2", 3, 10, 25),
        mk(21, "L3", 5, 0, 22), mk(22, "L3", 4, 5, 20),
        mk(31, "L4", 0, 5, 28), mk(32, "L4", 0, 0, 32),
        mk(41, "L5", 0, 5, 30), mk(42, "L5", 0, 5, 30),
    ])

    combos = [
        [1, 31, 41], [1, 11, 31, 41], [1, 11, 21, 31, 41],
        [1, 12, 31, 41], [2, 11, 22, 32, 42], [2, 21, 32, 41],
    ]
    by_id = {it["id"]: it for it in closet.items}
    for ids in combos:
        path = [by_id[i] for i in ids]
        name = "+".join(x["slot"] for x in path)
        good = [x for x in range(-10, 41)
                if rules.outfit_ok(path, float(x)) is None]
        if not good:
            ok(False, f"{name}：一个温度都穿不了（那多半是数据问题，不是模型问题）")
            continue
        lo, hi = min(good), max(good)
        holes = sorted(set(range(lo, hi + 1)) - set(good))
        ok(not holes,
           f"{name}：合法区间 {lo}~{hi}℃ 连续"
           f"{'（空洞 ' + str(holes) + ' ← 「差一度」的来源）' if holes else ''}")


# ══════════════════════════════════════════════
# ④ 下半身只参与搭配
# ══════════════════════════════════════════════

def test_bottom_half() -> None:
    head("④ 下半身只参与搭配，不参与保暖加总（用户口径）")

    t = mk(1, "L1", 1, -10, 38)
    pants = mk(2, "L4", 0, 5, 28)
    pants_warm = mk(3, "L4", 99, 5, 28)      # 就算给它填 99℃ 也不该算数
    shoe = mk(4, "L5", 0, 5, 30)
    qiuku = mk(5, "L4I", 0, -10, 14)

    ok(rules.warmth(pants) == 0.0, "下装的保暖值恒为 0（不参与加总）")
    ok(rules.warmth(shoe) == 0.0, "鞋同理")
    ok(rules.warmth(qiuku) == 0.0, "秋裤同理")
    ok(rules.stack_warmth([t, pants_warm, shoe]) == rules.stack_warmth([t]),
       "给裤子填再大的保暖值，也不改变「这套够不够暖」")
    ok(rules.stack_warmth([t]) == rules.stack_warmth([t, pants, shoe]),
       "加裤子加鞋不改变保暖总量")

    # 但它们**各自**的温度区间仍然双边成立（防夏天棉裤 / 冬天凉鞋）
    ok(not rules.item_temp_ok(pants, 40.0, [pants]), "下装仍按自己的区间判：40℃ 超出上限")
    ok(not rules.item_temp_ok(shoe, 0.0, [shoe]), "鞋同理：0℃ 低于下限")
    ok(rules.item_temp_ok(pants, 20.0, [pants]), "20℃ 的裤子当然能穿")
    ok(rules.item_temp_ok(qiuku, -30.0, [qiuku]),
       "L4I 的下限**从不**参与（它永远被外裤盖住）")


# ══════════════════════════════════════════════
# ⑤ 舒适线可调，且改了会真的变
# ══════════════════════════════════════════════

def test_comfort_is_live() -> None:
    head("⑤ 舒适线：默认值、可调、改了真的会变结果")

    # ⚠️ 这里原来写的是 `prefs.load()["comfort"] == DEFAULT_PREFS["comfort"]` ——
    # 它**偷偷假设了 `data/prefs.json` 不存在**。2026-09-27 用户在 /settings 里
    # 把舒适线从 22℃ 调成了 19℃，这条断言当场变红，而**代码毫无问题**。
    # 属于"测试依赖了环境状态"，和 test_lan 那条横幅断言同一类毛病。
    # 正解：要验「默认值」就必须**换一个不存在的配置路径**，不能去读用户的真实配置。
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        real = prefs.CONFIG_PATH
        prefs.CONFIG_PATH = Path(td) / "prefs.json"
        try:
            ok(prefs.load()["comfort"] == prefs.DEFAULT_PREFS["comfort"],
               f"没有配置文件时是默认值 {prefs.DEFAULT_PREFS['comfort']:g}℃")
        finally:
            prefs.CONFIG_PATH = real

    # 用户真实的配置也要能读出来、且在合法区间内 —— 这条才是"当前环境"的体检
    cur = prefs.comfort()
    ok(prefs.COMFORT_MIN <= cur <= prefs.COMFORT_MAX,
       f"当前环境的舒适线读得出来且在区间内（{cur:g}℃）")

    ok(prefs.COMFORT_FOLK == 26.0, "法则原文那档 26℃ 仍然对外可见（界面要标它）")

    items = [mk(1, "L1", 1, -10, 38), mk(2, "L2", 4, 5, 20),
             mk(31, "L4", 0, 5, 28), mk(41, "L5", 0, 5, 30)]

    def serve_days(comfort: float) -> int:
        graph._CORE_CACHE.clear()
        c = graph.Closet(items, comfort=comfort)
        return sum(1 for x in range(-10, 41) if c.count_cores(float(x)) > 0)

    warm_room = serve_days(20.0)
    picky = serve_days(26.0)
    print(f"     舒适线 20℃ → 能穿 {warm_room} 个温度；26℃ → {picky} 个")
    ok(warm_room > picky,
       "舒适线越高，能穿的温度越少（怕冷的人要求更高）")

    # ⚠️ 缓存键必须含舒适线，否则改了设置会一路返回旧结果
    graph._CORE_CACHE.clear()
    a = graph.Closet(items, comfort=20.0).count_cores(16.0)
    b = graph.Closet(items, comfort=26.0).count_cores(16.0)
    ok(a != b, f"同一个温度、不同舒适线给出的核心数不同（{a} vs {b}）—— 缓存没串味")


# ══════════════════════════════════════════════
# ⑥ 落库 / 回填
# ══════════════════════════════════════════════

def test_persisted() -> None:
    head("⑥ 保暖值落库：列在、品类默认值齐、老数据被回填")

    conn = db.get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        ok("warmth" in cols, "items 表有 warmth 列（少一次 ALTER 就永远读不到）")

        from app.main import ITEM_FIELDS
        ok("warmth" in ITEM_FIELDS,
           "warmth 在 Pydantic 模型里声明了（否则 _insert 会静默丢掉它）")

        ok("warmth" in graph._FP_FIELDS,
           "warmth 在 _FP_FIELDS 里（否则改了保暖值搭配不变 —— 老坑）")

        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        missing = conn.execute(
            "SELECT COUNT(*) FROM items WHERE warmth IS NULL").fetchone()[0]
        ok(total == 0 or missing == 0,
           f"在库 {total} 件都有保暖值（缺 {missing} 件）—— 老库靠启动时回填")

        # ⚠️ 这里原来断言「库里每一件的 warmth 都等于品类默认值」—— **写错了**。
        #
        # `_backfill_warmth` 的口径是 `WHERE warmth IS NULL`：只补空的，
        # **不覆盖用户改过的**（见 db.py 里那个函数的 docstring）。也就是说
        # 「某件 ≠ 品类默认值」本来就是**合法状态**，那正是"用户去「改字段」里
        # 把保暖值调过了"的样子。断言"全部相等"等于把用户的权利当成异常：
        # 用户一改那件三合一冲锋衣（夹克默认 3 → 1），这个自检就红，
        # 而代码和数据都没错。2026-09-27 真的红了一次，排查了半天才发现是断言的问题。
        #
        # 换成两条更准确、同时也**更强**的断言：
        #   ① 真回填测试（以前根本没人钉过"只补 NULL、不覆盖"这个语义）
        #   ② 不一致的只能是**个别件**（大面积不一致 = 回填压根没跑）
        diff = []
        for r in conn.execute("SELECT id, name, category, warmth FROM items"):
            want = catalog.CATEGORY_DEFAULTS.get(r["category"], {}).get("warmth")
            if want is not None and r["warmth"] != want:
                diff.append((r["name"], r["warmth"], want))
        ok(len(diff) <= 3,
           f"与品类默认值不同的只有个别件（= 用户手动调过的），实际 {len(diff)} 件："
           f"{diff or '无'}")

        # ① 回填语义 = **只补 NULL，不覆盖已有值**。
        # 全程在同一个事务里，最后 ROLLBACK —— 绝不许往真实衣橱里留探针件。
        conn.rollback()                      # 先确保不残留在别的事务里
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO items (name, category, slot, stage, status, warmth) "
                "VALUES ('__backfill_probe__', '夹克', 'L3', 'owned', 'in_wardrobe', NULL)")
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            db._backfill_warmth(conn)
            got = conn.execute("SELECT warmth FROM items WHERE id = ?", (rid,)).fetchone()[0]
            ok(got == catalog.CATEGORY_DEFAULTS["夹克"]["warmth"],
               f"NULL 的保暖值会被回填成品类默认值（夹克 NULL → {got}）")

            conn.execute("UPDATE items SET warmth = 1 WHERE id = ?", (rid,))
            db._backfill_warmth(conn)
            keep = conn.execute("SELECT warmth FROM items WHERE id = ?", (rid,)).fetchone()[0]
            ok(keep == 1,
               "回填**不覆盖**已有值（用户改过的不许被冲掉）—— 这正是那件夹克的处境")
        finally:
            conn.execute("ROLLBACK")
            gone = conn.execute(
                "SELECT COUNT(*) FROM items WHERE name = '__backfill_probe__'").fetchone()[0]
            ok(gone == 0, "探针件已回滚，真实衣橱里一件都没多")
    finally:
        conn.close()

    tops = [c for s, cs in catalog.CATEGORIES.items()
            if s in rules.TOP_SLOTS for c in cs]
    missing_default = [c for c in tops if "warmth" not in catalog.CATEGORY_DEFAULTS.get(c, {})]
    ok(not missing_default,
       f"每个上装品类都有默认保暖值（缺：{missing_default or '无'}）")
    ok(catalog.CATEGORY_DEFAULTS["卫衣"]["warmth"] == 4,
       "卫衣 = 4℃（法则里「厚羊毛衫」那一档）")
    ok(catalog.CATEGORY_DEFAULTS["羽绒服"]["warmth"] == 9, "羽绒服 = 9℃（法则原文）")


# ══════════════════════════════════════════════
# ⑦ 真实衣橱：空态必须说得出原因
# ══════════════════════════════════════════════

def test_real_closet_gap() -> None:
    head("⑦ 真实衣橱：不够暖的时候要说得清（不是「算错了」）")

    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()
    if len(items) < 6:
        print("     单品太少，跳过")
        return

    closet = graph.Closet(items, comfort=prefs.comfort())
    best = closet.max_top_warmth()
    limit = rules.comfort_of(closet.comfort) - best
    print(f"     上装叠满的保暖值 {best:g}℃，舒适线 {rules.comfort_of(closet.comfort):g}℃ "
          f"→ 这套衣橱只能穿到 {limit:g}℃")

    d = closet.diagnose(limit - 5)
    ok(d["kind"] == "too_cold",
       f"{limit - 5:g}℃ 的诊断是 too_cold（说实话「缺一件外套」），实际 {d['kind']}")
    ok(str(best) in d["hint"] or f"{best:g}" in d["hint"],
       f"提示里带上了「最多能垫多少度」：{d['hint'][:56]}…")

    # 边界两侧：limit 那天有得穿，limit−1 没有
    ok(closet.count_cores(limit) > 0, f"{limit:g}℃ 有得穿")
    ok(closet.count_cores(limit - 1) == 0, f"{limit - 1:g}℃ 没得穿（正好卡在边界上）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把输出写到文件（避免 PowerShell 中文编码问题）")
    args = ap.parse_args()

    lines: list[str] = []

    class Tee:
        def write(self, s: str) -> None:
            sys.__stdout__.write(s)
            lines.append(s)

        def flush(self) -> None:
            sys.__stdout__.flush()

    old = sys.stdout
    sys.stdout = Tee()  # type: ignore[assignment]
    try:
        db.init_db()
        print("「26℃穿衣法则」自检")
        test_formula()
        test_too_hot()
        test_contiguous_range()
        test_bottom_half()
        test_comfort_is_live()
        test_persisted()
        test_real_closet_gap()
        print()
        print("=" * 60)
        if FAILS:
            print(f"✗ {len(FAILS)} 项没过：")
            for f in FAILS:
                print(f"   - {f}")
        else:
            print("✓ 全部通过")
        print("=" * 60)
    finally:
        sys.stdout = old

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
