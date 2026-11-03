"""搭配图引擎：按温度决定层数，DFS 剪枝枚举合法路径，算衣橱指标。

见 docs/02-推荐引擎算法流程.md 阶段 1、3、8。

关键点：不枚举全部组合，只枚举通过硬规则的路径 —— 这才是"有效搭配"的真义。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict, defaultdict
from typing import Callable, Iterable, Optional

from . import rules

# 代表温度带（用于跨季节统计）
# **采样网格**（不是"温度带"！）：算全年累计、效率曲线时扫这几个温度。
# 名字是历史遗留 —— 2026-09-23 之前它真是"温度带的分档阈值"，
# 换成「26℃穿衣法则」之后层数不再由阈值决定，它退化成一张采样表。
# 注意 0℃ / 8℃ 对这套衣橱会返回 0 个核心（真实冬季缺口），那是**如实显示**。
TEMP_BANDS = [0, 8, 16, 24, 30]

# 路径枚举上限，防止配置异常时爆炸
PATH_LIMIT = 300_000

# 层名（只在引擎内部用于提示文案，避免反向依赖 catalog）
SLOT_LABEL = {"L1": "内搭", "L2": "中间层", "L3": "外层",
              "L4I": "内穿下装", "L4": "下装", "L5": "鞋", "ACC": "配饰"}


def layer_plan(temp: float) -> tuple[list[str], list[str], int]:
    """哪些层是必须的、哪些可选、最多能叠几层。

    返回 (必需层, 可选层, 叠层上限)。

    ⚠️ **2026-09-23 大改**：以前是"温度阶梯"—— `≥24℃` 不许有第二层、
    `<14℃` 把中间层/外层改成**必需**。实测那是一条**悬崖**：
    24→23℃ 核心组合数 +50%、14→13℃ **−81%**（上衣栈从 4 种塌成 1 种），
    而且**规则和单品数据打架**（衬衫标着 10~25℃，24℃ 却不许出现第二层）。

    现在：**必需层永远只有 L1/L4/L5**，中间层/外层一律"可选"——
    "今天穿不穿外套"变成**算出来的**（由 `rules.stack_warm_ok` 判够不够暖），
    而不是日历上写死的。层数不再由温度带决定，而是"这套衣服的保暖值够不够"。

    ⚠️ 连"28℃ 以上不考虑中间层"这种**单一**阈值也不要留：
    那又是在日历上切一刀。真正管这件事的是**那件衣服自己的上限**
    （夹克标 0~22℃，30℃ 那天它在 `item_temp_ok` 里就被否了）——
    留一个全局阈值只会在 27.9/28.0℃ 之间凭空造出一条缝。
    """
    opt: list[str] = ["L2", "L3"]
    if temp < 14:
        # L4I（秋裤）只在冷天进候选。它永远可选 —— 0℃ 不穿秋裤也合法。
        opt = opt + ["L4I"]
    return ["L1", "L4", "L5"], opt, 3


def layers_for_temp(temp: float) -> list[str]:
    """该温度下**可能出现**的所有层（必需 + 可选）。

    语义是"这个温度下有没有机会穿到这一层"，用于判断某件单品在这个温度
    有没有戏（买前模拟的层位检查）。不等于"必须穿"。
    """
    req, opt, _ = layer_plan(temp)
    return [s for s in rules.LAYER_ORDER if s in set(req) | set(opt)]


# 参与「能不能搭」判定的字段 —— 缓存键必须覆盖它们，不能只用 id。
# **加了新的判定字段（比如 tags）一定要加到这里**，否则改了它搭配不变 ——
# 同一个坑（指纹不覆盖）已经吃过一次了。
_FP_FIELDS = ("id", "slot", "category", "temp_min", "temp_max", "warmth",
              "formality",
              "thickness", "color_hex", "is_neutral", "collar", "sleeve",
              "length_band", "fit", "material", "pattern", "tags",
              "status", "stage")

# 核心组合枚举结果的进程内缓存。今日页拖动温度滑杆、体检页扫 5 个温度带
# 都会反复问同样的问题，不缓存就每次重枚举一遍。
_CORE_CACHE: "OrderedDict[tuple, tuple[int, int]]" = OrderedDict()
_CORE_CACHE_MAX = 96


def closet_fingerprint(items: Iterable[dict]) -> str:
    """集合指纹，用作缓存键。

    **必须把影响搭配判定的字段都算进去，不能只用 id** ——
    用户改掉某件的温度区间或颜色之后，只看 id 的缓存会一直返回旧结果，
    表现为"改了属性但搭配没变"。
    """
    h = hashlib.blake2b(digest_size=12)
    for it in sorted(items, key=lambda x: x.get("id") or 0):
        for k in _FP_FIELDS:
            h.update(str(it.get(k)).encode())
            h.update(b"\x1f")
        h.update(b"\x1e")
    return h.hexdigest()


# ══════════════════════════════════════════════
# 核心组合：把"叠穿变体"折回同一个核心
# ══════════════════════════════════════════════

def core_of(path: list[dict]) -> tuple:
    """一套搭配的「核心」= **最外层上装 × 下装 × 鞋**。

    叠穿多出来的层算同一个核心的变体：
      · T恤 + 皮衣 + 裤 + 鞋
      · T恤 + 卫衣 + 皮衣 + 裤 + 鞋
    **是同一个核心** —— 穿出门是同一个样子，多的那件卫衣被外套盖住了。

    而「T恤 + 裤 + 鞋」（不穿外套）是**另一个核心** —— 看起来确实不一样。
    换一件别的白 T 当打底也不产生新核心（最外层没变）。

    为什么这样定义：层数可变之后，路径数是**乘法**膨胀的
    （28 件衣橱 20℃ 从 320 套涨到 2240 套），这个数字就不再回答
    "最少几件衣服搭出几种样子"了。折成核心之后它才有意义。
    """
    top = None
    top_i = -1
    bottom = None
    shoes = None
    for it in path:
        slot = it["slot"]
        if slot in rules.STACK_LAYERS:
            i = rules.LAYER_ORDER.index(slot)
            if i > top_i:
                top_i, top = i, it
        elif slot == "L4" and bottom is None:
            bottom = it
        elif slot == "L5" and shoes is None:
            shoes = it
    return (top["id"] if top else None,
            bottom["id"] if bottom else None,
            shoes["id"] if shoes else None)


class Closet:
    """一套依品集合 + 成对兼容缓存。实例内缓存跨调用保留。

    `comfort` 是「26℃穿衣法则」里的**体感舒适温度**（`rules.DEFAULT_COMFORT`
    是兜底，真实值来自 `prefs.comfort()`）。放在 Closet 上而不是做成全局：
    它必须能被显式传参，否则单测没法构造"怕冷的人"和"怕热的人"。
    """

    def __init__(self, items: Iterable[dict], comfort: Optional[float] = None):
        self.items: list[dict] = [it for it in items if it["slot"] != "ACC"]
        self.by_slot: dict[str, list[dict]] = defaultdict(list)
        for it in self.items:
            self.by_slot[it["slot"]].append(it)
        self._pcache: dict[tuple[int, int], Optional[str]] = {}
        self._fp = closet_fingerprint(self.items)
        self.comfort = rules.comfort_of(comfort)

    # ── 成对兼容（带缓存）──
    def pair(self, a: dict, b: dict) -> Optional[str]:
        key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
        if key in self._pcache:
            return self._pcache[key]
        adjacent = (a["slot"] in rules.STACK_LAYERS
                    and b["slot"] in rules.STACK_LAYERS)
        reason = rules.pair_ok(a, b, adjacent=adjacent)
        self._pcache[key] = reason
        return reason

    # ── 层候选（先按温度筛，冷天再强制厚外层）──
    def _candidates(self, slot: str, temp: float) -> list[dict]:
        """该层在这个温度下**真正能穿**的单品。

        温度过滤必须在这里做，不能只在最后校验。原来放在叶子校验（outfit_ok）时，
        DFS 会把"最高只到 20℃ 的外套"排进 23℃ 的每条路径，然后**每条都在叶子被否**，
        结果不是"少几套"，而是直接 0 套、首页空白。提前筛还有个白拿的好处：
        搜索空间小一大截，5 个温度带全算完仍然是毫秒级。
        """
        pool = self.by_slot.get(slot) or []
        pool = [it for it in pool if rules.temp_ok(it, temp)]
        # ⚠️ 这里原来有一条 `if temp < 5 and slot == "L3"`：5℃ 以下只留 thickness ≥ 4
        # 的外套。那是又一条藏在候选生成里的"差一度"阈值（而且不在温度口径清单上），
        # 换成 26℃法则之后由 `stack_warm_ok` 负责"够不够暖"，**删掉**。
        return pool

    # ── DFS 枚举 ──
    def count_paths(self, temp: float, occasion: Optional[str] = None,
                    must: Optional[dict[str, dict]] = None,
                    path_hook: Optional[Callable[[list[dict]], None]] = None,
                    limit: int = PATH_LIMIT) -> int:
        """返回合法路径数。path_hook 每条合法路径调一次（不落全量列表，省内存）。

        层数由 temperature 决定（见 layer_plan）：
          · 必需层（内搭/下装/鞋，冬天还有中间层和外层）缺合格单品 → 这条分支直接死
          · 可选层（春秋的中间层/外层）可以不放 —— 所以"今天不穿外套"是合法搭配
          · 叠层数超过该温度的上限 → 剪枝（20℃ 不该出现三件叠穿）
        """
        required, optional, max_stack = layer_plan(temp)
        required_set = set(required)
        want = required_set | set(optional)
        slots = [s for s in rules.LAYER_ORDER if s in want]
        must = must or {}
        total = 0
        path: list[dict] = []

        def dfs(i: int, stack_n: int) -> None:
            nonlocal total
            if total >= limit or stack_n > max_stack:
                return
            if i == len(slots):
                if rules.outfit_ok(path, temp, occasion, self.comfort) is None:
                    total += 1
                    if path_hook:
                        path_hook(path)
                return

            slot = slots[i]
            forced = slot in must
            if forced:
                candidates = [must[slot]]
            else:
                candidates = self._candidates(slot, temp)
                # 可选层可以整个不放 —— 先走"不放"这条分支
                if slot not in required_set:
                    dfs(i + 1, stack_n)
                if not candidates:
                    # 必需层没有任何合格单品（比如没有能穿到 0℃ 的外套）
                    # → 这条分支不合法。不能默默跳过，否则冬天会推出"没穿外套"的方案。
                    return

            for it in candidates:
                ok = True
                for prev in path:
                    if self.pair(prev, it) is not None:
                        ok = False
                        break
                if not ok:
                    continue
                path.append(it)
                dfs(i + 1, stack_n + (1 if slot in rules.STACK_LAYERS else 0))
                path.pop()

        dfs(0, 0)
        return total

    # ── 空态诊断 ──
    def diagnose(self, temp: float, occasion: Optional[str] = None) -> dict:
        """算不出搭配时，说清楚卡在哪一层。

        首页原来只会说"多半是某一层缺件"，用户还得自己猜。
        真正的信息是：**哪一层、在这一层里能穿的有几件、库里总共有几件。**
        """
        required, optional, max_stack = layer_plan(temp)
        rows = []
        blocked = []
        for slot in rules.LAYER_ORDER:
            req = slot in required
            if not req and slot not in optional:
                continue
            pool = self.by_slot.get(slot) or []
            ok = self._candidates(slot, temp)
            row = {"slot": slot, "name": SLOT_LABEL.get(slot, slot),
                   "required": req, "pool": len(pool), "eligible": len(ok)}
            rows.append(row)
            if req and not ok:
                blocked.append(row)

        # 「26℃穿衣法则」下最常见的空态：不是缺某一层，是**整套都不够暖**。
        # ⚠️ 这条必须排在 `blocked`（某一层缺件）**前面** —— 顺序反了会说出
        #    很荒谬的话。实例：舒适线降到 19℃ 后，探针温度落到了 4℃，
        #    4 双运动鞋（区间 5~30℃）集体出局 → 旧顺序先报
        #    「你有 4 件「鞋」，但没有一件能穿到 4℃」，
        #    而真正的原因是"所有上装叠满也只有 10℃，根本差 5℃"。
        #    后者才是要买的东西（一件真外套），前者只是同一件事的第二个症状。
        need = rules.comfort_of(self.comfort) - temp
        best = self.max_top_warmth()
        # `best > 0`：一件上装都没有时别硬说"缺外套"，那是真的缺那一层。
        too_cold = need > best > 0

        if too_cold:
            kind = "too_cold"
            hint = (f"{temp:g}℃ 下需要上装提供 {need:.0f}℃ 保暖"
                    f"（舒适线 {rules.comfort_of(self.comfort):g}℃），"
                    f"而你所有上衣叠满也只有 {best:g}℃ —— "
                    f"不是没有衣服，是缺一件真正的外套。")
            if blocked:
                # 次要事实也要说，别瞒着（4℃ 连鞋都出不了门，只报"缺外套"会让人
                # 买完外套再撞一次墙）。但主因已经说清了，不再改 kind。
                names = "、".join(f"「{b['name']}」" for b in blocked)
                hint += f"另外这个温度下 {names} 也全都穿不了。"
        elif blocked:
            if len(blocked) > 1:
                names = "、".join(b["name"] for b in blocked)
                hint = f"{temp:g}℃ 下这几层都没有合格单品：{names}。"
            else:
                head = blocked[0]
                if head["pool"] == 0:
                    hint = f"你还没有「{head['name']}」这一类。"
                else:
                    hint = (f"你有 {head['pool']} 件「{head['name']}」，"
                            f"但没有一件能穿到 {temp:g}℃。")
            kind = "missing_layer"
        elif occasion and self.count_paths(temp, None, limit=1) > 0:
            # 同温度不限场合能出，加上场合就出不来 → 卡在正式度上
            lo, hi = rules.OCCASION_FORMALITY.get(occasion, (1, 5))
            hint = (f"卡在场合上：这些衣服能组出搭配，但没有一套的正式度落在"
                    f"「{occasion}」要求的 {lo}~{hi} 区间里。")
            kind = "occasion"
        else:
            hint = (f"{temp:g}℃ 下每一层都有单品，但它们两两之间过不了硬规则"
                    f"（同槽位、温度不相交、正式度差过大、撞色）。")
            kind = "conflict"
        return {"temp": temp, "occasion": occasion, "max_stack": max_stack,
                "kind": kind, "layers": rows,
                "blocked": [b["slot"] for b in blocked], "hint": hint,
                "comfort": rules.comfort_of(self.comfort),
                "max_warmth": self.max_top_warmth()}

    def max_top_warmth(self) -> float:
        """把所有上装叠满能达到的保暖值（**上界**，不判成对规则）。

        用途只有一个：判"这套衣橱是不是根本不够暖"。所以宁可给乐观的上界 ——
        上界都够不着需求，才敢断定"缺外套"。
        L1 里标了 `solo_only`（只能单穿）的不算 —— 它们当不了内层。
        """
        def best(slot: str, exclude_solo: bool = False) -> float:
            pool = self.by_slot.get(slot) or []
            vals = [rules.warmth(it) for it in pool
                    if not (exclude_solo and rules.TAG_SOLO in rules.item_tags(it))]
            return max(vals, default=0.0)

        return best("L1", exclude_solo=True) + best("L2") + best("L3")

    def first_outfit(self, temp: float, occasion: Optional[str] = None) -> Optional[list[dict]]:
        """随便找一条合法路径（用于快速判断某件能否参与搭配）。"""
        found: list[list[dict]] = []

        def hook(p: list[dict]) -> None:
            if not found:
                found.append(list(p))

        self.count_paths(temp, occasion, path_hook=hook, limit=1)
        return found[0] if found else None

    # ── 核心组合（叠穿变体折叠后的那个数）──
    def core_and_variant(self, temp: float, occasion: Optional[str] = None,
                         must: Optional[dict[str, dict]] = None,
                         limit: int = PATH_LIMIT) -> tuple[int, int]:
        """返回 (核心组合数, 叠穿变体总数)。

        带进程内缓存 —— 今日页拖温度滑杆、体检页扫 5 个温度带都会重复问同一个问题。
        缓存键含集合指纹（`closet_fingerprint`），改单品属性后自动失效。
        """
        mk = tuple(sorted((k, v["id"]) for k, v in (must or {}).items()))
        # ⚠️ 缓存键必须含 comfort —— 改了「体感舒适温度」而键里没有它的话，
        # 会一路返回旧结果，表现是"改了设置但推荐没变"（同一个坑：指纹不覆盖）。
        key = (self._fp, round(float(temp), 2), occasion or "", mk, limit,
               self.comfort)
        hit = _CORE_CACHE.get(key)
        if hit is not None:
            _CORE_CACHE.move_to_end(key)
            return hit

        seen: set[tuple] = set()
        n = [0]

        def hook(p: list[dict]) -> None:
            n[0] += 1
            seen.add(core_of(p))

        self.count_paths(temp, occasion, must=must, path_hook=hook, limit=limit)
        res = (len(seen), n[0])

        _CORE_CACHE[key] = res
        while len(_CORE_CACHE) > _CORE_CACHE_MAX:
            _CORE_CACHE.popitem(last=False)
        return res

    def count_cores(self, temp: float, occasion: Optional[str] = None,
                    must: Optional[dict[str, dict]] = None,
                    limit: int = PATH_LIMIT) -> int:
        return self.core_and_variant(temp, occasion, must=must, limit=limit)[0]

    def core_capacity(self, temp: float) -> int:
        """该温度下核心组合的理论上限，用来算「组合密度」。

        **顶层 = 可能成为"最外层上装"的那些件。** 核心的定义是
        「最外层上装 × 下装 × 鞋」，所以 L1/L2/L3 都可能是顶层
        （只穿 T 恤时顶层就是 L1；T恤+夹克 时顶层是夹克）——
        这三层的候选全算进来才是真正的上界。

        ⚠️ 这个上界**不考虑"够不够暖"**，只考虑"能不能穿"。所以
        「26℃穿衣法则」下天冷时会看到"上限不低、密度 0%"——
        那不是矛盾，是"候选池里有件、但没有一套够暖"，正是该给用户看的信息。

        密度的意义：**它是尺度无关的** —— 衣服越多它不会自动变大，
        只有衣服之间真的更搭得上才会变大。买前模拟里它就是"这件会不会跟我现有的衣服打架"。
        """
        tops: list[dict] = []
        for slot in ("L1", "L2", "L3"):
            tops += self._candidates(slot, temp)
        return (len(tops) * len(self._candidates("L4", temp))
                * len(self._candidates("L5", temp)))


# ══════════════════════════════════════════════
# 派生指标
# ══════════════════════════════════════════════

def participating_counts(closet: Closet, temp: float,
                         occasion: Optional[str] = None) -> dict[int, int]:
    """每件单品在给定温度/场合下参与了几个搭配。0 = 孤岛。"""
    counts: dict[int, int] = defaultdict(int)

    def hook(path: list[dict]) -> None:
        for it in path:
            counts[it["id"]] += 1

    closet.count_paths(temp, occasion, path_hook=hook)
    return counts


def island_items(closet: Closet, temps: Optional[list[int]] = None) -> list[dict]:
    """全季孤岛件：在所有代表温度带上都参与不了任何搭配。这些才是该扔的。"""
    temps = temps or TEMP_BANDS
    alive: set[int] = set()
    for t in temps:
        counts = participating_counts(closet, t)
        alive.update(i for i, c in counts.items() if c > 0)
    return [it for it in closet.items if it["id"] not in alive]


def inactive_now(closet: Closet, temp: float,
                 occasion: Optional[str] = None) -> list[dict]:
    """当前温度下参与不了搭配的件。

    这不等于该扔 —— 可能只是这个季节穿不上（羽绒服在七月）。
    必须和全季孤岛分开，否则你会在夏天扔掉大衣。
    """
    counts = participating_counts(closet, temp, occasion)
    return [it for it in closet.items if counts.get(it["id"], 0) == 0]


def delta_n(closet: Closet, item: dict, temp: float,
            occasion: Optional[str] = None, base_n: Optional[int] = None) -> int:
    """ΔN(i) = N − N(移除 i 之后)，单位是**核心组合**。孤岛件为 0。"""
    if base_n is None:
        base_n = closet.count_cores(temp, occasion)
    reduced = Closet([it for it in closet.items if it["id"] != item["id"]],
                     comfort=closet.comfort)
    return base_n - reduced.count_cores(temp, occasion)


def hinge_ranking(closet: Closet, temp: float, occasion: Optional[str] = None,
                  top: int = 8) -> list[dict]:
    """枢纽件排行：删了就崩的那几件。单位是核心组合。"""
    base_n = closet.count_cores(temp, occasion)
    rows = []
    for it in closet.items:
        d = delta_n(closet, it, temp, occasion, base_n)
        if d > 0:
            rows.append({"id": it["id"], "name": it["name"], "slot": it["slot"],
                         "delta_n": d})
    rows.sort(key=lambda r: -r["delta_n"])
    return rows[:top]


def wardrobe_stats(closet: Closet, temp: float,
                   occasion: Optional[str] = None) -> dict:
    """衣橱体检指标。

    **主指标是「核心组合数」，不是"合法路径数"**（见 `core_of`）。
    层级可变之后路径数按乘法膨胀，只有折成核心才回答得了
    "最少几件衣服能搭出几种样子"。

      · 效率 = 核心组合数 ÷ 在库单品数　→ 每件衣服平均贡献几种穿法
      · 密度 = 核心组合数 ÷ 理论上限　　 → 上装×下装×鞋 里有多少真的搭得上

    **密度才是尺度无关的那个数**：衣服越多它不会自动变大，
    只有衣服之间真的更搭得上才会变大。
    """
    cores, variants = closet.core_and_variant(temp, occasion)
    n_items = len(closet.items)
    cap = closet.core_capacity(temp)
    # 算式三因子（顶层上装 × 下装 × 鞋），给体检页「组合密度」说人话用。
    # 单独枚举一遍而不是改 core_capacity 的返回值 —— 那个函数有多处调用点，别动它。
    _tops: list[dict] = []
    for _slot in ("L1", "L2", "L3"):
        _tops += closet._candidates(_slot, temp)
    factors = {"tops": len(_tops),
               "bottoms": len(closet._candidates("L4", temp)),
               "shoes": len(closet._candidates("L5", temp))}
    total_all = sum(closet.count_cores(t, occasion) for t in TEMP_BANDS)
    return {
        "temp": temp,
        "n_outfits": cores,          # 兼容旧字段名，含义已改为核心组合数
        "n_cores": cores,
        "n_variants": variants,
        "core_capacity": cap,
        "capacity_factors": factors,
        "n_items": n_items,
        "efficiency": round(cores / n_items, 2) if n_items else 0.0,
        "density": round(cores / cap, 3) if cap else 0.0,
        "total_all_season": total_all,
        "islands": island_items(closet),
        "inactive": inactive_now(closet, temp, occasion),
    }
