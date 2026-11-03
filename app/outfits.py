# -*- coding: utf-8 -*-
"""每日推荐：从合法搭配里挑出该穿的那几套，并接受滑动反馈。

分工和文档里定的一样：
  · 合法路径由 graph.py 用硬规则枚举出来（物理上一定穿得出去）
  · 这里只做**排序**和**学习**，不生成任何东西
  · 模型（可选）只在最后做审美排序，见 vision.rank_outfits

学习闭环是纯本地的：
  「不喜欢」→ w_item.beta += 1、w_pair.beta += 1
  「就穿这套」→ w_item.alpha += 1、wear_log 记一条
  下一轮打分时读回后验均值 alpha/(alpha+beta) 参与排序。

为什么用 Beta 而不是百分比：后验均值天生带样本量衰减，被拒一次不会打死一件衣服，
出现三次以上才明显降权。手写百分比权重两星期就会被噪音带偏。
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional

from .engine import graph, rules

# 打分基准分。各项加减都相对它。
BASE_SCORE = 60.0

# 反馈 → 权重增量。auto_worn 是最强正信号（你实际穿了，比任何自述都可靠）
GESTURE_DELTA = {
    "accepted": ("alpha", 1.0),
    "auto_worn": ("alpha", 1.5),
    "skipped": ("beta", 0.3),
    "rejected": ("beta", 1.0),
}

# 一件衣服**出现在被拒搭配里**多少次，才够格提示"该淘汰了"。
# 单次拒绝说明不了什么（很可能是**那一套配得不好**，不是这件衣服不好），
# 所以给一个阈值：够了吗？够了几次才提一句。**永远不自动删** —— 由人拍板。
DISLIKE_ELIMINATE_HINT = 3


def dislike_item(conn, item_id: int, weight: float = 1.0) -> dict:
    """「不喜欢」**这一件衣服**（不是这一整套）。

    ⚠️ 语义必须按件。一套组合被你否掉，说明不了其中**哪一件**有问题 ——
    摊到三件头上，只会让三件都白背一次黑锅，而且"这件该淘汰了"永远算不出来。
    （用户原话：「不喜欢是指单件衣服不是组合」。）
    """
    conn.execute(
        "INSERT INTO w_item (item_id, alpha, beta, dislike_n, last_seen) "
        "VALUES (?, 1.0, 1.0 + ?, 1, datetime('now','localtime')) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "beta = beta + ?, "
        "dislike_n = COALESCE(dislike_n, 0) + 1, "
        "last_seen = datetime('now','localtime')",
        (int(item_id), float(weight), float(weight)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT beta, COALESCE(dislike_n, 0) AS n FROM w_item WHERE item_id = ?",
        (int(item_id),)).fetchone()
    return {"item_id": int(item_id),
            "beta": round(float(row["beta"]), 3) if row else None,
            "dislike_n": int(row["n"]) if row else 0}


def dislike_counts(conn, item_ids: Optional[list[int]] = None) -> dict[int, int]:
    """每件单品被「不喜欢」过几次（按件累计）。体检页「待淘汰观察」读它。

    ⚠️ 用 `COALESCE`：老库刚 ALTER 出来的列可能是 NULL（迁移只加列、不回填）。
    """
    sql = ("SELECT item_id, COALESCE(dislike_n, 0) AS n FROM w_item "
           "WHERE COALESCE(dislike_n, 0) > 0")
    args: tuple = ()
    if item_ids is not None:
        ids = [int(x) for x in item_ids]
        if not ids:
            return {}
        sql += " AND item_id IN (%s)" % ",".join("?" * len(ids))
        args = tuple(ids)
    return {int(r["item_id"]): int(r["n"]) for r in conn.execute(sql, args).fetchall()}


# ══════════════════════════════════════════════
# 权重读取
# ══════════════════════════════════════════════

def load_weights(conn) -> dict:
    item_w: dict[int, float] = {}
    for r in conn.execute("SELECT item_id, alpha, beta FROM w_item").fetchall():
        a, b = float(r["alpha"] or 1), float(r["beta"] or 1)
        item_w[int(r["item_id"])] = a / (a + b) if (a + b) else 0.5

    pair_w: dict[tuple[int, int], float] = {}
    for r in conn.execute("SELECT item_a_id, item_b_id, alpha, beta FROM w_pair").fetchall():
        a, b = float(r["alpha"] or 1), float(r["beta"] or 1)
        pair_w[(int(r["item_a_id"]), int(r["item_b_id"]))] = a / (a + b) if (a + b) else 0.5

    return {"item": item_w, "pair": pair_w}


def recent_wear_map(conn, days: int = 14) -> dict[int, int]:
    """item_id → 最近多少天内穿过（越小越新）。用于新鲜度。"""
    rows = conn.execute(
        "SELECT item_id, MAX(date) AS d FROM wear_log "
        "WHERE date >= date('now', 'localtime', ?) GROUP BY item_id",
        (f"-{int(days)} day",),
    ).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        d = conn.execute("SELECT CAST(julianday('now','localtime') - julianday(?) AS INT) AS n",
                         (r["d"],)).fetchone()
        out[int(r["item_id"])] = int(d["n"] or 0) if d else days
    return out


def pair_counts(path: list[dict]) -> list[tuple[int, int]]:
    ids = sorted(it["id"] for it in path)
    return [(ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))]


# ══════════════════════════════════════════════
# 打分
# ══════════════════════════════════════════════

def score_outfit(path: list[dict], temp: float, occasion: Optional[str],
                 weights: dict, worn: dict[int, int],
                 avoid_ids: set[int], rng: random.Random) -> dict:
    score = BASE_SCORE
    pros: list[str] = []
    cons: list[str] = []

    # ── 1 场合正式度 ──
    if occasion and occasion in rules.OCCASION_FORMALITY:
        lo, hi = rules.OCCASION_FORMALITY[occasion]
        bad = 0
        for it in path:
            f = rules._formality(it)
            if f < lo:
                bad += lo - f
            elif f > hi:
                bad += f - hi
        avg = bad / max(1, len(path))
        score -= avg * 5.0
        if avg == 0:
            pros.append(f"{occasion}场合，整套正式度刚好落在 {lo}–{hi}")

    # ── 2 色彩和谐 ──
    colors = [it.get("color_main") for it in path
              if not it.get("is_neutral") and it.get("color_main")]
    distinct = sorted(set(colors))
    has_neutral = any(it.get("is_neutral") for it in path)
    if len(distinct) == 0:
        score += 5
        pros.append("全中性色，最不容易出错，也最平淡")
    elif len(distinct) == 1:
        score += 12
        anchor = next((it["name"] for it in path if it.get("is_neutral")), None)
        pros.append(f"全身只有「{distinct[0]}」一个彩色" +
                    (f"，用{anchor}压住" if anchor else ""))
    elif len(distinct) == 2:
        score += 6
        pros.append(f"两个彩色（{'、'.join(distinct)}）互相呼应")
    else:
        score += 6 - 6 * (len(distinct) - 2)
        cons.append(f"身上 {len(distinct)} 个彩色（{'、'.join(distinct)}），有点花")
    if has_neutral and len(distinct) >= 1:
        score += 4

    # ── 3 温度 ──
    n_layers = len(path)
    if temp <= 5:
        outer = max((it.get("thickness") or 3) for it in path if it["slot"] == "L3") \
            if any(it["slot"] == "L3" for it in path) else 0
        if outer >= 4:
            score += 3
    if temp >= 26:
        if sum(it.get("thickness") or 3 for it in path) <= 8:
            score += 4

    # ── 4 新鲜度（穿过的降权，避免连着好几天同一套）──
    stale, fresh = [], []
    for it in path:
        if it["id"] in worn:
            d = worn[it["id"]]
            if d <= 1:
                score -= 18
                fresh.append(it["name"])
            elif d <= 6:
                score -= 9
                fresh.append(it["name"])
            else:
                stale.append((it["name"], d))
    if fresh:
        cons.append("最近刚穿过：" + "、".join(fresh))
    if stale:
        n, d = stale[0]
        pros.append(f"{n} 已经 {d} 天没穿了")

    # ── 5 学习权重（Beta 后验）──
    # 系数取 40 是有意的：一次「不喜欢」让后验从 0.5 掉到 0.33，扣 6.7 分，
    # 足以把它挤出前几名；但要攒到三次才会被真正压死。Beta 的意义就在这里 ——
    # 单次反馈不打死一件衣服，重复反馈才降权。
    iw = weights.get("item") or {}
    for it in path:
        p = iw.get(it["id"])
        if p is not None:
            score += (p - 0.5) * 40
            if p < 0.34:
                cons.append(f"{it['name']} 你跳过多次了")

    pw = weights.get("pair") or {}
    for a, b in pair_counts(path):
        p = pw.get((a, b))
        if p is not None and p < 0.5:
            score += (p - 0.5) * 30

    # ── 6 本命单品 ──
    sig = sum(1 for it in path if it.get("is_signature"))
    score += min(6, sig * 3)

    # ── 7 刚被否掉的件（本次「换一批」内不要重复出现）──
    hit = [it["name"] for it in path if it["id"] in avoid_ids]
    if hit:
        score -= 40 * len(hit)

    # ── 8 抖动：让「换一批」真的有变化 ──
    score += rng.uniform(-3.5, 3.5)

    return {"score": round(score, 2), "pros": pros, "cons": cons}


def make_reason(path: list[dict], scored: dict, temp: float,
                occasion: Optional[str]) -> str:
    """一句话说清这套为什么好。取自打分里权重最高的那几条。"""
    bits = list(scored["pros"][:2])
    layers = len(path)
    if not bits:
        bits.append(f"{temp}℃ 下 {layers} 层的合法组合，没有明显冲突")
    tail = f"{int(temp)}℃，{layers} 件"
    if occasion:
        tail += f"，{occasion}"
    return "；".join(bits) + "。（" + tail + "）"


# ══════════════════════════════════════════════
# 推荐
# ══════════════════════════════════════════════

def recommend(closet: graph.Closet, temp: float, occasion: Optional[str],
              weights: dict, worn: dict[int, int],
              avoid_ids: Optional[set[int]] = None,
              n: int = 3, seed: Optional[int] = None,
              enum_limit: int = 20000,
              exclude_sets: Optional[list[set[int]]] = None,
              anchor_id: Optional[int] = None) -> dict:
    """返回 {cores, variants, outfits}。outfits 是挑好并排好序的 n 套。

    `cores` 是核心组合数（最外层上装 × 下装 × 鞋，见 graph.core_of），
    `variants` 是叠穿变体总数。给用户看的那个数是 cores。
    """
    cores, variants = closet.core_and_variant(temp, occasion)

    collected: list[list[dict]] = []

    def hook(p: list[dict]) -> None:
        if len(collected) < enum_limit:
            collected.append(list(p))

    closet.count_paths(temp, occasion, path_hook=hook, limit=enum_limit)

    if not collected:
        return {"cores": cores, "variants": variants, "candidates": cores,
                "outfits": [], "truncated": False}

    # 「围绕这件搭」：只保留**含这一件**的路径。这是硬约束 ——
    # 用户明确说要以这件为核心，就别再端出不含它的组合。
    if anchor_id is not None:
        aid = int(anchor_id)
        collected = [p for p in collected if any(int(it["id"]) == aid for it in p)]
        if not collected:
            return {"cores": 0, "variants": 0, "candidates": 0, "outfits": [],
                    "exhausted": False, "truncated": False, "anchor_empty": True}
        # ⚠️ 计数必须**按锚定后的池子重算**：否则页头写着"252 种搭配"、
        # 实际只端得出几套，用户会以为坏了。
        cores = len({graph.core_of(p) for p in collected})
        variants = len(collected)

    rng = random.Random(seed)
    avoid_ids = avoid_ids or set()

    scored = []
    for p in collected:
        s = score_outfit(p, temp, occasion, weights, worn, avoid_ids, rng)
        scored.append((s["score"], s, p))
    scored.sort(key=lambda x: -x[0])

    # 「换一批」必须真的换：已经给你看过的组合要**硬排除**，不是扣分。
    bars = [set(x) for x in (exclude_sets or [])]

    def _too_similar(ids: set[int], other: set[int]) -> bool:
        """差 ≤1 件就算"同一套"（只是换了双鞋/换了个配饰）。"""
        return len(ids & other) > len(ids) - 2

    def _pick(live_bars: list[set[int]], strict: bool) -> list[dict]:
        out: list[dict] = []
        for sc, s, p in scored:
            ids = {it["id"] for it in p}
            if live_bars:
                if strict:
                    if any(_too_similar(ids, b) for b in live_bars):
                        continue
                elif any(ids == b for b in live_bars):
                    continue
            # 批内多样性：一套要跟同批已选的每一套都至少差 2 件
            if any(_too_similar(ids, {it["id"] for it in q["path"]}) for q in out):
                continue
            out.append({
                "score": sc,
                "path": p,
                "items": [public_item(it) for it in p],
                "reason": make_reason(p, s, temp, occasion),
                "notes": s["cons"][:2],
            })
            if len(out) >= n:
                break
        return out

    picked = _pick(bars, True)
    exhausted = False
    if not picked and bars:
        # 池子小的时候（旅行模式常只有几件）严格排除可能一件不剩。
        # 退一步：只排掉"完全一样"的；还不行就承认**真的排完了**。
        picked = _pick(bars, False)
    if not picked and bars:
        # 不返回空屏（那会让用户以为坏了）—— 原样再给一遍，但**明说排完了**
        picked = _pick([], True)
        exhausted = True

    return {"cores": cores, "variants": variants, "candidates": cores,
            "outfits": picked,
            "exhausted": exhausted,
            "truncated": variants >= graph.PATH_LIMIT}


def public_item(it: dict) -> dict:
    return {k: it.get(k) for k in
            ("id", "name", "slot", "category", "color_main", "color_hex", "is_neutral",
             "img_display", "formality", "thickness", "collar", "material", "notes",
             "is_signature", "purchase_price", "wear_count")}


# ══════════════════════════════════════════════
# 落库 / 反馈 / 穿着
# ══════════════════════════════════════════════

def save_outfits(conn, outfits: list[dict], temp: float, occasion: Optional[str],
                 source: str = "local") -> list[int]:
    ids: list[int] = []
    for o in outfits:
        cur = conn.execute(
            "INSERT INTO outfits (date, occasion, temp_actual, source, local_score) "
            "VALUES (date('now','localtime'), ?, ?, ?, ?)",
            (occasion, int(temp), source, o.get("score")),
        )
        oid = cur.lastrowid
        for it in o["items"]:
            conn.execute(
                "INSERT OR IGNORE INTO outfit_items (outfit_id, item_id, slot) VALUES (?,?,?)",
                (oid, it["id"], it["slot"]),
            )
        ids.append(oid)
    return ids


def apply_feedback(conn, outfit_id: int, gesture: str) -> dict:
    """手势 → 权重增量。这是整个学习闭环唯一的写入口。"""
    if gesture not in GESTURE_DELTA:
        raise ValueError(f"未知手势：{gesture}")
    col, delta = GESTURE_DELTA[gesture]

    rows = conn.execute(
        "SELECT item_id FROM outfit_items WHERE outfit_id = ?", (outfit_id,)
    ).fetchall()
    item_ids = [int(r["item_id"]) for r in rows]

    conn.execute("INSERT INTO feedback (outfit_id, gesture) VALUES (?, ?)",
                 (outfit_id, gesture))

    for iid in item_ids:
        # 增量必须同时写进 VALUES 和 DO UPDATE：
        # INSERT ... ON CONFLICT DO UPDATE 在**插入新行时不执行 DO UPDATE 分支**，
        # 只写 DO UPDATE 的话，一件衣服的第一次反馈会被静默吞掉，beta 永远停在 1.0，
        # 学习闭环根本启动不了。（踩过。）
        a0, b0 = (delta, 0.0) if col == "alpha" else (0.0, delta)
        conn.execute(
            "INSERT INTO w_item (item_id, alpha, beta, last_seen) "
            "VALUES (?, 1.0 + ?, 1.0 + ?, datetime('now','localtime')) "
            f"ON CONFLICT(item_id) DO UPDATE SET {col} = {col} + ?, "
            "last_seen = datetime('now','localtime')",
            (iid, a0, b0, delta),
        )

    # 成对关系：单独都接受、连在一起总被拒 → 冲突在这一对，不在任何一件上。
    # 这是滑动反馈里最值钱的信息，任何现成 App 都没做。
    for a, b in pair_counts([{"id": i} for i in item_ids]):
        pa, pb = (delta * 0.5, 0.0) if col == "alpha" else (0.0, delta * 0.5)
        conn.execute(
            "INSERT INTO w_pair (item_a_id, item_b_id, alpha, beta) "
            "VALUES (?, ?, 1.0 + ?, 1.0 + ?) "
            "ON CONFLICT(item_a_id, item_b_id) DO UPDATE SET "
            f"{col} = {col} + ?",
            (a, b, pa, pb, delta * 0.5),
        )

    if gesture in ("accepted", "auto_worn"):
        conn.execute("UPDATE outfits SET is_worn = 1 WHERE id = ?", (outfit_id,))
        for iid in item_ids:
            conn.execute(
                "INSERT INTO wear_log (item_id, date, outfit_id) "
                "VALUES (?, date('now','localtime'), ?)", (iid, outfit_id),
            )
            conn.execute(
                "UPDATE items SET wear_count = COALESCE(wear_count,0) + 1, "
                "updated_at = datetime('now','localtime') WHERE id = ?", (iid,),
            )

    conn.commit()
    return {"ok": True, "items": item_ids, "gesture": gesture}


def recent_reject_ids(conn, days: int = 3) -> set[int]:
    """最近被拒/跳过的单品。立刻影响下一次「换一批」。"""
    rows = conn.execute(
        "SELECT oi.item_id FROM feedback f "
        "JOIN outfit_items oi ON oi.outfit_id = f.outfit_id "
        "WHERE f.gesture IN ('rejected','skipped') "
        "AND f.created_at >= datetime('now','localtime', ?) "
        "GROUP BY oi.item_id HAVING COUNT(*) >= 2",
        (f"-{int(days)} day",),
    ).fetchall()
    return {int(r["item_id"]) for r in rows}
