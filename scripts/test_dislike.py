"""「不喜欢」按件计的自检：只否这一件，不连坐同套的其他件。

用法：python scripts/test_dislike.py

为什么单独一个文件：
  · 这条的语义核心是**按件不是按套**（用户原话：「不喜欢是指单件衣服不是组合」）。
    一整套被否说明不了其中哪一件有问题；摊到三件头上，三件都白背一次黑锅，
    而且"这件该淘汰了"永远算不出来。
  · 它要写库（`w_item.dislike_n` / `beta`），所以**必须跑真实库的临时副本** ——
    绝不能在用户的库上验证"写进去对不对"。

覆盖：
  ① 连点三次：dislike_n 累计到 3，beta 每次 +1
  ② **同套的其他件一分不动**（这条才是"按件不按套"的正面证据）
  ③ dislike_counts：只出 >0 的；给了 ids 就只统计那几件；空列表返回 {}
  ④ /api/stats 的 disliked[] 带 reject_count + ΔN（ΔN 是"删了损失多少组合"）
  ⑤ 阈值常量 = 3（页面提示靠它）
  ⑥ 迁移幂等：init_db 再跑一次不炸，列还在
  ⑦ 删掉这件 → w_item 行跟着 CASCADE 消失，不留幽灵
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

from app import avoid, db, kit, outfits  # noqa: E402

FAILS: list[str] = []
_TMP: list[pathlib.Path] = []


def p(t: str = "") -> None:
    print(t)


def ok(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


def isolate() -> pathlib.Path:
    """把 DB 指到临时副本（同 test_delete）。

    ⚠️ **必须做**：这个自检会真的写 `w_item`。不隔离的话，跑一次就把用户
    衣服的权重改了 —— 而且不报错，只会让人觉得"排序怎么怪怪的"。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsule-dislike-"))
    real = db.get_conn()
    copy = tmp / "closet.db"
    dst = sqlite3.connect(copy)  # fk-agnostic: 只是 backup API 的写入目标，不跑业务 SQL
    try:
        real.backup(dst)
        dst.commit()
    finally:
        dst.close()
        real.close()
    db.DB_PATH = copy
    db.DATA_DIR = tmp
    # 「不喜欢」现在还会写 avoid / 动 kit —— 同样必须隔离，否则跑一次就把
    # 用户的"今天不想看到"和随身清单改掉了，而且不报错。
    for mod, name in ((avoid, "avoid.json"), (kit, "kit.json")):
        d = pathlib.Path(tempfile.mkdtemp(prefix=f"capsule-{mod.__name__}-"))
        mod.DATA_DIR = d
        mod.CONFIG_PATH = d / name
        _TMP.append(d)
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把报告写到文件")
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
    tmpdir = None
    try:
        if not db.DB_PATH.exists():
            ok(False, f"找不到真实库 {db.DB_PATH}")
            raise SystemExit(1)
        tmpdir = isolate()
        db.init_db()
        conn = db.get_conn()

        p("「不喜欢」按件自检")
        p()

        # 造一个"一套三件"的场景：挑一件已入库单品 + 另外两件做对照组
        owned = db.load_items(conn, "owned")
        if len(owned) < 3:
            ok(False, "衣橱少于 3 件，无法构造对照组")
            raise SystemExit(1)
        victim = int(owned[0]["id"])
        others = [int(x["id"]) for x in owned[1:3]]

        def weight(iid: int) -> tuple[float, int]:
            r = conn.execute(
                "SELECT beta, COALESCE(dislike_n,0) AS n FROM w_item WHERE item_id=?",
                (iid,)).fetchone()
            return (float(r["beta"]), int(r["n"])) if r else (1.0, 0)

        b0, n0 = weight(victim)
        o0 = [weight(i) for i in others]

        p("① 连点三次")
        p("─" * 56)
        for k in (1, 2, 3):
            out = outfits.dislike_item(conn, victim)
            ok(out["dislike_n"] == n0 + k, f"第 {k} 次之后 dislike_n = {n0 + k}")
        b1, n1 = weight(victim)
        ok(abs(b1 - (b0 + 3.0)) < 1e-6, f"beta 加满 3.0（{b0:.2f} → {b1:.2f}）")
        p()

        p("② 同套的其他件一分不动（按件不是按套）")
        p("─" * 56)
        o1 = [weight(i) for i in others]
        ok(o1 == o0, f"另外两件的 (beta, dislike_n) 原样未动：{o0} → {o1}")
        p()

        p("③ dislike_counts")
        p("─" * 56)
        c = outfits.dislike_counts(conn)
        ok(c.get(victim) == n1, f"全表里这一件 = {n1}")
        ok(victim not in c or c[victim] > 0, "只出 >0 的（0 不占位）")
        lim = outfits.dislike_counts(conn, [victim, others[0]])
        ok(set(lim) <= {victim, others[0]}, f"给了 ids 就只统计这几件（{sorted(lim)}）")
        ok(outfits.dislike_counts(conn, []) == {}, "空列表 → {}（不是全表）")
        p()

        p("④ /api/stats 的 disliked[]（带 ΔN）")
        p("─" * 56)
        from app.main import api_stats
        st = api_stats(temp=20, detail=True)
        row = next((x for x in st.get("disliked", []) if x["id"] == victim), None)
        ok(row is not None, "体检数据里出现了这一件")
        if row:
            ok(row["reject_count"] == n1, f"reject_count = {n1}")
            ok(isinstance(row.get("delta_n"), int),
               f"带 ΔN（删了损失 {row.get('delta_n')} 套组合）—— 和次数一起才做得了决定")
        p()

        p("⑤ 阈值常量")
        p("─" * 56)
        ok(outfits.DISLIKE_ELIMINATE_HINT == 3,
           f"DISLIKE_ELIMINATE_HINT = {outfits.DISLIKE_ELIMINATE_HINT}（页面提示靠它）")
        p()

        p("⑥ 迁移幂等")
        p("─" * 56)
        db.init_db()          # 再跑一次：列已存在，不该报错
        cols = [r[1] for r in conn.execute("PRAGMA table_info(w_item)").fetchall()]
        ok("dislike_n" in cols, f"w_item 仍有 dislike_n（{cols}）")
        p()

        p("⑦ 「不喜欢」＝今天直接排掉（不是降权）")
        p("─" * 56)
        from app.main import api_today
        avoid.clear()

        def today(**kw):
            kw.setdefault("save", False)
            kw.setdefault("use_llm", False)
            kw.setdefault("temp", 22)
            return api_today(**kw)

        base = today(seed=5)
        v2 = int(base["outfits"][0]["items"][0]["id"])
        outfits.dislike_item(conn, v2)
        avoid.add(v2)
        after = today(seed=5)
        hit = [i for o in (after.get("outfits") or []) for i in o["items"]
               if int(i["id"]) == v2]
        ok(not hit, "不喜欢之后，返回的每一套里都不含它（**硬排除**，不是扣分）")
        ok(any(int(x["id"]) == v2 for x in (after.get("avoided") or [])),
           "响应里带着 avoided —— 页面据此给出「点一下放回」的入口")

        avoid.remove(v2)
        back = today(seed=5)
        ok(avoid.ids() == set(), "恢复之后 avoid 清空")
        ok(len(back.get("outfits") or []) > 0, "恢复之后照常排得出来")
        p()

        p("⑧ 旅行模式里不喜欢 → 也从随身清单里拿掉")
        p("─" * 56)
        kit.set_kit("自检行程", "2026-10-24", 3, [97, 86, 106])
        kk = kit.remove_item(97)
        ok(kk is not None and kk["n_items"] == 2,
           f"清单从 3 件变 {kk and kk['n_items']} 件")
        ok(97 not in {int(x) for x in (kit.load() or {"item_ids": []})["item_ids"]},
           "清单里确实没有 97 了")
        ok(kk and not kk["emptied"], "还有剩的，旅行模式继续")
        kk2 = kit.remove_item(86)
        kk3 = kit.remove_item(106)
        ok(kk3 and kk3["emptied"], "拿空了 → emptied=True（前端据此说「已退出旅行模式」）")
        ok(kit.load() is None, "清单空了就整个关掉 —— 留个 0 件的清单等于让首页卡死在空态")
        p()

        p("⑨ 删掉这件 → w_item 行跟着走")
        p("─" * 56)
        db.delete_item(conn, victim)
        left = conn.execute("SELECT 1 FROM w_item WHERE item_id=?", (victim,)).fetchone()
        ok(left is None, "w_item 里那一行没了（ON DELETE CASCADE，不留幽灵）")

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
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
