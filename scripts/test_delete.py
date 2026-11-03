"""删除链路 + 外键自愈测试（**不碰真实库**）。

要证明五件事：
  ① 复现 bug：老结构（outfit_items 少了 ON DELETE CASCADE）上删一件进过搭配的衣服 → 外键拦死
  ② 老库能自愈：迁移清掉孤儿、补上 CASCADE，数据一行不丢，动 DDL 前有备份
  ③ 删一件"进过搭配"的衣服真的能成，且不留残留
  ④ 删不存在的 id 返回 not_found，不是假成功
  ⑤ 没人再用裸 sqlite3.connect 碰**应用的库**（它默认 foreign_keys=OFF，会静默攒孤儿）

全程跑在真实库的**临时副本**上（在线备份 API 拿一致快照），最后删掉副本。
用法：python scripts/test_delete.py [--out out.txt]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

L: list[str] = []
FAILS: list[str] = []
# ⚠️ 拆开写：这条守卫要扫 `scripts/*.py`，也就扫它自己。
#    字面量拼在一起，`_CONNECT = "sqlite3.connect("` 这一行自己就成了违规项 ——
#    原来那版能过是因为它那行**碰巧**含 `fk-agnostic` 字样而自我豁免，属于运气。
_CONNECT = "sqlite3" + ".connect("


def p(s: str = "") -> None:
    L.append(s)


def ok(cond: bool, label: str) -> None:
    p("  " + ("✓ " if cond else "✗ ") + label)
    if not cond:
        FAILS.append(label)


# ── 把一个库降级成"老结构"：outfit_items 不带 ON DELETE ────────────────
_OLD_OUTFIT_ITEMS = """
CREATE TABLE outfit_items_old (
  outfit_id INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  item_id   INTEGER NOT NULL REFERENCES items(id),
  slot      TEXT NOT NULL,
  PRIMARY KEY (outfit_id, item_id)
);
INSERT INTO outfit_items_old (outfit_id, item_id, slot)
    SELECT outfit_id, item_id, slot FROM outfit_items;
DROP TABLE outfit_items;
ALTER TABLE outfit_items_old RENAME TO outfit_items;
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()

    if not db.DB_PATH.exists():
        p(f"找不到真实库 {db.DB_PATH}，无法建副本")
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="capsule-del-"))
    real = db.get_conn()
    copy = tmp / "closet.db"
    dst = sqlite3.connect(copy)  # fk-agnostic: 这只是备份 API 的写入目标，不跑业务 SQL
    try:
        real.backup(dst)                # 在线备份，拿到一致快照
        dst.commit()
    finally:
        dst.close()
        real.close()

    # 之后所有 db.get_conn() / backup_db() 都落在临时目录，真实库一个字不动
    db.DB_PATH = copy
    db.DATA_DIR = tmp

    conn = db.get_conn()

    # ── 造场景：降级结构 + 注入孤儿 + 一条合法搭配 ────────────────
    p("=" * 64)
    p("① 复现「页面上没办法删除」")
    p("=" * 64)

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(_OLD_OUTFIT_ITEMS)

    item_ids = [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id LIMIT 3")]
    assert len(item_ids) == 3, "副本里至少要有 3 件衣服"
    # 一条**合法**搭配（父行存在），用来证明迁移不会把好数据也清掉
    conn.execute("INSERT INTO outfits (id, date, occasion, temp_actual, source, local_score) "
                 "VALUES (?, date('now','localtime'), '通勤', 16, 'local', 80)", (900001,))
    for n, iid in enumerate(item_ids):
        conn.execute("INSERT INTO outfit_items (outfit_id, item_id, slot) VALUES (?,?,?)",
                     (900001, iid, ["L1", "L3", "L4"][n]))
    # 三行孤儿：父搭配不存在（真实库里这样攒了 979 行）
    for bogus in (990001, 990002, 990003):
        conn.execute("INSERT INTO outfit_items (outfit_id, item_id, slot) VALUES (?,?,?)",
                     (bogus, item_ids[0], "L1"))
    conn.commit()                       # ← 必须的：PRAGMA 在事务里是空操作，
    conn.execute("PRAGMA foreign_keys = ON")   #   上一版漏了这句 commit，FK 根本没开回来

    ok(conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "外键约束已开启")
    n_oi = conn.execute("SELECT COUNT(*) FROM outfit_items").fetchone()[0]
    n_valid = conn.execute(
        "SELECT COUNT(*) FROM outfit_items oi "
        "WHERE EXISTS (SELECT 1 FROM outfits o WHERE o.id = oi.outfit_id)").fetchone()[0]
    n_orphan = n_oi - n_valid
    p(f"  outfit_items {n_oi} 行 = 合法 {n_valid} + 孤儿 {n_orphan}"
      f"（副本里带着真实库的全部行，我又注入了 3 行孤儿）")
    ok(n_orphan >= 3, f"孤儿不少于注入的 3 行（实际 {n_orphan}）")
    # 只断言**我自己造的**那 3 行在，不断言"库里一共只有 3 行" ——
    # 真实库会随着「今日」页面被打开而正常攒下搭配记录（实测 15 条），
    # 写死绝对行数的断言迟早会误报。
    mine = conn.execute("SELECT COUNT(*) FROM outfit_items WHERE outfit_id = 900001"
                        ).fetchone()[0]
    ok(mine == 3, f"我注入的那 3 行都在（实际 {mine}）")
    ok(db._fk_needs_cascade(conn, "outfit_items", "items"),
       "降级后的库确实被判为「需要重建外键」")

    victim = item_ids[0]
    try:
        conn.execute("DELETE FROM items WHERE id = ?", (victim,))
        conn.rollback()
        ok(False, "裸 DELETE 应当被外键拦住（复现 bug）")
    except sqlite3.IntegrityError as e:
        conn.rollback()
        p(f"  复现成功：裸 DELETE id={victim} → IntegrityError: {e}")
        ok(True, "裸 DELETE 被外键拦住 —— 这就是用户看到「点了没反应」的根因")

    # ── 自愈 ──────────────────────────────────────────────────────
    p()
    p("=" * 64)
    p("② 老库自愈（清孤儿 + 补 CASCADE + 备份，而且不许丢好数据）")
    p("=" * 64)

    n_items_before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    db._migrate(conn)                       # ← 服务启动时跑的就是这个

    baks = sorted(tmp.glob("closet.db.bak-*"))
    ok(bool(baks), f"动 DDL 之前做了备份 → {baks[-1].name if baks else '（没备份！）'}")
    ok(not conn.execute("PRAGMA foreign_key_check").fetchall(),
       "迁移后 foreign_key_check 干净（孤儿已清）")
    n_oi2 = conn.execute("SELECT COUNT(*) FROM outfit_items").fetchone()[0]
    n_orphan2 = conn.execute(
        "SELECT COUNT(*) FROM outfit_items oi "
        "WHERE NOT EXISTS (SELECT 1 FROM outfits o WHERE o.id = oi.outfit_id)"
    ).fetchone()[0]
    ok(n_orphan2 == 0, f"孤儿清零（{n_orphan} → {n_orphan2}）")
    ok(n_oi2 == n_valid, f"合法搭配一行没丢（{n_valid} → {n_oi2}）")
    ok(not db._fk_needs_cascade(conn, "outfit_items", "items"),
       "outfit_items.item_id 已是 ON DELETE CASCADE")
    ok(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == n_items_before,
       f"items 一行没丢（{n_items_before}）")
    db._migrate(conn)                   # 再跑一次，应当是幂等的
    ok(not db._fk_needs_cascade(conn, "outfit_items", "items"),
       "二次迁移后依然正确（幂等）")

    # ── ③ 删除真的能成 ───────────────────────────────────────────
    p()
    p("=" * 64)
    p("③ 删一件「进过搭配」的衣服")
    p("=" * 64)
    refs = conn.execute("SELECT COUNT(*) FROM outfit_items WHERE item_id=?",
                        (victim,)).fetchone()[0]   # 迁移之后重算，孤儿已经清掉了
    p(f"  拿 id={victim} 开刀，它在 {refs} 条搭配里")

    r = db.delete_item(conn, victim)
    ok(r.get("ok") is True, f"delete_item 成功（摘掉 {r.get('unlinked')} 个引用）")
    ok(r.get("unlinked") == refs, f"摘掉的引用数对得上（{r.get('unlinked')} == {refs}）")
    ok(not conn.execute("SELECT 1 FROM items WHERE id=?", (victim,)).fetchone(),
       "本体已删除")
    ok(not conn.execute("SELECT 1 FROM outfit_items WHERE item_id=?", (victim,)).fetchone(),
       "指向它的搭配引用已清空")
    ok(not conn.execute("SELECT 1 FROM wear_log WHERE item_id=?", (victim,)).fetchone(),
       "wear_log 无残留（CASCADE）")
    ok(not conn.execute("SELECT 1 FROM w_pair WHERE item_a_id=? OR item_b_id=?",
                        (victim, victim)).fetchone(), "w_pair 无残留（CASCADE）")
    ok(not conn.execute("PRAGMA foreign_key_check").fetchall(),
       "删除后 foreign_key_check 干净")

    # 连删：用户的真实场景是一次清理 4 组重复件
    more = [x[0] for x in conn.execute("SELECT id FROM items ORDER BY id DESC LIMIT 3")]
    rs = [db.delete_item(conn, x) for x in more]
    ok(all(x.get("ok") for x in rs), f"连删 {len(more)} 件都成功")
    ok(not conn.execute("PRAGMA foreign_key_check").fetchall(), "连删后依然干净")

    # ── ④ 边界 ───────────────────────────────────────────────────
    p()
    p("=" * 64)
    p("④ 边界")
    p("=" * 64)
    r3 = db.delete_item(conn, 999999)
    ok(r3.get("ok") is False and r3.get("reason") == "not_found",
       f"删不存在的 id → not_found（{r3}），不是假成功")
    conn.close()

    # ── ⑤ 裸连接守卫 ─────────────────────────────────────────────
    p()
    p("=" * 64)
    p("⑤ 不许有人绕过 db.get_conn()（它才设 PRAGMA foreign_keys=ON）")
    p("=" * 64)
    offenders = []
    excused = 0
    for d in ("app", "scripts"):
        for f in sorted((ROOT / d).glob("*.py")):
            if f.name == "db.py":
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if _CONNECT not in code:
                    continue
                # 两种例外都是**说不出问题**的，但必须写明白，不能默默放过：
                #   · 行里标了 `fk-agnostic` —— 明确声明"这条连接不需要管外键"
                #   · 只读 URI（`mode=ro`）—— 它**根本写不进去**，攒不出孤儿
                if "fk-agnostic" in line:
                    excused += 1
                    continue
                if "mode=ro" in code:
                    excused += 1
                    continue
                offenders.append(f"{d}/{f.name}:{n}  {line.strip()}")
    ok(not offenders, "没有拿裸连接碰应用的库" +
       ("" if not offenders else " → " + " | ".join(offenders)))
    if excused:
        p(f"  · 另有 {excused} 条只读 / 标注过的连接，按例外放过"
          f"（要么是只读 URI，要么自带 `fk-agnostic` 标注）")

    shutil.rmtree(tmp, ignore_errors=True)

    p()
    if FAILS:
        p(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            p("  - " + f)
    else:
        p("全部通过。")
    return 1 if FAILS else 0


def _finish(out: str | None) -> None:
    text = "\n".join(L)
    print(text)
    if out:
        (ROOT / out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import traceback
    argv = sys.argv[1:]
    _out = None
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            _out = argv[i + 1]
    try:
        _code = main()
    except Exception:                       # 崩了也要把栈写进 UTF-8 文件，
        L.append("")                        # 否则 PowerShell 会把中文输出吞掉
        L.append("脚本异常终止：")
        L.append(traceback.format_exc())
        _code = 2
    _finish(_out)
    sys.exit(_code)
