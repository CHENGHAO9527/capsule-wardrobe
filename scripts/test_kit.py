"""旅行模式（随身清单）自检：出门那几天只从"带出去的这几件"里挑。

用法：python scripts/test_kit.py

为什么单独一个文件：它和洗衣篮是**两个独立的"缩小"**，很容易互相污染 ——
  · 清单必须真的把"没带的"从候选里剔掉，否则首页还在推留在家里的大衣。
  · 但它**只影响今日那一屏**：计划页照旧按完整衣橱排（`plan.py` 里不该出现 kit）。
  · 它和洗衣篮一样，只能**取交集**用 —— 清单里被删掉的 id 不能让整屏炸掉。
  · 空态要自证：清单被筛空时说 `kit_empty`，不能说成"全洗了"。
  · 退出之后必须**逐套逐件**回到原样（不然这个开关就是不可逆的）。

覆盖：
  ① 模块层：幂等 / 坏 json 退 None / 坏值跳过 / end 由 start+n_days 推出 / temp_for
  ② 今日推荐：清单外的件一件都不出现，而且 n_items 仍是衣橱总数
  ③ 清单外的件**一件都不出现**；仍然排得出（清单覆盖了必需层）
  ④ **计划页不受影响**（`app/plan.py` 里压根没有 kit 这个词）
  ⑤ 残留 id：清单里的衣服被删掉 → 交集过滤，不炸
  ⑥ 空态 `kit_empty` + 退出后逐套逐件回到原样
  ⑦ 看"未来某天"时**不**受清单限制（那时未必还在旅行）
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

from app import db, kit, laundry, outfits, plan, prefs  # noqa: E402
from app.engine import graph  # noqa: E402

FAILS: list[str] = []
TEMP = 20.0
SEED = 42


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


def isolate_kit() -> Path:
    """把随身清单指到临时目录。

    ⚠️ **必须做**：`kit` 读写的是真实文件（`data/kit.json`）。不隔离的话，
    跑一次自检就会把用户正开着的旅行模式关掉 —— 而且不报错，只会让人某天
    发现"我明明在旅行模式里，怎么又推家里的大衣了"。（同 `test_wash`。）
    """
    tmp = Path(tempfile.mkdtemp(prefix="kit-"))
    kit.DATA_DIR = tmp
    kit.CONFIG_PATH = tmp / "kit.json"
    return kit.CONFIG_PATH


def isolate_laundry() -> Path:
    """洗衣篮也隔离：它是另一个"缩小"，用户真洗着衣服时会让结果不确定。"""
    tmp = Path(tempfile.mkdtemp(prefix="kit-wash-"))
    laundry.DATA_DIR = tmp
    laundry.CONFIG_PATH = tmp / "laundry.json"
    return laundry.CONFIG_PATH


def today(**kw) -> dict:
    from app.main import api_today
    kw.setdefault("temp", TEMP)
    kw.setdefault("save", False)      # 不落库
    kw.setdefault("use_llm", False)   # 不花钱
    kw.setdefault("seed", SEED)
    return api_today(**kw)


def ids_of(d: dict) -> list[list[int]]:
    return [[it["id"] for it in o["items"]] for o in (d.get("outfits") or [])]


def pick_kit_ids(items_all: list[dict]) -> list[int]:
    """挑一份"排得出来"的最小清单：每个必需层各一件。"""
    need = ("L1", "L4", "L5")
    out = []
    for slot in need:
        got = next((it["id"] for it in items_all if it["slot"] == slot), None)
        if got is not None:
            out.append(int(got))
    return out


# ══════════════════════════════════════════════
# ① 模块层
# ══════════════════════════════════════════════

def test_module() -> None:
    head("① 模块层：幂等 / 坏文件 / 坏值 / end 推导")

    p = kit.CONFIG_PATH
    kit.clear()
    ok(kit.load() is None, "退出之后 load() 是 None（不是空壳 dict）")

    kit.set_kit("京都 › 大阪", "2026-10-16", 10, [3, 7, 7])
    k = kit.load()
    ok(k is not None and k["item_ids"] == [3, 7], "重复 id 去重后落盘")
    ok(k["end"] == "2026-10-25", "end = start + (n_days-1) 天（10-16 起 10 天 → 10-25）")
    ok(k["label"] == "京都 › 大阪", "标签原样保留")

    # 覆盖式：再开一趟就把上一趟顶掉（同一时刻只能有一个随身清单）
    kit.set_kit("巴黎", "2026-11-01", 3, [9])
    k2 = kit.load()
    ok(k2["label"] == "巴黎" and k2["item_ids"] == [9], "再开一趟覆盖旧的（不是追加）")
    ok(k2["end"] == "2026-11-03", "end 跟着新 start 重算")

    # temp_for：**显式**给日期，不在函数里偷读 today()
    kit.set_kit("X", "2026-11-01", 3, [9],
                temps=[{"date": "2026-11-01", "temp": 18.5}, {"date": "bad", "temp": "x"}])
    ok(kit.temp_for("2026-11-01") == 18.5, "temp_for 命中那天 → 18.5")
    ok(kit.temp_for("2026-11-09") is None, "不在行程里的日期 → None（调用方走天气）")
    ok(kit.temp_for(None) is None, "不给日期 → None（绝不偷读当天）")

    # 坏文件：任何失败都当作"没开"。这个值挂在"打开首页"这条路径上。
    p.write_text("这不是 json", encoding="utf-8")
    ok(kit.load() is None and kit.ids() == set(), "文件坏掉 → 当作没开（首页照常打开）")
    p.write_text('{"active": true, "item_ids": ["x", null, 7, {"a":1}]}', encoding="utf-8")
    ok(kit.ids() == {7}, "单个坏值跳过，好值留着")
    p.write_text('{"active": false, "item_ids": [1,2]}', encoding="utf-8")
    ok(kit.load() is None, "active=false → None（退出状态不残留）")
    p.write_text('{"item_ids": [1,2]}', encoding="utf-8")
    ok(kit.load() is None, "没有 active 字段 → None")

    kit.clear()


# ══════════════════════════════════════════════
# ② 今日推荐：清单外的件一件都不出现
# ══════════════════════════════════════════════

def test_today_restricted(items_all: list[dict]) -> None:
    head("② 今日推荐：只从清单里挑")

    kit.clear()
    laundry.clear()
    d0 = today()
    ok(bool(d0.get("outfits")), f"开旅行模式前排出 {len(d0.get('outfits') or [])} 套")
    ok(d0.get("kit") is None, "没开时 `kit` 字段是 None（前端据此不显示横幅）")

    kids = pick_kit_ids(items_all)
    ok(len(kids) >= 2, f"挑了 {len(kids)} 件做清单（覆盖必需层，排得出来）")
    kit.set_kit("测试行程", "2026-10-16", 5, kids)

    d1 = today()
    ok(d1.get("kit") is not None and d1["kit"]["n_items"] == len(kids),
       f"响应里带上了 `kit`（{len(kids)} 件）—— 横幅就是读它")
    used = {it["id"] for o in (d1.get("outfits") or []) for it in o["items"]}
    bad = sorted(used - set(kids))
    ok(not bad, "返回的每一套**只用清单里的件**" + (f" ✗ 漏了：{bad}" if bad else ""))
    ok(d1["n_items"] == len(items_all),
       f"`n_items` 仍是衣橱总数 {len(items_all)}（不是缩小后的数 —— 否则像衣服丢了）")
    ok(bool(d1.get("outfits")),
       f"而且照样排得出来（{len(d1.get('outfits') or [])} 套）—— 清单覆盖了必需层")

    kit.clear()


# ══════════════════════════════════════════════
# ③ 计划页不受影响 —— 这是这个功能的边界
# ══════════════════════════════════════════════

def test_plan_unaffected(conn, items_all: list[dict]) -> None:
    head("③ 计划页不受影响（一周 / 旅行照旧按完整衣橱排）")

    # 结构性的保证：`app/plan.py` 里压根不该出现 kit 这个词。
    # 比"跑一遍看着一样"更硬 —— 拦得住以后有人图省事顺手加进去。
    src = (ROOT / "app" / "plan.py").read_text(encoding="utf-8")
    ok("kit." not in src and "import kit" not in src,
       "`app/plan.py` 里没有引用 kit（旅行清单不读随身清单）")

    days = [{"date": f"2026-10-{10 + i:02d}", "temp": t, "tmin": t}
            for i, t in enumerate([20, 20, 19, 18, 20, 21, 20])]
    weights = outfits.load_weights(conn)
    comfort = prefs.comfort()

    kit.clear()
    p1 = plan.build_plan(graph.Closet(items_all, comfort=comfort), days,
                         occasion="通勤", weights=weights, mode="week",
                         seed=3, use_llm=False)

    kit.set_kit("测试行程", "2026-10-10", 7, pick_kit_ids(items_all))
    p2 = plan.build_plan(graph.Closet(items_all, comfort=comfort), days,
                         occasion="通勤", weights=weights, mode="week",
                         seed=3, use_llm=False)

    ok(p1["n_planned"] == p2["n_planned"],
       f"开着旅行模式，计划的实排天数一样（{p1['n_planned']} 天）")
    a = [[x["id"] for x in d["outfit"]["items"]] for d in p1["days"] if d.get("outfit")]
    b = [[x["id"] for x in d["outfit"]["items"]] for d in p2["days"] if d.get("outfit")]
    ok(a == b, f"开前开后七天排出的搭配**逐件一致**（{len(a)} 天，{sum(len(x) for x in a)} 件次）")

    kit.clear()


# ══════════════════════════════════════════════
# ④ 残留 id
# ══════════════════════════════════════════════

def test_orphan_id(items_all: list[dict]) -> None:
    head("④ 清单里的衣服被删了：交集过滤，不炸")

    kit.clear()
    kids = pick_kit_ids(items_all)
    kit.set_kit("测试行程", "2026-10-16", 5, kids + [999999])   # 这个 id 不存在

    d = today()
    ok(d.get("kit") is not None and d["kit"]["n_items"] == len(kids),
       f"幽灵 id 被交集挡掉（清单说 {len(kids) + 1} 件，实际算 {len(kids)} 件）")
    used = {it["id"] for o in (d.get("outfits") or []) for it in o["items"]}
    ok(999999 not in used, "它当然也不会出现在搭配里")
    ok(d["n_items"] == len(items_all), "衣橱总数不受影响")

    kit.clear()


# ══════════════════════════════════════════════
# ⑤ 空态 + 退出恢复
# ══════════════════════════════════════════════

def test_empty_and_restore(items_all: list[dict]) -> None:
    head("⑤ 清单被筛空 → 专门的空态；退出后逐套逐件回到原样")

    kit.clear()
    laundry.clear()
    base = ids_of(today())

    # 清单里全是**不存在**的 id（衣服后来被删了）→ 交集为空。
    # ⚠️ 这不是"全洗了"：衣橱是满的，是**这份清单**失效了 —— 两句话不一样。
    #    而且这一支必须**带上 kit**，否则用户找不到地方关掉它（被关在门外）。
    kit.set_kit("幽灵行程", "2026-10-16", 3, [999991, 999992])
    d = today()
    ok(d.get("empty") and d.get("reason") == "kit_empty",
       "清单里的件全没了 → `reason='kit_empty'`，**不是** all_washed"
       "（衣橱是满的，是清单失效了）")
    ok(d.get("kit") is not None,
       "空态也带着 `kit` —— 否则关不掉旅行模式，用户被关在门外")

    kit.clear()
    after = ids_of(today())
    ok(base == after, f"退出之后，同 seed 下逐套逐件一致（{len(base)} 套）")

    kit.clear()


# ══════════════════════════════════════════════
# ⑥ 看"未来某天"时不受清单限制
# ══════════════════════════════════════════════

def test_future_not_restricted(items_all: list[dict]) -> None:
    head("⑥ 看未来某天时不受清单限制（那时未必还在旅行）")

    kit.clear()
    kids = pick_kit_ids(items_all)
    kit.set_kit("测试行程", "2026-10-16", 5, kids)

    d_now = today()
    d_future = today(include_washed=True)

    ok(d_now.get("kit") is not None, "今天这一屏：清单生效（kit 非 None）")
    ok(d_future.get("kit") is None,
       "未来那一屏：`include_washed=True` → 清单不生效、也不显示横幅"
       "（状态和所见一致，不会让人以为'未来的搭配也被限制了'）")
    ok(d_future["cores"] >= d_now["cores"],
       f"未来那屏的组合数 ≥ 受限那屏（{d_future['cores']} ≥ {d_now['cores']}）"
       "—— 确实没被清单限制")

    kit.clear()


# ══════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把报告写到文件（PowerShell 抓不到子进程 stdout）")
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
    tmpdirs: list[Path] = []
    try:
        tmpdirs.append(isolate_kit().parent)
        tmpdirs.append(isolate_laundry().parent)
        db.init_db()
        conn = db.get_conn()
        try:
            items_all = db.load_items(conn, "owned")
            print("旅行模式（随身清单）自检")
            print(f"真实衣橱：{len(items_all)} 件")

            test_module()
            if len(items_all) >= 8:
                test_today_restricted(items_all)
                test_plan_unaffected(conn, items_all)
                test_orphan_id(items_all)
                test_empty_and_restore(items_all)
                test_future_not_restricted(items_all)
            else:
                say(f"⚠️ 只有 {len(items_all)} 件，跳过依赖真实衣橱的 ②~⑥"
                    f"（模块层没问题，是数据不够跑）")
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
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
