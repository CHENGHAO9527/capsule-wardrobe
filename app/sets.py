# -*- coding: utf-8 -*-
"""套装：自己搭的一整套，存下来之后推荐可以直接用。

它和已有的两张表都不是一回事：

  · `outfits` —— 程序算给你的一次性结果，用完就过去了（反馈挂在这上面）
  · `sets`    —— **你自己挑的，会一直在**，而且今日页会优先端出来

判定一套能不能穿，**不另写一套规则**：把这几件单独装进一个 `Closet`，
逼每一件都上，问引擎还枚不枚举得出合法路径。规则改了它自动跟着改 ——
不会出现「套装页说能穿、今日页说不能穿」这种各说各话。

（这条是这个项目的老规矩：凡是能用引擎回答的问题，就不要在旁边再实现一遍。
"算不算换了一套"的口径、温度口径，都是因为写了两份而踩过坑。）
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional

from . import catalog
from . import outfits as rec
from .engine import graph, rules

SLOT_ORDER = list(rules.LAYER_ORDER)

# 扫一遍温度，看这套在哪些温度下是合法搭配。
# -10~38 覆盖了预报能给出的全部范围；一套最多七八件，每次枚举都是毫秒级，
# 扫 49 个温度也就一眨眼 —— 换来的是"不用维护一个会和引擎漂移的温度区间字段"。
TEMP_SCAN = list(range(-10, 39))

# 一个套装最少几件才算一套
MIN_ITEMS = 2

# 评估结果按「衣橱指纹 + 场合」缓存。**必须用 fingerprint 而不是 id 列表** ——
# 只按 id 缓存的话，改了一件的温度区间，这里会一直返回旧结论（这个坑踩过）。
_EVAL_CACHE: dict = {}
_EVAL_CACHE_MAX = 128


# ══════════════════════════════════════════════
# 判定：这几件是不是一套能穿的搭配
# ══════════════════════════════════════════════

def can_wear(items: list[dict], temp: float,
             occasion: Optional[str] = None) -> bool:
    """**只有这些衣服**、在这个温度下，这套能不能穿出去。

    逐温度扫描用 `evaluate()`；单点精确判定（今日页每次刷新要问一遍）用这个。

    ⚠️ 前两关必须自己做，不能只靠下面的枚举：
      · **同槽位两件**（两条裤子）—— `must` 是 `{slot: item}`，同槽位会被静默压成一件
      · **这个温度根本不该出现的那一层** —— 引擎的层位表由温度决定，
        28℃ 的方案里压根没有 L2/L3 这两个槽，于是"T恤+卫衣+夹克+裤+鞋"这套
        在 28℃ 会被判定成能穿（多出来的两件被无声丢掉）。
        一套衣服的意思就是"这些一起穿"，少一件都不算。
    """
    if not items or len({it.get("slot") for it in items}) != len(items):
        return False
    try:
        required, optional, _max_stack = graph.layer_plan(float(temp))
        want = set(required) | set(optional)
        if any(it.get("slot") not in want for it in items):
            return False
        must = {it["slot"]: it for it in items}
        return graph.Closet(items).count_paths(
            float(temp), occasion, must=must, limit=1) > 0
    except Exception:
        # 单品数据缺字段之类 → 当作穿不了。不抛异常：今日页不能因为一个坏套装打不开。
        return False


def evaluate(items: list[dict], occasion: Optional[str] = None) -> dict:
    """这套在哪些温度下能穿。

    返回 `{ok, reason, temps, temp_min, temp_max}`：
      · `ok=False` 时 `reason` 说得清**为什么**（撞色 / 缺层 / 温度对不上），
        而不是一句"不合法" —— 失败要看得见，这是本项目的老要求。
      · `temps` 是全部可穿温度，`temp_min`/`temp_max` 是其中**最长连续段**的首尾，
        给用户看的就是这个范围（断续的情况很少，但真出现了也不会撒谎）。
    """
    items = list(items or [])
    if len(items) < MIN_ITEMS:
        return _bad(f"至少 {MIN_ITEMS} 件才算一套。")

    slots = [it.get("slot") for it in items]
    dup = sorted({s for s in slots if slots.count(s) > 1})
    if dup:
        names = "、".join(catalog.SLOT_NAMES.get(s, s) for s in dup)
        return _bad(f"同一个槽位只能放一件（{names} 重复了）。")

    closet = graph.Closet(items)
    good = [t for t in TEMP_SCAN if can_wear(items, float(t), occasion)]
    if not good:
        return _bad(_why(closet, items))

    lo, hi, _n = _longest_run(good)
    return {"ok": True, "reason": "", "temps": good,
            "temp_min": lo, "temp_max": hi}


def evaluate_cached(items: list[dict],
                    occasion: Optional[str] = None) -> dict:
    key = (graph.closet_fingerprint(items), occasion or "")
    hit = _EVAL_CACHE.get(key)
    if hit is not None:
        return hit
    res = evaluate(items, occasion)
    if len(_EVAL_CACHE) > _EVAL_CACHE_MAX:
        _EVAL_CACHE.clear()
    _EVAL_CACHE[key] = res
    return res


def _bad(reason: str) -> dict:
    return {"ok": False, "reason": reason, "temps": [],
            "temp_min": None, "temp_max": None}


def _longest_run(temps: list[int]) -> tuple[int, int, int]:
    """最长的一段连续温度（`TEMP_SCAN` 是连续的整数，所以直接比差值）。"""
    best = (temps[0], temps[0], 1)
    lo = prev = temps[0]
    for t in temps[1:]:
        if t == prev + 1:
            prev = t
        else:
            if prev - lo + 1 > best[2]:
                best = (lo, prev, prev - lo + 1)
            lo = prev = t
    if prev - lo + 1 > best[2]:
        best = (lo, prev, prev - lo + 1)
    return best


def _why(closet, items: list[dict]) -> str:
    """凑不成一套时，指出到底是哪儿不对。顺序按"原因的具体程度"排。"""
    # ① 配对冲突最具体 —— 直接说哪两件、因为什么
    names = {id(it): (it.get("name") or "?") for it in items}
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            r = closet.pair(a, b)
            if r:
                return (f"{names[id(a)]} ✕ {names[id(b)]} 配不到一起：{r}。")
    # ② 再看缺层：在"这几件最用得上"的温度上，必需层还差什么
    have = {it.get("slot") for it in items}
    best: Optional[tuple[int, list[str]]] = None
    for t in TEMP_SCAN:
        required, _opt, _stack = graph.layer_plan(float(t))
        lack = [s for s in required if s not in have]
        if best is None or len(lack) < len(best[1]):
            best = (t, lack)
    if best and best[1]:
        miss = "、".join(graph.SLOT_LABEL.get(s, s) for s in best[1])
        return f"缺 {miss} —— 一套得先能穿出门，再谈好不好看。"
    # ③ 层位齐了还不成 → 温度区间撞不上（一件要热天、一件要冷天）
    lo = max(rules.temp_min(it) for it in items)
    hi = min(rules.temp_max(it) for it in items)
    if lo > hi:
        return (f"这几件没有共同的适穿温度：合起来要求既 ≥{lo}℃ 又 ≤{hi}℃。"
                f"多半是挑来的件差了季节。")
    return "这几件在任何温度下都凑不成一套合法搭配。"


# ══════════════════════════════════════════════
# 存取
# ══════════════════════════════════════════════

def _hydrate(conn, rows) -> list[dict]:
    """给套装行挂上它的单品。"""
    ids = [int(r["id"]) for r in rows]
    out = [dict(r) for r in rows]
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    links = conn.execute(
        f"SELECT si.set_id AS _sid, i.* FROM set_items si "
        f"JOIN items i ON i.id = si.item_id WHERE si.set_id IN ({q})",
        ids).fetchall()
    bag: dict[int, list[dict]] = defaultdict(list)
    for r in links:
        d = dict(r)
        bag[int(d.pop("_sid"))].append(d)
    for d in out:
        items = bag.get(int(d["id"]), [])
        items.sort(key=lambda it: (SLOT_ORDER.index(it["slot"])
                                   if it.get("slot") in SLOT_ORDER else 99,
                                   int(it["id"])))
        d["items"] = items
        d["n_items"] = len(items)
    return out


def list_sets(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM sets ORDER BY id DESC").fetchall()
    return _hydrate(conn, rows)


def get_set(conn, set_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone()
    if not row:
        return None
    got = _hydrate(conn, [row])
    return got[0] if got else None


def _split_ids(conn, item_ids) -> tuple[list[int], list[int]]:
    """去重保序，再分成 (还在衣橱里的, 已经取不到的)。

    ⚠️ 两个返回值都基于**用户传进来的原始列表**算 —— 第一版让 `pick_items`
    在"已经筛完的列表"里再找缺失项，结果 `missing` **恒为空**，
    界面上永远不会提示"少了 X 件"。典型的不报错、也不算数。
    """
    want: list[int] = []
    for x in item_ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in want:
            want.append(i)
    if not want:
        return [], []
    q = ",".join("?" * len(want))
    alive = {int(r["id"]) for r in conn.execute(
        f"SELECT id FROM items WHERE id IN ({q}) "
        f"AND stage = 'owned' AND status = 'in_wardrobe'", want).fetchall()}
    # 保持用户挑的顺序（先挑上衣再挑裤子是自然的），不按 id 重排
    return ([i for i in want if i in alive], [i for i in want if i not in alive])


def _clean_ids(conn, item_ids) -> list[int]:
    return _split_ids(conn, item_ids)[0]


def pick_items(conn, item_ids) -> tuple[list[dict], list[int]]:
    """按用户给的顺序取出单品行，顺带报告哪些 id 取不到。

    取不到是正常情况（那件刚被删了，或者前端存的草稿过期了）——
    所以是"丢掉并报告"，不是报错。
    """
    ids, missing = _split_ids(conn, item_ids)
    if not ids:
        return [], missing
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM items WHERE id IN ({q})", ids).fetchall()
    by_id = {int(r["id"]): dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id], missing


def _auto_name(conn, occasion: Optional[str]) -> str:
    n = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    return f"{occasion or '自搭'}套装 {int(n) + 1}"


# 「这个字段没传」和「这个字段要清空」是两件事，不能都用 None 表示 ——
# 用 None 的话，PATCH 只改个名字就会把 occasion 悄悄清掉。
UNSET = object()


def save_set(conn, name=None, item_ids=None, occasion=UNSET, note=UNSET,
             set_id: Optional[int] = None) -> dict:
    """新建或改写一套。

    **没传的字段一律保持原样**，不是用默认值覆盖 ——
    `item_ids=None` 表示"不改单品"，`occasion=UNSET` 表示"不改场合"。

    踩过的坑：最初把 `item_ids` 的默认值写成 `[]`，于是 PATCH 只改名字时
    单品被清空、直接抛"至少挑 2 件"。看起来像 PATCH 坏了，
    但真正危险的是**它不会报错的那种版本**（比如默认值恰好是合法值）。

    还有一条：**存下来的必须是能穿的**。存一套永远穿不出去的东西，
    只会在今日页里变成一个永不出现的幽灵，用户还以为功能坏了。
    """
    cur = None
    if set_id:
        cur = conn.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone()
        if not cur:
            raise ValueError("这套不存在（可能已经被删了）。")

    # ① 单品：没传就沿用原来那几件
    if item_ids is None and cur:
        ids = [int(r["item_id"]) for r in conn.execute(
            "SELECT item_id FROM set_items WHERE set_id = ?", (set_id,)).fetchall()]
    else:
        ids = _clean_ids(conn, item_ids)
    if len(ids) < MIN_ITEMS:
        raise ValueError(f"至少挑 {MIN_ITEMS} 件。")

    items, _missing = pick_items(conn, ids)
    occ = (cur["occasion"] if cur else None) if occasion is UNSET else occasion
    nte = (cur["note"] if cur else None) if note is UNSET else note
    ev = evaluate(items, occ)
    if not ev["ok"]:
        raise ValueError(ev["reason"])

    # ② 名字：没给（或给了空的）就沿用原名；新建时编一个
    name = (name or "").strip()[:40]
    if not name:
        name = (cur["name"] if cur else "") or _auto_name(conn, occ)

    if cur:
        conn.execute(
            "UPDATE sets SET name = ?, occasion = ?, note = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (name, occ, nte, set_id))
        conn.execute("DELETE FROM set_items WHERE set_id = ?", (set_id,))
        target = int(set_id)
    else:
        cur2 = conn.execute(
            "INSERT INTO sets (name, occasion, note) VALUES (?, ?, ?)",
            (name, occ, nte))
        target = int(cur2.lastrowid)

    for it in items:
        conn.execute(
            "INSERT OR IGNORE INTO set_items (set_id, item_id, slot) VALUES (?,?,?)",
            (target, int(it["id"]), it["slot"]))
    conn.commit()
    return get_set(conn, target) or {}


def delete_set(conn, set_id: int) -> dict:
    row = conn.execute("SELECT id, name FROM sets WHERE id = ?",
                       (set_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "not_found"}
    conn.execute("DELETE FROM set_items WHERE set_id = ?", (set_id,))
    conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))
    conn.commit()
    return {"ok": True, "id": int(set_id), "name": row["name"]}


def mark_used(conn, set_id: int) -> None:
    conn.execute(
        "UPDATE sets SET used_at = datetime('now','localtime'), "
        "use_count = COALESCE(use_count, 0) + 1 WHERE id = ?", (set_id,))


# ══════════════════════════════════════════════
# 给页面用
# ══════════════════════════════════════════════

def all_with_state(conn, temp: Optional[float] = None,
                   occasion: Optional[str] = None) -> list[dict]:
    """每个套装挂上「能不能穿、什么温度能穿」。套装页用它。

    有 `temp` 时额外给一个 `usable`：这个温度下能不能直接穿。
    """
    out = []
    for s in list_sets(conn):
        ev = evaluate_cached(s["items"], occasion)
        if not ev["ok"] and s["n_items"] < MIN_ITEMS:
            # 库里存的套装是"至少两件才能存"，所以件数不够只可能是**里面的单品
            # 被删了**。这种情况下说"至少两件才算一套"等于没说，得告诉用户实情。
            ev = dict(ev)
            ev["reason"] = (f"这套只剩 {s['n_items']} 件了 —— 有单品被删掉了。"
                            f"补一件，或者删掉这套。")
        row = dict(s)
        row["eval"] = ev
        row["usable"] = bool(ev["ok"] and temp is not None
                             and can_wear(s["items"], float(temp), occasion))
        out.append(row)
    return out


def usable_at(conn, temp: float, occasion: Optional[str] = None) -> list[dict]:
    """当前温度 + 场合下能直接穿的套装。今日页用它。

    场合不靠套装自己记的那个字段筛 —— 直接拿当前场合去问引擎，
    这样「存的时候写通勤、但正式度其实也压得住休闲」不会漏掉。
    """
    out = []
    for s in list_sets(conn):
        if not s["items"]:
            continue
        if not evaluate_cached(s["items"], occasion)["ok"]:
            continue
        if can_wear(s["items"], float(temp), occasion):
            out.append(s)
    return out


def to_outfit(row: dict, temp: Optional[float] = None,
              occasion: Optional[str] = None) -> dict:
    """转成今日页那套结构 —— 前端不用为"套装"再写一遍渲染。

    `score` / `reason` 走的是同一套本地打分，所以自己搭的套装也有一句
    说得过去的理由，不是干巴巴一个名字。
    """
    path = list(row.get("items") or [])
    try:
        s = rec.score_outfit(path, float(temp if temp is not None else 20),
                             occasion, {"item": {}, "pair": {}}, {},
                             set(), random.Random(0))
        reason = rec.make_reason(path, s,
                                 float(temp if temp is not None else 20), occasion)
        notes = s["cons"][:2]
        score = s["score"]
    except Exception:
        reason, notes, score = "", [], None
    return {
        "score": score,
        "path": path,
        "items": [rec.public_item(it) for it in path],
        "reason": reason,
        "notes": notes,
        "set_id": int(row["id"]),
        "set_name": row.get("name"),
    }
