# -*- coding: utf-8 -*-
"""套装（app/sets.py）自检 —— 全程离线，不联网、不碰真实库。

**为什么在内存库里跑**：套装的增删改是真写库的操作，而 `save_set` /
`/api/sets/{id}/use` 会连带写 `wear_log` 和 `items.wear_count`。
在真实库上测就是拿用户的穿着记录当试验田 —— 这个项目已经因为
"测试污染真实数据"栽过（apitest 清空过搭配历史、往表里留过假 id）。
所以：开一个内存库 + 跑一遍 schema.sql + 自己塞几件假单品，
把存储和判定都测干净，真实库一行都不动。

判定逻辑本身（撞色 / 缺层 / 温度撞不上）是纯函数，不需要库，直接测。

跑法：python scripts/test_sets.py --out test_sets.out.txt
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import sets  # noqa: E402
from app.engine import graph, rules  # noqa: E402

L: list[str] = []
FAILS = [0]


def say(s: str = "") -> None:
    L.append(s)


def ok(cond: bool, msg: str) -> bool:
    L.append(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS[0] += 1
    return bool(cond)


def head(t: str) -> None:
    say()
    say("─" * 52)
    say(t)


# ══════════════════════════════════════════════
# 内存库脚手架
# ══════════════════════════════════════════════

def memdb() -> sqlite3.Connection:
    # 至于为什么用裸 connect 而不用 db.get_conn()：后者固定连 data/closet.db，
    # 而这里要的是一个只活在内存里的空库。
    conn = sqlite3.connect(":memory:")  # fk-agnostic: 内存库，不碰真实库文件
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((ROOT / "app" / "schema.sql").read_text(encoding="utf-8"))
    return conn


def mk(iid: int, slot: str, name: str = "", **kw) -> dict:
    """造一件假单品。

    字段不能瞎造：判定是直接问引擎的（`Closet.count_paths` → `rules`），
    它真的会读这些字段。`is_neutral` 绕过 R4 撞色，这样测的就是
    **套装机制本身**，不掺衣橱数据的随机性。

    ⚠️ **2026-09-23「26℃穿衣法则」之后，"温度给满"这条老规矩不成立了。**
    以前层数由 `layer_plan` 的全局温度带决定，所以给满区间（-10~40）反而
    能隔离出"套装机制"；现在**每件自己的区间和保暖值都参与判定** ——
    再给满就等于造了一批"40℃ 还能穿夹克"的假衣服，测出来的结论全是假的。
    所以默认值改成**照 `catalog` 的品类默认值来**（T恤 15~30、卫衣 5~20、夹克 0~22…），
    要覆盖就在 `**kw` 里显式写。保暖值同理。

    ⚠️ **`**kw` 必须真的应用下去。** 第一版忘了 `it.update(kw)`，于是
    传进去的 `is_neutral=0` / `color_hex="#FF0000"` 全被静默丢掉，
    所有假单品都成了中性灰 —— "撞色不能存"那几条断言于是失败，
    看上去像产品 bug，其实是脚手架自己没生效。
    """
    # 槽位 → (温度区间, 厚度, 保暖值)，数值对齐 catalog.CATEGORY_DEFAULTS
    shape = {
        "L1": ((-10, 38), 2, 1),      # 内搭不设温度门槛（体感由外层决定）
        # ⚠️ 厚度取 3 不是 4：D1 要求"内薄外厚"，卫衣 4 + 夹克 3 会被判
        #    "厚度倒挂"（真实库里的卫衣也是厚 3 —— 用户自己改过）。
        "L2": ((5, 20), 3, 4),        # 卫衣
        "L3": ((0, 22), 3, 3),        # 夹克
        "L4I": ((-10, 14), 4, 0),     # 秋裤：被外裤盖住，不参与保暖加总
        "L4": ((5, 28), 3, 0),
        "L5": ((5, 30), 3, 0),
    }
    (tmin, tmax), thick, warm = shape.get(slot, ((-10, 40), 3, 0))
    it = {
        "id": iid, "name": name or f"{slot}-{iid}", "slot": slot,
        "category": "T恤" if slot == "L1" else "其他",
        "stage": "owned", "status": "in_wardrobe",
        "temp_min": tmin, "temp_max": tmax, "formality": 2, "thickness": thick,
        "warmth": warm,
        "is_neutral": 1, "color_main": "黑", "color_hex": "#333333",
        "created_at": "2026-09-21", "updated_at": "2026-09-21",
    }
    it.update(kw)
    return it


ITEMS = [
    mk(1, "L1", "白T"),
    mk(2, "L1", "灰T"),
    mk(3, "L4", "牛仔裤"),
    mk(4, "L5", "运动鞋"),
    mk(5, "L2", "卫衣"),
    mk(6, "L3", "夹克"),
    mk(7, "L1", "红T", is_neutral=0, color_main="红", color_hex="#FF0000"),
    mk(8, "L4", "绿裤", is_neutral=0, color_main="绿", color_hex="#00FF00"),
]


def seed(conn) -> None:
    for it in ITEMS:
        cols = ", ".join(it)
        qs = ", ".join("?" * len(it))
        conn.execute(f"INSERT INTO items ({cols}) VALUES ({qs})", list(it.values()))
    conn.commit()


def by_id(iid: int) -> dict:
    return next(it for it in ITEMS if it["id"] == iid)


def pick(*iids: int) -> list[dict]:
    return [by_id(i) for i in iids]


# ══════════════════════════════════════════════
# ① 判定
# ══════════════════════════════════════════════

def test_evaluate() -> None:
    head("① 判定：这堆衣服是不是一套能穿的")
    t, p, s = mk(1, "L1", "白T"), mk(3, "L4", "牛仔裤"), mk(4, "L5", "运动鞋")

    ev = sets.evaluate([t, p, s], "通勤")
    say(f"     齐整的三件 → ok={ev['ok']} {ev['temp_min']}~{ev['temp_max']}℃")
    ok(ev["ok"], "内搭+下装+鞋 判为能穿")
    # ⚠️ 下限**不能写死**：它是「26℃穿衣法则」算出来的 ——
    # 舒适线 − 这套上装的保暖值。写死一个数字，改了舒适线或保暖值就假通过。
    want_lo = int(rules.comfort_of() - rules.stack_warmth([t, p, s]) + 0.999)
    ok(ev["temp_min"] == want_lo,
       f"下限 = 舒适线 {rules.comfort_of():g} − 上装保暖 "
       f"{rules.stack_warmth([t, p, s]):g} → {want_lo}℃（这套没外套，再冷就得加）"
       f"，实际 {ev['temp_min']}")
    want_hi = min(rules.temp_max(x) for x in (t, p, s))
    ok(ev["temp_max"] == want_hi,
       f"上限 = 这几件里最低的那个上限 {want_hi}℃（裤子 28 / 鞋 30）实际 {ev['temp_max']}")

    ev = sets.evaluate([t, p], "通勤")
    ok(not ev["ok"], "缺鞋 → 不能穿")
    ok("鞋" in ev["reason"], f"而且说得出缺的是鞋：{ev['reason']!r}")

    ev = sets.evaluate([mk(1, "L1", "红T", is_neutral=0, color_hex="#FF0000"),
                        mk(3, "L4", "绿裤", is_neutral=0, color_hex="#00FF00"),
                        mk(4, "L5")], "通勤")
    ok(not ev["ok"], "红配绿 → 不能穿")
    ok("撞色" in ev["reason"], f"而且指名道姓说是撞色：{ev['reason']!r}")

    ev = sets.evaluate([t, mk(2, "L1", "灰T"), p, s], "通勤")
    ok(not ev["ok"] and "槽位" in ev["reason"],
       f"两件内搭 → 拦下并说明白：{ev['reason']!r}")

    ok(not sets.evaluate([t], None)["ok"], "一件不算一套")
    ok(not sets.evaluate([], None)["ok"], "空的不算一套")

    ev = sets.evaluate([mk(1, "L1", "夏T", temp_min=25, temp_max=35),
                        mk(3, "L4", "冬裤", temp_min=-5, temp_max=5),
                        mk(4, "L5")], None)
    ok(not ev["ok"] and "温度" in ev["reason"],
       f"冷暖差一个季节 → 说清是温度对不上：{ev['reason']!r}")

    say()
    say("· 单点判定 can_wear（今日页每次刷新都问一遍）")
    # ⚠️ 单点温度不能写死 20℃：这套（只有内搭、没外套）的下限是
    # 「26℃法则」算出来的 21℃ —— 20℃ 按新口径**确实穿不了**。
    lo = int(rules.comfort_of() - rules.stack_warmth([t, p, s]) + 0.999)
    ok(sets.can_wear([t, p, s], float(lo)), f"{lo}℃（刚好够暖）能穿")
    ok(not sets.can_wear([t, p, s], float(lo) - 1), f"{lo - 1}℃ 不够暖 → 穿不了")
    ok(not sets.can_wear([t, p, s], 45.0), "45℃ 穿不了")
    ok(not sets.can_wear([t, p], float(lo)), "缺件时不能穿")
    # 同一槽位重复 → 不能穿，且不能抛异常
    ok(not sets.can_wear([t, mk(2, "L1"), p, s], float(lo)), "同槽位重复 → 直接 False，不炸")


# ══════════════════════════════════════════════
# ② 存储（内存库）
# ══════════════════════════════════════════════

def test_store() -> None:
    head("② 存 / 改 / 删（内存库，真实库一行不动）")
    conn = memdb()
    seed(conn)

    row = sets.save_set(conn, "通勤三件", [1, 3, 4], "通勤", "测试用")
    say(f"     存了一套 → id={row['id']} name={row['name']!r} 件数={row['n_items']}")
    ok(row["n_items"] == 3, "三件都存进去了")
    ok([it["id"] for it in row["items"]] == [1, 3, 4],
       f"单品齐全：{[it['id'] for it in row['items']]}")

    lst = sets.list_sets(conn)
    ok(len(lst) == 1, f"列出来 1 套（实际 {len(lst)}）")

    # 存一套穿不了的，必须被拦
    try:
        sets.save_set(conn, "残套", [1], None)
        ok(False, "只给一件竟然存进去了")
    except ValueError as e:
        ok("至少" in str(e), f"只给一件被拦：{e}")

    try:
        sets.save_set(conn, "撞色套", [7, 8, 4], None)
        ok(False, "撞色的竟然存进去了")
    except ValueError as e:
        ok("撞色" in str(e), f"撞色被拦：{e}")

    # 取不到的 id 被丢掉并报告
    got, missing = sets.pick_items(conn, [1, 3, 9999])
    ok([it["id"] for it in got] == [1, 3] and missing == [9999],
       f"不存在的 id 被丢掉并报告：missing={missing}")

    say()
    say("· 改：只传名字，单品必须原样留着")
    row2 = sets.save_set(conn, "改个名", set_id=row["id"])
    ok(row2["name"] == "改个名", f"名字改了：{row2['name']!r}")
    ok(row2["n_items"] == 3, f"**单品没被清掉**（{row2['n_items']} 件）")
    ok(row2["occasion"] == "通勤", f"场合没被冲掉：{row2['occasion']!r}")
    ok(row2["note"] == "测试用", f"备注没被冲掉：{row2['note']!r}")

    say("· 改：传了 item_ids 才替换")
    row3 = sets.save_set(conn, None, [1, 3, 4, 5], set_id=row["id"])
    ok(row3["n_items"] == 4, f"换成 4 件：{row3['n_items']}")

    say("· 改：清空场合要显式传 None")
    row4 = sets.save_set(conn, None, None, None, sets.UNSET, set_id=row["id"])
    ok(row4["occasion"] is None, f"场合清掉了：{row4['occasion']!r}")

    say("· 件数不够时不许改坏")
    try:
        sets.save_set(conn, None, [1], set_id=row["id"])
        ok(False, "改成一件竟然成功了")
    except ValueError as e:
        ok("至少" in str(e), f"改成一件被拦：{e}")
    ok(sets.get_set(conn, row["id"])["n_items"] == 4, "被拦之后原来的 4 件还在")

    say()
    say("· 删")
    res = sets.delete_set(conn, row["id"])
    ok(res.get("ok") and not sets.list_sets(conn), "删干净了")
    ok(conn.execute("SELECT COUNT(*) FROM set_items").fetchone()[0] == 0,
       "set_items 没有残留（外键级联）")
    ok(sets.delete_set(conn, row["id"]) == {"ok": False, "reason": "not_found"},
       "重复删返回 not_found，不抛异常")
    ok(sets.mark_used(conn, 12345) is None, "mark_used 一个不存在的 id 也不炸")

    say()
    say("· 单品被删之后，套装不能变成「幽灵」")
    conn2 = memdb()
    seed(conn2)
    r = sets.save_set(conn2, "会被拆的套", [1, 3, 4], None)
    conn2.execute("DELETE FROM items WHERE id = 4")   # 模拟用户删了那双鞋
    conn2.commit()
    st = sets.all_with_state(conn2, 20.0, None)
    one = next(x for x in st if x["id"] == r["id"])
    say(f"     删掉鞋之后：ok={one['eval']['ok']} usable={one['usable']} "
        f"reason={one['eval']['reason']!r}")
    ok(not one["eval"]["ok"] and not one["usable"], "不再算能穿")
    ok("鞋" in one["eval"]["reason"], f"说得出缺的是鞋：{one['eval']['reason']!r}")

    # 只剩一件（低于存量下限）→ 这时候要说清是"被删了"，
    # 因为"至少 2 件才算一套"对一个已经存下来的套装等于没说
    conn2.execute("DELETE FROM items WHERE id = 3")
    conn2.commit()
    one2 = next(x for x in sets.all_with_state(conn2, 20.0, None) if x["id"] == r["id"])
    say(f"     再删掉裤子（只剩 1 件）：reason={one2['eval']['reason']!r}")
    ok("删" in one2["eval"]["reason"],
       f"告诉用户是被删了，不是含糊的『不合法』：{one2['eval']['reason']!r}")
    conn2.close()
    conn.close()


# ══════════════════════════════════════════════
# ③ 今日页会挑出哪些
# ══════════════════════════════════════════════

def test_usable() -> None:
    head("③ 今日页端出来的那批")
    conn = memdb()
    seed(conn)
    a = sets.save_set(conn, "夏天那套", [1, 3, 4], None)
    b = sets.save_set(conn, "冬天那套", [1, 3, 4, 5, 6], None)
    say(f"     两套：夏天(id={a['id']}) / 冬天(id={b['id']})")

    hot = [x["id"] for x in sets.usable_at(conn, 28.0, None)]
    cold = [x["id"] for x in sets.usable_at(conn, 15.0, None)]
    say(f"     28℃ → {hot}；15℃ → {cold}")
    ok(a["id"] in hot, "28℃ 端出薄的")
    ok(b["id"] not in hot, "28℃ 不端出带夹克的（夹克上限 22℃）")
    ok(b["id"] in cold, "15℃ 端出带夹克的")
    ok(a["id"] not in cold, "15℃ 不端出只有内搭的那套（26℃法则：差 6℃ 保暖）")
    ok(hot != cold, "温度不同，端出来的不是同一批")

    # 场合也参与判定（引擎的 outfit_ok 会查场合正式度）
    st = sets.all_with_state(conn, 28.0, None)
    ok(all(x["usable"] is False or x["eval"]["ok"] for x in st),
       "usable 不会在 ok=False 时是 True")

    say()
    say("· 缓存不能骗人：改了单品的属性，结论必须跟着变")
    sets.evaluate_cached(pick(1, 3, 4), None)
    before = sets.can_wear(pick(1, 3, 4), 28.0, None)
    conn.execute("UPDATE items SET temp_max = 10 WHERE id = 1")   # 白T 只能在 10℃ 以下穿
    conn.commit()
    sets_after = [it for it in sets.pick_items(conn, [1, 3, 4])[0]]
    after = sets.evaluate(sets_after, None)
    ok(before and not after["ok"],
       f"改了温度区间之后 28℃ 就不能穿了（此前 {before}，此后 {after['ok']}）")

    conn.close()


# ══════════════════════════════════════════════
# ④ 交叉检查
# ══════════════════════════════════════════════

def test_symbols() -> None:
    """main.py 里引用的东西是不是真的存在 —— 抓"改名没改调用方"。"""
    head("④ 交叉检查：main.py 引用的套装接口/符号真的在吗")
    import ast
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)

    routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute) and f.attr in
                    ("get", "post", "patch", "delete", "api_route")):
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant):
                routes.add(str(dec.args[0].value))
    for r in ("/api/sets", "/api/sets/evaluate", "/api/sets/{set_id}",
              "/api/sets/{set_id}/use", "/sets"):
        ok(r in routes, f"路由存在：{r}")

    # main.py 里对 sets.<name> 的引用
    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "sets"):
            used.add(node.attr)
    for name in sorted(used):
        ok(hasattr(sets, name), f"app.sets 有 {name}()")
    say(f"     main.py 用到 app.sets 的：{sorted(used)}")

    # save_set 的参数名不能漂 —— 调用方是按关键字传的
    import inspect
    sig = set(inspect.signature(sets.save_set).parameters)
    for k in ("name", "item_ids", "occasion", "note", "set_id"):
        ok(k in sig, f"save_set 接受 {k}")
    ok("UNSET" in dir(sets), "UNSET 哨兵对外可见（PATCH 要用）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    say("套装自检（离线，内存库）")
    test_evaluate()
    test_store()
    test_usable()
    test_symbols()

    say()
    say("═" * 52)
    if FAILS[0]:
        say(f"✗ {FAILS[0]} 项没过")
    else:
        say("✓ 全部通过")
    txt = "\n".join(L)
    if args.out:
        (ROOT / args.out).write_text(txt, encoding="utf-8")
    print(txt)
    return 1 if FAILS[0] else 0


if __name__ == "__main__":
    sys.exit(main())
