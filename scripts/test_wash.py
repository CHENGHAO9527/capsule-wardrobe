"""洗衣篮自检：洗了的件不进今日推荐，而且**只影响今日**。

用法：python scripts/test_wash.py

为什么单独一个文件：这个功能的**边界比功能本身更容易出错**。
  · 「洗了」必须真的把它从候选里剔掉。**不能拿 `avoid_ids` 凑合** ——
    那条路只是打分降权，罚完照样可能被选中，而洗了的衣服是物理上穿不了。
  · 但它**只能**影响今日推荐。计划页的一周 / 旅行回答的是
    "出门那几天这件在不在手边"，跟"今天这件干不干净"是两个问题。
  · 洗光了必须留有出口：全洗了 → 排不出来 → 首页上还得能点「恢复」。
    否则用户被自己的操作关在门外，又是一次静默失败。

覆盖：
  ① 模块层：幂等 / 坏 json 退空集 / 坏值跳过 / 排序落盘
  ② 今日推荐：洗了的件绝不出现（洗 1 件、洗一半）
  ③ 全洗了有专门空态，而且带着洗衣篮（还能恢复）
  ④ **计划页不受影响**（洗前洗后排出的清单逐件一致）
  ⑤ 删单品后残留 id 不炸，也不显示成幽灵衣服
  ⑥ 恢复之后回到跟没洗过一样
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, laundry, outfits, plan, prefs  # noqa: E402
from app.engine import graph  # noqa: E402

FAILS: list[str] = []

TEMP = 20.0        # 今日推荐都用这个温度扫
SEED = 42          # 固定 seed，否则每次枚举顺序不同，逐件比对没法做


def head(t: str) -> None:
    print()
    print("─" * 60)
    print(t)
    print("─" * 60)


def say(t: str = "") -> None:
    if t:
        print(f"  {t}")


def ok(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


def isolate_laundry() -> Path:
    """把洗衣篮指到临时目录。

    ⚠️ **必须做**：`laundry` 读写的是一个真实文件（`data/laundry.json`）。
    不隔离的话，跑一次自检就会把用户正在洗的衣服清空 —— 而这件事不会报错，
    只会让人某天发现"我明明洗了三件，怎么又都回来了"。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wash-"))
    laundry.DATA_DIR = tmp
    laundry.CONFIG_PATH = tmp / "laundry.json"
    return laundry.CONFIG_PATH


def today(**kw) -> dict:
    """走**真实的** `/api/today`（直接调函数，不开 HTTP）。

    `save=False` 不落库、`use_llm=False` 不请模型 —— 自检不该写用户数据、
    也不该花钱。除此之外跟浏览器拿到的是同一条路径。
    """
    from app.main import api_today
    kw.setdefault("temp", TEMP)
    kw.setdefault("save", False)
    kw.setdefault("use_llm", False)
    kw.setdefault("seed", SEED)
    return api_today(**kw)


def ids_of(d: dict) -> list[list[int]]:
    return [[it["id"] for it in o["items"]] for o in (d.get("outfits") or [])]


# ══════════════════════════════════════════════
# ① 模块层
# ══════════════════════════════════════════════

def test_module() -> None:
    head("① 模块层：幂等 / 坏文件 / 坏值")

    p = laundry.CONFIG_PATH
    laundry.clear()
    ok(laundry.ids() == set(), "起点是空集")

    laundry.wash(7)
    laundry.wash(7)
    ok(laundry.ids() == {7}, "同一件洗两次还是 {7}（幂等）")
    ok(json.loads(p.read_text(encoding="utf-8"))["ids"] == [7],
       "落盘是 `{ids: [...]}`（人眼能直接看，出问题能 diff）")

    laundry.wash(3)
    ok(json.loads(p.read_text(encoding="utf-8"))["ids"] == [3, 7],
       "落盘前排序（不是随机的 set 顺序）")

    laundry.restore(3)
    ok(laundry.ids() == {7}, "放回一件")
    laundry.restore(999)
    ok(laundry.ids() == {7}, "放回一件本来就不在篮子里的：幂等，不抛")

    # 坏文件：任何失败都退空集。这个值挂在"打开首页"这条路径上，
    # 一个被改坏的 json 不该让首页打不开。
    p.write_text("这不是 json", encoding="utf-8")
    ok(laundry.ids() == set(), "文件坏掉 → 空集（不抛异常，首页照常打开）")

    p.write_text('{"ids": ["x", null, 7, {"a": 1}]}', encoding="utf-8")
    ok(laundry.ids() == {7}, "单个坏值跳过，好值留着")

    p.write_text("[1, 2, 3]", encoding="utf-8")
    ok(laundry.ids() == set(), "顶层不是对象 → 空集")

    laundry.clear()


# ══════════════════════════════════════════════
# ② 今日推荐：洗了的件绝不出现
# ══════════════════════════════════════════════

def test_today_hides(items_all: list[dict]) -> None:
    head("② 今日推荐：洗了的件绝不出现")

    laundry.clear()
    d0 = today()
    ok(bool(d0.get("outfits")), f"洗之前排出了 {len(d0.get('outfits') or [])} 套")
    ok(d0["n_washed"] == 0, "洗衣篮是空的")
    ok(d0["n_items"] == len(items_all),
       f"`n_items` = 衣橱总件数 {len(items_all)}（**不是**剔除后的数 —— 否则看着像衣服丢了）")

    victim = d0["outfits"][0]["items"][0]
    laundry.wash(victim["id"])
    d1 = today()

    hit = [it for o in (d1.get("outfits") or []) for it in o["items"]
           if it["id"] == victim["id"]]
    ok(not hit, f"洗掉「{victim['name']}」之后，返回的每一套里都不含它")
    # ⚠️ 上面那条只有在"还排得出来"的时候才不是空真。洗一件就整屏空掉的话，
    #    它是白过的 —— 而"洗一两件"恰恰是最常发生的用法，必须走实。
    ok(bool(d1.get("outfits")),
       f"而且照样排得出来（{len(d1.get('outfits') or [])} 套，只是换了搭配）"
       "—— 洗一两件不该让整屏空掉")
    ok(d1["n_washed"] == 1, "洗衣篮 1 件")
    ok([w["name"] for w in d1["washed"]] == [victim["name"]],
       "洗衣篮带着名字 —— 否则用户不知道洗的是哪几件")
    ok(d1["n_items"] == len(items_all), "`n_items` 不变（衣服还在，只是今天不参与）")

    # 洗掉一半。关键不是"还剩几套"，而是**剩下的一套都不许含洗掉的件** ——
    # 用 avoid_ids 那种降权写法，这一步必然有四分之一左右的概率翻车。
    half = [it["id"] for it in items_all][:len(items_all) // 2]
    for i in half:
        laundry.wash(i)
    d2 = today()
    left = {it["id"] for o in (d2.get("outfits") or []) for it in o["items"]}
    bad = sorted(left & set(half))
    ok(not bad,
       f"洗掉一半（{len(half)} 件）后，残留的推荐里一件都不含它们"
       f"（这天还剩 {len(d2.get('outfits') or [])} 套，排不出来也是实话）"
       + (f" ✗ 踩雷：{bad[:3]}" if bad else ""))
    laundry.clear()


# ══════════════════════════════════════════════
# ③ 全洗了
# ══════════════════════════════════════════════

def test_all_washed(items_all: list[dict]) -> None:
    head("③ 全洗了：专门的空态 + 出口还在")

    laundry.clear()
    for it in items_all:
        laundry.wash(it["id"])
    d = today()

    ok(d.get("empty") and d.get("reason") == "all_washed",
       "全洗了 → `reason='all_washed'`，**不是** `no_items`"
       "（衣橱是满的，只是今天没得穿 —— 这两句话不一样）")
    ok(len(d.get("washed") or []) == len(items_all),
       f"空态也带着整份洗衣篮（{len(d.get('washed') or [])} 件）"
       "—— 没有它，用户就找不到地方点「恢复」")
    ok(d.get("n_items") == len(items_all), "`n_items` 仍是衣橱总件数")

    laundry.clear()
    d2 = today()
    ok(not d2.get("empty"), "全部恢复之后，又排得出来了")


# ══════════════════════════════════════════════
# ④ 计划页不受影响 —— 这是这个功能的边界
# ══════════════════════════════════════════════

def test_plan_unaffected(conn, items_all: list[dict]) -> None:
    head("④ 计划页不受影响（一周 / 旅行与洗衣篮无关）")

    # 结构性的保证：plan.py 里压根不该出现 laundry 这个词。
    # 这比"跑一遍看着一样"更硬 —— 它拦得住以后有人图省事顺手加进去。
    src = (ROOT / "app" / "plan.py").read_text(encoding="utf-8")
    ok("laundry" not in src,
       "`app/plan.py` 里压根没有 laundry 这个词（旅行清单不读洗衣篮）")

    days = [{"date": f"2026-10-{10 + i:02d}", "temp": t, "tmin": t}
            for i, t in enumerate([20, 20, 19, 18, 20, 21, 20])]
    weights = outfits.load_weights(conn)
    comfort = prefs.comfort()

    laundry.clear()
    p1 = plan.build_plan(graph.Closet(items_all, comfort=comfort), days,
                         occasion="通勤", weights=weights, mode="week",
                         seed=3, use_llm=False)

    washed = [it["id"] for it in items_all[:5]]
    for i in washed:
        laundry.wash(i)
    # 计划页拿到的**永远是全量衣橱**（这就是 main.py 里"只在这一条路径上剔"的意思）
    p2 = plan.build_plan(graph.Closet(items_all, comfort=comfort), days,
                         occasion="通勤", weights=weights, mode="week",
                         seed=3, use_llm=False)

    ok(p1["n_planned"] == p2["n_planned"],
       f"洗了 5 件，计划的实排天数一样（{p1['n_planned']} 天）")
    a = [[x["id"] for x in d["outfit"]["items"]] for d in p1["days"] if d.get("outfit")]
    b = [[x["id"] for x in d["outfit"]["items"]] for d in p2["days"] if d.get("outfit")]
    ok(a == b, f"洗前洗后七天排出的搭配**逐件一致**（{len(a)} 天，{sum(len(x) for x in a)} 件次）")

    laundry.clear()


# ══════════════════════════════════════════════
# ⑤ 残留 id
# ══════════════════════════════════════════════

def test_orphan_id(items_all: list[dict]) -> None:
    head("⑤ 删掉的单品残留在洗衣篮里，不能把首页炸了")

    laundry.clear()
    laundry.wash(999999)          # 这个 id 不存在（模拟：洗了之后又把那件删了）
    d = today()

    ok(not d.get("empty"), "推荐照常排得出来")
    ok(d["n_washed"] == 0,
       "那个不存在的 id 被**交集**过滤掉了（否则界面上会出现一件点不动的幽灵衣服）")
    ok(d["n_items"] == len(items_all), "件数也不受它影响")

    laundry.clear()


# ══════════════════════════════════════════════
# ⑥ 恢复
# ══════════════════════════════════════════════

def test_restore(items_all: list[dict]) -> None:
    head("⑥ 恢复之后回到跟没洗过一样")

    laundry.clear()
    a = ids_of(today())

    laundry.wash(items_all[0]["id"])
    laundry.wash(items_all[1]["id"])
    mid = ids_of(today())
    ok(mid != a, "中间态确实变了（不然这一节等于没测）")

    laundry.clear()
    b = ids_of(today())
    ok(a == b, f"全部恢复后，同 seed 下逐套逐件一致（{len(a)} 套）")

    laundry.clear()


# ══════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把报告写到文件"
                                  "（PowerShell 抓不到子进程 stdout，用它）")
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
        isolate_laundry()
        db.init_db()
        conn = db.get_conn()
        try:
            items_all = db.load_items(conn, "owned")
            print("洗衣篮自检")
            print(f"真实衣橱：{len(items_all)} 件")

            test_module()
            if len(items_all) >= 8:
                test_today_hides(items_all)
                test_all_washed(items_all)
                test_plan_unaffected(conn, items_all)
                test_orphan_id(items_all)
                test_restore(items_all)
            else:
                say(f"⚠️ 只有 {len(items_all)} 件，跳过依赖真实衣橱的 ②~⑥"
                    f"（洗衣篮本身没问题，是数据不够跑）")
        finally:
            conn.close()

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
        # 隔离目录整个删掉 —— `tempfile.mkdtemp()` 建的是**目录**，
        # 只删里面的 json 会在系统临时目录里留一地空壳（每跑一次留一个）。
        shutil.rmtree(laundry.DATA_DIR, ignore_errors=True)

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
