"""买前模拟：候选件放在衣橱外面做模拟，算 ΔN / 救活数 / 替代率 / 决策门槛。

见 docs/02-推荐引擎算法流程.md 阶段 9。

三条最容易做错的：
  1. 候选件绝不参与衣橱统计（stage='candidate'）
  2. ΔN 单独用会骗人 —— 空洞效应必须修正
  3. 没有决策门槛，每件候选都能新增组合，你会觉得什么都该买

实现上的两个口径决定：
  · ΔN 只在「主温度」上算（默认 20℃）。跨季节累加会让数字虚高到几百套，
    对决策毫无意义 —— 覆盖了几个季节是另一个指标。
  · 决策门槛用「相对增长率」而不是绝对套数。衣橱越大，同样的衣服绝对增量越大，
    绝对阈值会失效。
"""

from __future__ import annotations

import colorsys
import json
from collections import defaultdict
from typing import Optional

from . import graph, rules
from .graph import TEMP_BANDS, Closet

SLOT_NAMES = {
    "L1": "内搭", "L2": "中间层", "L3": "外层",
    "L4": "下装", "L5": "鞋", "ACC": "配饰",
}

BAND_SEASON = {0: "冬", 8: "冬", 16: "春秋", 24: "夏", 30: "夏"}
SEASON_ORDER = ["春", "春秋", "夏", "秋", "冬"]

# 决策门槛（相对增长率）
GROWTH_STRONG = 0.12    # 显著增长
GROWTH_WEAK = 0.03      # 弱到可以忽略
MAIN_TEMP_DEFAULT = 20


def _formality(it: dict) -> int:
    v = it.get("formality")
    return 3 if v is None else int(v)


def _thickness(it: dict) -> int:
    v = it.get("thickness")
    return 3 if v is None else int(v)


def _temp_span(it: dict) -> tuple[int, int]:
    lo = -10 if it.get("temp_min") is None else int(it["temp_min"])
    hi = 40 if it.get("temp_max") is None else int(it["temp_max"])
    return lo, hi


def _temp_overlap_ratio(a: dict, b: dict) -> float:
    la, ha = _temp_span(a)
    lb, hb = _temp_span(b)
    inter = max(0, min(ha, hb) - max(la, lb))
    union = max(ha, hb) - min(la, lb)
    return inter / union if union > 0 else 0.0


# ══════════════════════════════════════════════
# 颜色相似度（判重不能只看"都是中性色"）
# ══════════════════════════════════════════════

def _hsv(it: dict) -> Optional[tuple[float, float, float]]:
    return rules.hsv_from_hex(it.get("color_hex"))


def colors_alike(a: dict, b: dict) -> bool:
    """两个颜色是不是"看起来差不多"。

    只看"都算中性色"会把藏蓝和卡其判成重复 —— 那是完全不同的两条裤子。
    无彩色比明度，有彩色比色相 + 明度。
    """
    ha, hb = _hsv(a), _hsv(b)
    if not ha or not hb:
        # 没有色值就退回按名字比
        ca, cb = a.get("color_main"), b.get("color_main")
        return bool(ca) and ca == cb

    neutral_a = ha[1] < 0.18
    neutral_b = hb[1] < 0.18
    if neutral_a and neutral_b:
        return abs(ha[2] - hb[2]) < 0.22
    if neutral_a != neutral_b:
        return False
    return rules.hue_delta(ha[0], hb[0]) < 30 and abs(ha[2] - hb[2]) < 0.25


# ══════════════════════════════════════════════
# 替代率
# ══════════════════════════════════════════════

def find_duplicates(candidate: dict, owned: list[dict]) -> list[dict]:
    """候选件能顶掉的同槽位现有单品。

    判重条件（全部满足）：
      · 同槽位
      · 正式度差 ≤ 1
      · 厚度差 ≤ 1
      · 温度区间重叠率 ≥ 0.5
      · 颜色看起来差不多
    """
    dup = []
    for e in owned:
        if e["slot"] != candidate["slot"]:
            continue
        if abs(_formality(candidate) - _formality(e)) > 1:
            continue
        if abs(_thickness(candidate) - _thickness(e)) > 1:
            continue
        if _temp_overlap_ratio(candidate, e) < 0.5:
            continue
        if colors_alike(candidate, e):
            dup.append(e)
    return dup


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def evaluate(candidate: dict, owned: list[dict],
             temp: Optional[int] = None,
             occasion: Optional[str] = None,
             season_temps: Optional[list[int]] = None) -> dict:
    """候选件全量评估。owned 只传 stage='owned' 的在库件。

    temp 不传时取候选件自身适用温度区间的中点 —— 用固定的 20℃ 评估一件
    羽绒服是没有意义的。
    """
    temp = _pick_temp(candidate, temp)
    season_temps = season_temps or TEMP_BANDS
    slot = candidate["slot"]
    if slot == "ACC":
        return {"error": "配饰暂不支持买前模拟"}

    base = Closet(owned)
    with_cand = Closet(list(owned) + [candidate])

    # ── 主温度：ΔN 与搭档 ──
    # ΔN 的单位是**核心组合**（最外层上装 × 下装 × 鞋），叠穿变体不算新增 ——
    # 见 graph.core_of。用路径数会让"多一件打底衫"看起来新增几十套。
    base_n = base.count_cores(temp, occasion)
    per_main: dict[int, int] = defaultdict(int)
    hits = {"n": 0}
    main_cores: set[tuple] = set()

    def hook_main(path: list[dict]) -> None:
        hits["n"] += 1
        main_cores.add(graph.core_of(path))
        for it in path:
            per_main[it["id"]] += 1

    with_cand.count_paths(temp, occasion, must={slot: candidate}, path_hook=hook_main)
    delta_n = len(main_cores)
    with_n = base_n + delta_n
    growth = (delta_n / base_n) if base_n else 0.0

    # ── 跨温度带：覆盖季节 + 救活数 ──
    base_alive: set[int] = set()
    for t in season_temps:
        bc = graph.participating_counts(base, t, occasion)
        base_alive.update(i for i, c in bc.items() if c > 0)
    base_islands = {it["id"] for it in base.items} - base_alive

    delta_by_t: dict[int, int] = {}
    revived: set[int] = set()
    for t in season_temps:
        # 该温度下这一层不存在（比如 25℃ 穿中层），候选件根本进不了路径。
        # 若不拦住，枚举出来的会是不含候选件的普通搭配，ΔN 假性为正。
        if slot not in graph.layers_for_temp(t):
            delta_by_t[t] = 0
            continue

        per: dict[int, int] = defaultdict(int)
        cnt = {"n": 0}
        cores_t: set[tuple] = set()

        def hook(path: list[dict], _per=per, _cnt=cnt, _cores=cores_t) -> None:
            _cnt["n"] += 1
            _cores.add(graph.core_of(path))
            for it in path:
                _per[it["id"]] += 1

        with_cand.count_paths(t, occasion, must={slot: candidate}, path_hook=hook)
        delta_by_t[t] = len(cores_t)
        revived.update(i for i in base_islands if per.get(i, 0) > 0)

    seasons = sorted({BAND_SEASON.get(t, "其他") for t, d in delta_by_t.items() if d > 0},
                     key=lambda s: SEASON_ORDER.index(s) if s in SEASON_ORDER else 99)

    # ── 替代率与净收益 ──
    dup = find_duplicates(candidate, owned)
    replace_rate = len(dup)
    replace_ids: list[int] = []
    worst_delta = 0
    if dup:
        ranked = sorted(((graph.delta_n(base, e, temp, occasion, base_n), e) for e in dup),
                        key=lambda x: -x[0])
        replace_ids = [e["id"] for _, e in ranked]
        worst_delta = ranked[0][0]
    net = delta_n - worst_delta

    hole_flag = not any(it["slot"] == slot for it in owned)

    # ── 搭档 / 冲突（冲突排除同槽位，那是替代关系不是冲突）──
    partners, conflicts = [], []
    for it in owned:
        if it["slot"] == "ACC":
            continue
        n = per_main.get(it["id"], 0)
        if n > 0:
            partners.append({"id": it["id"], "name": it["name"], "slot": it["slot"],
                             "category": it.get("category"), "count": n})
        elif it["slot"] != slot:
            reason = with_cand.pair(candidate, it)
            if reason:
                conflicts.append({"id": it["id"], "name": it["name"], "slot": it["slot"],
                                  "category": it.get("category"), "reason": reason})
    partners.sort(key=lambda p: -p["count"])
    conflicts.sort(key=lambda c: -_layer_first(c["slot"]))

    result = {
        "candidate": {
            "id": candidate["id"], "name": candidate["name"],
            "slot": candidate["slot"], "category": candidate.get("category"),
        },
        "temp": temp,
        "base_n": base_n,
        "with_n": with_n,
        "delta_n": delta_n,
        "growth": round(growth, 4),
        "revived_count": len(revived),
        "revival_ids": sorted(revived),
        "revivals": [{"id": it["id"], "name": it["name"],
                      "category": it.get("category"), "slot": it["slot"]}
                     for it in base.items if it["id"] in revived],
        "seasons": seasons,
        "season_count": len(seasons),
        "replace_rate": replace_rate,
        "replace_ids": replace_ids,
        "replace_names": [it["name"] for it in owned if it["id"] in replace_ids],
        "net_gain": net,
        "hole_flag": hole_flag,
        "partners": partners,
        "partner_total": len(partners),
        "conflicts": conflicts,
        "delta_by_temp": delta_by_t,
        "notes": _notes(candidate, owned),
    }
    result["verdict"], result["verdict_text"] = _decide(result, slot)
    return result


def _layer_first(slot: str) -> int:
    # 跟 rules.LAYER_ORDER 保持同序：L4I（秋裤）在下装之前
    order = ["L1", "L2", "L3", "L4I", "L4", "L5"]
    return order.index(slot) if slot in order else 9


def _mid_temp(candidate: dict) -> int:
    lo, hi = _temp_span(candidate)
    lo, hi = max(lo, -5), min(hi, 35)
    if hi < lo:
        hi = lo
    return int(round((lo + hi) / 2))


def _pick_temp(candidate: dict, requested: Optional[int]) -> int:
    """选主评估温度。

    优先用候选件自身适用温度的中点 —— 羽绒服就该在 5℃ 评估，不在 20℃。
    但若这个温度下候选件的层位根本不存在（外层标了 15~30℃，而 25℃ 不穿外层），
    就退到最近的、含该层的温度。
    """
    if requested is not None:
        return requested

    slot = candidate["slot"]
    mid = _mid_temp(candidate)
    if slot in graph.layers_for_temp(mid):
        return mid
    for t in (14, 8, 20, 5, 16, 0, 24, 11, 30, -5):
        if slot in graph.layers_for_temp(t):
            return t
    return mid


def _slot_name(slot: str) -> str:
    return SLOT_NAMES.get(slot, slot)


def _decide(r: dict, slot: str) -> tuple[str, str]:
    """决策门槛。用相对增长率，不用绝对套数 —— 衣橱越大绝对增量越大，绝对阈值会失效。"""
    d = r["delta_n"]
    g = r["growth"]
    rev = r["revived_count"]
    rep = r["replace_rate"]
    seasons = r["season_count"]
    net = r["net_gain"]
    name = _slot_name(slot)
    pct = round(g * 100)

    if r["hole_flag"]:
        if seasons >= 2:
            return ("compare",
                    f"你缺的是「{name}」这个位置，不是这一件 —— "
                    f"ΔN {d} 里很大一部分来自空槽，不是它的功劳。"
                    f"一次录 2–3 件同品类候选，用批次横向比再决定。")
        return ("skip",
                f"「{name}」这个位置是空的，但这件只覆盖 {seasons} 个温度带 —— "
                f"先别急，等找到更通用的再来。")

    if g >= GROWTH_STRONG and rep == 0 and rev >= 1:
        return ("buy", f"组合空间 +{pct}%，替代率 0，还救活 {rev} 件孤岛 → 建议买。")

    if g >= GROWTH_STRONG and rep == 0:
        return ("buy", f"组合空间 +{pct}%，不顶掉任何现有单品 → 建议买。")

    if g >= GROWTH_STRONG and net > 0:
        return ("buy", f"组合空间 +{pct}%，替代 {rep} 件后净赚 {net} 套 → "
                       f"可以买，记得执行「进一件出一件」。")

    if g < GROWTH_WEAK:
        return ("skip", f"组合空间只 +{pct}%（{d} 套）—— 它和你现有衣服的搭配余地太小，不买。")

    if rep >= 1 and net <= 0:
        return ("skip", f"和现有 {rep} 件功能重复，以旧换新后净收益 {net} 套 —— "
                        f"顶不掉旧的，等于白买一件。")

    return ("compare",
            f"组合空间 +{pct}%、替代率 {rep}、覆盖 {seasons} 个温度带 —— "
            f"边缘情况，看你还缺不缺这一件。")


def _notes(candidate: dict, owned: Optional[list[dict]] = None) -> list[str]:
    """候选件的数据质量与结构提示。"""
    out: list[str] = []

    if owned is not None:
        same_cat = [it for it in owned
                    if it["slot"] == candidate["slot"]
                    and it.get("category") == candidate.get("category")]
        if same_cat:
            names = "、".join(it["name"] for it in same_cat[:4])
            out.append(f"你已经有 {len(same_cat)} 件同类（{candidate.get('category')}）："
                       f"{names} —— 它们颜色不同所以不算重复，但功能是重叠的，替不替代你自己判断。")

    if not candidate.get("color_hex"):
        out.append("主色没填，色彩规则（R4 撞色）不会生效 —— 结论会偏乐观。")

    mid = _mid_temp(candidate)
    if candidate["slot"] not in graph.layers_for_temp(mid):
        lo, hi = _temp_span(candidate)
        out.append(f"你填的温度区间（{lo}~{hi}℃）里，「{_slot_name(candidate['slot'])}」"
                   f"这一层不会出现 —— 温度可能填错了，已经换温度评估。")

    conf_raw = candidate.get("field_conf")
    if conf_raw:
        try:
            conf = json.loads(conf_raw) if isinstance(conf_raw, str) else conf_raw
            low = [k for k, v in conf.items() if isinstance(v, (int, float)) and v < 0.7]
            if low:
                out.append("置信度偏低的字段：" + "、".join(low) + " —— 核实后重算一次。")
        except Exception:
            pass

    if candidate.get("source_url"):
        out.append("主色可能取自商品图（有打光偏差）—— 决定买之前用自然光拍一次实物核验。")

    if not candidate.get("formality"):
        out.append("正式度没填，已按默认 3 处理。")

    return out
