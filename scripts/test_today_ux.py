"""今日页这一轮的三件事：换一批真的换 / 围绕某件搭 / 体感校准。

用法：python scripts/test_today_ux.py

为什么三件事放一个文件：它们改的是**同一条链路**（`/api/today` 的入参 → 推荐 → 页头数字），
分开测很容易各自绿、合起来错（例如锚定之后 `cores` 忘了重算）。

覆盖：
  ① 换一批：**硬排除**已经看过的组合（不是扣分）→ 两批零重叠
  ② 换一批：池子小到排不出新的 → `exhausted=True`，而且**仍然返回一套**（不空屏）
  ③ 围绕某件：返回的每一套都含它；`cores` 按锚定后的池子**重算**
  ④ 围绕某件：这个温度穿不上它 → `anchor_empty` + `hint.kind='temp'`（不是"排不出来"）
  ⑤ 体感：偏冷 +0.5℃ / 偏热 -0.5℃，顶到上下限时 `moved=0`
  ⑥ 体感线索：同一套连点两次偏冷 → 那几件各记 2 次（只是线索，不改保暖值）

⚠️ 全程跑**临时副本**：库用 `real.backup()` 复制一份，`prefs` / `feel` 指到临时目录。
   不隔离的话，跑一次就把用户的舒适线推走了 —— 而且不报错。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, feel, outfits, prefs  # noqa: E402

FAILS: list[str] = []


def p(t: str = "") -> None:
    print(t)


def ok(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


def isolate() -> list[pathlib.Path]:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsule-ux-"))
    real = db.get_conn()
    copy = tmp / "closet.db"
    dst = sqlite3.connect(copy)  # fk-agnostic: 只是 backup API 的写入目标
    try:
        real.backup(dst)
        dst.commit()
    finally:
        dst.close()
        real.close()
    db.DB_PATH = copy
    db.DATA_DIR = tmp

    for mod, name in ((prefs, "prefs.json"), (feel, "feel.json")):
        d = pathlib.Path(tempfile.mkdtemp(prefix=f"capsule-{mod.__name__}-"))
        mod.DATA_DIR = d
        mod.CONFIG_PATH = d / name
        tmp_list.append(d)
    return tmp_list


tmp_list: list[pathlib.Path] = []


def today(**kw) -> dict:
    from app.main import api_today
    kw.setdefault("save", False)      # 不落库
    kw.setdefault("use_llm", False)   # 不花钱
    kw.setdefault("temp", 22)
    kw.setdefault("n", 3)
    return api_today(**kw)


def sets_of(d: dict) -> list[frozenset]:
    return [frozenset(i["id"] for i in o["items"]) for o in (d.get("outfits") or [])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
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
        if not db.DB_PATH.exists():
            ok(False, f"找不到真实库 {db.DB_PATH}")
            raise SystemExit(1)
        isolate()
        db.init_db()
        conn = db.get_conn()
        items = db.load_items(conn, "owned")
        p("今日页三件事自检")
        p(f"临时副本：{len(items)} 件")
        p()

        if len(items) < 8:
            ok(False, "衣橱少于 8 件，构造不出对照场景")
            raise SystemExit(1)

        # ── ① 换一批：硬排除 ────────────────────────────────
        p("① 换一批：两批**零重叠**（以前只是扣 40 分，池子小就等于没换）")
        p("─" * 60)
        a = today(seed=1)
        bars = "|".join("-".join(str(i) for i in sorted(s)) for s in sets_of(a))
        b = today(seed=2, exclude_sets=bars)
        A, B = set(sets_of(a)), set(sets_of(b))
        ok(bool(A) and bool(B), f"两批都排出了东西（{len(A)} / {len(B)} 套）")
        overlap = A & B
        ok(not overlap, f"两批没有一套重复（交集 {len(overlap)}）" + (f" ✗ {overlap}" if overlap else ""))
        ok(not b.get("exhausted"), "全衣橱不该被判成'排完了'")
        p()

        # ── ② 池子小 → exhausted ────────────────────────────
        p("② 池子小到排不出新的：明说'排完了'，但不空屏")
        p("─" * 60)
        from app import kit
        kd = pathlib.Path(tempfile.mkdtemp(prefix="capsule-kit-"))
        tmp_list.append(kd)
        kit.DATA_DIR = kd
        kit.CONFIG_PATH = kd / "kit.json"
        need = ("L1", "L4", "L5")
        small = [next(int(x["id"]) for x in items if x["slot"] == s_) for s_ in need]
        kit.set_kit("自检行程", "2026-10-17", 3, small)
        c1 = today(seed=1)
        bars2 = "|".join("-".join(str(i) for i in sorted(s)) for s in sets_of(c1))
        c2 = today(seed=2, exclude_sets=bars2)
        ok(len(sets_of(c1)) >= 1, f"3 件的清单能排出 {len(sets_of(c1))} 套")
        ok(bool(c2.get("exhausted")), "换一批 → `exhausted=True`（池子就这么大，这是事实）")
        ok(len(sets_of(c2)) >= 1, "而且**不空屏** —— 空屏会被当成功能坏了")
        kit.clear()
        p()

        # ── ③ 围绕某件 ─────────────────────────────────────
        p("③ 围绕某件搭：每一套都含它，且 cores 按锚定后的池子重算")
        p("─" * 60)
        base = today(seed=1)
        anchor_item = base["outfits"][0]["items"][0]
        aid = int(anchor_item["id"])
        an = today(seed=1, anchor=aid)
        ok(bool(an.get("outfits")), f"围绕「{anchor_item['name']}」排出 {len(an.get('outfits') or [])} 套")
        ok(all(any(int(i["id"]) == aid for i in o["items"]) for o in (an.get("outfits") or [])),
           "每一套都含这一件（硬约束，不是「尽量」）")
        ok(an.get("anchor", {}).get("id") == aid, "响应里带着 anchor（页头要显示围绕谁）")
        ok(an["cores"] < base["cores"],
           f"cores 重算过：锚定 {an['cores']} < 全量 {base['cores']} "
           f"（不重算的话页头会写一个根本排不出来的数）")
        p()

        # ── ④ 锚定冲突 ─────────────────────────────────────
        p("④ 这个温度穿不上它：说清是温度卡住，不是「排不出来」")
        p("─" * 60)
        warm = [x for x in items if (x.get("temp_max") or 99) <= 20]
        if not warm:
            ok(False, "没有上限 ≤20℃ 的单品，这条没法验（不许静默跳过）")
        else:
            w = warm[0]
            r = today(temp=26, anchor=int(w["id"]))
            ok(r.get("reason") == "anchor_empty",
               f"「{w['name']}」（上限 {w['temp_max']}℃）在 26℃ → `anchor_empty`")
            ok((r.get("anchor_hint") or {}).get("kind") == "temp",
               f"hint.kind=temp（不是含糊的「排不出来」）：{r.get('anchor_hint')}")
        p()

        # ── ⑤ 体感：动的是舒适线 ─────────────────────────────
        p("⑤ 体感校准：偏冷 +0.5℃ / 偏热 -0.5℃")
        p("─" * 60)
        saved = prefs.comfort()
        o = today(save=True, seed=7)["outfits"][0]      # 要 outfit_id
        r1 = feel.record(conn, "cold", o["outfit_id"], 22)
        ok(r1["comfort_after"] == saved + feel.STEP,
           f"偏冷一次：{r1['comfort_before']} → {r1['comfort_after']}")
        r2 = feel.record(conn, "hot", o["outfit_id"], 22)
        ok(r2["comfort_after"] == saved, f"偏热一次又退回来：{r2['comfort_before']} → {r2['comfort_after']}")

        prefs.save({"comfort": prefs.COMFORT_MAX})
        r3 = feel.record(conn, "cold", o["outfit_id"], 22)
        ok(r3["moved"] == 0, f"顶到上限（{prefs.COMFORT_MAX}℃）时 moved=0 —— 前端据此说'到头了'")
        prefs.save({"comfort": saved})
        p()

        # ── ⑥ 线索 ────────────────────────────────────────
        p("⑥ 体感线索：同一套连点两次，那几件各记 2 次（只展示，不改保暖值）")
        p("─" * 60)
        feel.CONFIG_PATH.unlink(missing_ok=True)
        for _ in range(2):
            feel.record(conn, "cold", o["outfit_id"], 22)
        flags = feel.garment_flags()
        wanted = {int(i["id"]) for i in o["items"]}
        ok(set(flags) == wanted, f"这一套的三件都被记上了（{len(flags)} 件）")
        ok(all(t["cold"] == 2 and t["hot"] == 0 for t in flags.values()),
           "每件 cold=2 / hot=0")
        warmth_before = {i["id"]: i.get("warmth") for i in db.load_items(conn, "owned")}
        from app.main import api_stats
        st = api_stats(temp=22, detail=True)
        ok(len(st.get("feel_flags") or []) == 3, "体检数据里列出了这 3 条线索")
        warmth_after = {i["id"]: i.get("warmth") for i in db.load_items(conn, "owned")}
        ok(warmth_before == warmth_after,
           "**保暖值一个都没被自动改** —— 单次体感分不清是舒适线还是单件估薄了")

        conn.close()
        p()
        p("=" * 60)
        if FAILS:
            p(f"✗ {len(FAILS)} 项没过：")
            for f in FAILS:
                p(f"   - {f}")
        else:
            p("✓ 全部通过")
        p("=" * 60)
    finally:
        sys.stdout = old
        for d in tmp_list:
            shutil.rmtree(d, ignore_errors=True)

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
