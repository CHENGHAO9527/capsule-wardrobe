# -*- coding: utf-8 -*-
"""多天排布：一周穿搭 / 旅行穿搭。

这两件事看着是两个功能，其实是同一个问题的两个方向：

    · 一周穿搭（在家）→ 目标是**不重样**：7 天别穿成同一套
    · 旅行穿搭（出门）→ 目标是**少带件**：箱子越小越好，但每天还是要有得穿

所以共用一个引擎：**排布逻辑完全一样**，旅行只多一步「逐件试删」。
（曾经给旅行单独写了一套"能省就省"的代价函数，结果是它乐于七天穿同一条裤子、
同一双鞋 —— 手感很差。见 `_cost` 的说明：省件数归试删管，排布只管排得好看。）

为什么不是"每天各推一套"就完事：
  · 每天独立推荐 → 7 天很可能推出 7 套几乎一样的（只换一件打底衫），
    那不叫一周穿搭，那叫一天穿搭穿 7 次。
  · 旅行更不能每天独立推 —— 它会理直气壮地让你带 20 件衣服出门。

算法三步：
  1. **每天各取一个候选池**：按**"看起来是不是同一套"**去重 ——
     上衣 + 裤子 + 鞋这个组合只留打分最高的那套（`_day_pool`）。
     *不是*"今日页那套 top-N 推荐"：后者带多样性剪枝，在多天排布里会把小衣橱
     砍到只剩两套候选（见 `_day_pool` 的说明）。
  2. **贪心排布**：先排候选最少的那天（它最没得挑），逐天挑代价最小的那套。
  3. **旅行再加一步：逐件试删**。把打包清单里的每一件拿出来，问
     "不带它，这 N 天还能不能每天穿出不一样的一套？" 能就真不带。
     —— 这一步才是这个功能真正值钱的地方：**它替你少带一件衣服**。
     判定必须**直接问引擎**（`_l1_domain` + `_assign_l1`），不能问候选池 ——
     候选池是"挑过的"，用它判断"少带一件还行不行"会系统性低估。

一条不能破的底线：**所有搭配都来自硬规则枚举**，这里只做选择，不生成。

── 两条口径（2026-09-21 按用户反馈定下的）──

**① "算不算换了穿搭"看上衣。** 用户原话："同样的搭配只是换一条同样颜色的裤子
这个逻辑总觉得很奇怪，从我自己来看我觉得换上衣才算是换穿搭。"
所以跨天去重的单位是**上衣栈**（`_look`：L1/L2/L3 那几件的组合），
而不是 `graph.core_of` 的核心 —— 核心只取"最外层上装"，
T恤+皮衣 换成 灰T+皮衣 它纹丝不动，正是被吐槽的"只换了条裤子"。
**体检页的主指标仍然用核心**：那是"衣橱能少到几件还穿得出几种样子"，
和"这一周怎么排"不是同一个问题，两者别互相改。

**② 贴身层有穿着规矩。** 内搭一件最多穿 `WEAR_CAP['L1']` 天、且不连穿两天
（用户原话："短袖基本不会连续穿两天"）。这不只是观感问题 ——
**它是"旅行 7 天只带一件 T 恤"这个荒唐结果的唯一拦路虎**：
试删只会问"每天还能不能有得穿"，不问"七天穿同一件 T 恤像话吗"。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import random
from typing import Iterable, Optional

from . import outfits as rec
from .engine import graph, rules

SLOT_ORDER = ["L1", "L2", "L3", "L4I", "L4", "L5", "ACC"]
SLOT_LABEL = graph.SLOT_LABEL

MODE_WEEK = "week"
MODE_TRIP = "trip"

# ══════════════════════════════════════════════
# 排布口径：什么算"换了一套"，什么算"穿不住"
# ══════════════════════════════════════════════

# 上装层 —— "换上衣才算换穿搭"就是在这三层上判断的。
TOP_LAYERS = ("L1", "L2", "L3")

# 贴身类上装的穿着上限（一件最多穿几天）。
#
# ⚠️ 2026-09-28：原来是 `{"L1": 2}`，**只到内搭为止**，注释里还写着
# "一件外套连着穿七天，正是'少带件'想要的结果"。那句话只对**外套**成立，
# 对**卫衣**完全不成立 —— 它是贴身穿的。用户原话：
# "10 天旅游只给我带一件中间的卫衣，8 天甚至 10 天都只穿那一件，很不合理"。
#
# 真实衣橱实测（27 件、10 天行程）：18℃ 排出「1 件卫衣穿 10/10 天」，
# 15℃ / 12℃ 同样；而衣橱里有 **5 件 L2、2 件 L3**，完全够换 —— 是模型不想换，不是没得换。
#
# 判据用**品类**而不是层位：`catalog.py` 的品类表早就把这件事分好了 ——
# L1（T恤/衬衫/薄针织/吊带/Polo衫）＋ L2（毛衣/卫衣/针织开衫/马甲/厚衬衫）＝ 贴身类，
# L3（西装外套/夹克/风衣/大衣/羽绒服/皮衣）＝ 外套类。
# 取不到品类时回落到 `WEAR_CAP` 的层位值。
L2_WEAR_CAP = 4          # 中间层重复上限（下面的表和 `_l2_need` 共用，防漂移）
WEAR_CAP_BY_CAT: dict[str, int] = {
    # L1 内搭：一天一换的量级
    "T恤": 2, "衬衫": 2, "薄针织": 2, "吊带": 2, "Polo衫": 2,
    # L2 中间层：比内搭能多穿一天 —— 卫衣不会穿一天就洗
    "厚衬衫": L2_WEAR_CAP, "卫衣": L2_WEAR_CAP, "毛衣": L2_WEAR_CAP,
    "针织开衫": L2_WEAR_CAP, "马甲": L2_WEAR_CAP,
}

# 取不到品类时的层位回落。
# ⚠️ **L3 故意不在这里** —— 外套连着穿不设上限，那正是"少带件"想要的结果。
WEAR_CAP: dict[str, int] = {"L1": 2}

# 不能连着两天穿同一件的层。同样只到贴身层：
# 卫衣、外套、裤子连着穿两天太正常了。
NO_ADJACENT = ("L1",)


def wear_cap(it: dict) -> int:
    """这件**最多穿几天**。**0 = 不限**（外套类）。"""
    cat = str(it.get("category") or "")
    if cat in WEAR_CAP_BY_CAT:
        return int(WEAR_CAP_BY_CAT[cat])
    return int(WEAR_CAP.get(str(it.get("slot") or ""), 0))

# 一天最多给多少个"不同上衣"的候选，以及同一件上衣最多留几套
# （同一件上衣配另一条裤子算新的一套，但别让一条裤子占满整个池子）
POOL_MAX_LOOKS = 24
VARIANTS_PER_LOOK = 3

# ── 保暖够不够 ──
# **引擎允许"不穿外套"是刻意的**（23℃ 不穿很正常，见 graph.layer_plan），
# 但"允许"不等于"随便"：温度越低，那件可叠的上装就越该被穿上。
# 踩过的坑：18℃ 排出"只穿一件短袖"，因为代价函数里**加一件外套只会**
# 被"引入新件 +1.5"罚分，而"穿得太单薄"完全没人管 —— 排序就向着少穿一件去了。
# 用户原话："18 度怎么可能只穿短袖"。
#
# ⚠️ 2026-09-23：「26℃穿衣法则」进了引擎之后，**这件事故障已经从源头堵住了** ——
# `rules.stack_warm_ok` 在叶子上就把"不够暖"的组合否掉，所以每天的候选池里
# 每一套都必然够暖。原来那两个写死的常量（`WARM_LINE=20` / `WARM_MIN_TOPS=2`）
# 随之删掉：**门槛只有一处**（`prefs.comfort()`），plan 这边不再自己定一条线。
# 下面的 `_warm_short` 保留成一道**防御性**检查：排布中途会改候选
# （试删后重排、`_repair_l1` 换内搭），那时池子的枚举温度和判定温度有可能不再一致。

# 单日枚举上限。排布要跑 7~14 次，这里不能像单日推荐那样放开到 2 万条。
ENUM_LIMIT = 6000

_WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


def weekday_of(date_str) -> str:
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except (TypeError, ValueError):
        return ""
    return "周" + _WEEKDAY[d.weekday()]


# ══════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════

def _ids(o: dict) -> list[int]:
    return [int(it["id"]) for it in (o.get("items") or [])]


def _core(o: dict) -> tuple:
    """一套搭配的核心 = 最外层上装 × 下装 × 鞋。

    直接复用 graph.core_of —— 折叠口径必须和体检页、买前模拟完全一致，
    否则"一周 7 套"和"衣橱能穿出几种样子"会各说各话。

    ⚠️ 排布的"算不算换了穿搭"**不看它**，看 `_look`（见模块开头口径①）。
    这里留着它是当"隔几天又原样穿了一遍"的次级判据。
    """
    return graph.core_of(o.get("items") or [])


def _look(o: dict) -> tuple:
    """一套搭配的「上衣栈」—— 判断"是不是换了穿搭"就用它。

    用户拍的口径：**换上衣才算换了穿搭**；同一件上衣只换一条同色裤子，
    在他自己看来还是同一套。所以跨天去重看 L1/L2/L3 的组合。

    为什么不能再用 `graph.core_of`：核心只取最外层上装，
    「白T+皮衣+牛仔裤」和「灰T+皮衣+牛仔裤」是同一个核心 ——
    而排布要的恰恰是这两套。按核心去重，跨天就只剩"换裤子"这一条路。
    """
    ids = [int(it["id"]) for it in (o.get("items") or [])
           if it.get("slot") in TOP_LAYERS]
    return tuple(sorted(ids))


def _cand_items(cand: dict) -> list[dict]:
    """候选里**带完整字段**的那份单品 —— 引擎判"暖不暖"必须用这份。

    ⚠️ 候选的 `items` 是 `outfits.public_item()` 的投影，那是**给前端看的**，
    故意不含 `warmth`（`thickness` 反倒留着）。拿它调 `rules.warmth()` 会
    **静默回落到"按厚度查表"**：用户在「改字段」里逐件调过的保暖值
    **一格都不生效**，而且现象极其隐蔽 ——
    厚度 4 的卫衣兜底成 5.0、厚度 3 的兜底成 3.0，于是 15℃ 的行程里
    "厚度 3 但保暖值 4"的那件卫衣永远差 2℃、一次都排不上
    （实测：10 天只带两件厚度 4 的卫衣，各穿 5 天，第三件明明在池子里）。
    `_day_pool` 把真实的那份另存为 `cand["path"]`，这里优先取它。
    """
    return cand.get("path") or cand.get("items") or []


def _warm_short(cand: dict, warm: float,
                comfort: Optional[float] = None) -> float:
    """这套上装在 `warm` 下**还差几度保暖**（0 = 够暖）。

    `warm` 传**当天最冷那会儿**（`_warm_of`）：一天 15~21℃ 时按 21℃ 判
    "够暖"就等于默认你只在中午出门。

    口径与引擎**同源** —— 直接调 `rules.stack_warmth` / `comfort_of`，
    绝不在这里另写一套算式（两套算式迟早漂移，那正是"差一度"那类怪现象的来源）。

    返回值是**度数**不是件数（原来是"少穿了几层"）：件数是个粗代理，
    "一件厚毛衣"和"一件薄衬衫"在它眼里一样。现在直接看暖量。
    """
    tops = [it for it in _cand_items(cand)
            if it.get("slot") in TOP_LAYERS]
    need = rules.comfort_of(comfort) - float(warm)
    return max(0.0, need - rules.stack_warmth(tops))


def _bottoms(o: dict) -> tuple:
    """下装 + 鞋的组合 —— 在家排一周时用它让裤子和鞋也轮着穿。

    ⚠️ 因为它是一个**组合** key，换掉任意一件就归零 —— 光靠它排出来的是
    "每天换双鞋、裤子纹丝不动"。真正管单件轮换的是 `_cost` ③′ 的按件平方罚，
    这里只留作"这一整组又原样出现了"的次级判据。

    只在家用，旅行**不**用它：出门复用裤子正是"少带件"想要的结果。
    """
    ids = [int(it["id"]) for it in (o.get("items") or [])
           if it.get("slot") in ("L4", "L5")]
    return tuple(sorted(ids))


def _layer_need(n_days: int, cap: int) -> int:
    """N 天要穿得住，这一层最少要几件（一件最多穿 `cap` 天）。"""
    cap = max(1, int(cap))
    return -(-max(0, int(n_days)) // cap)


def _l1_need(n_days: int) -> int:
    """N 天要穿得住，最少要几件内搭（一件最多穿 2 天）。"""
    return _layer_need(n_days, max(1, int(WEAR_CAP.get("L1", 2))))


def _l2_need(n_days: int) -> int:
    """N 天要穿得住，最少要几件**中间层**（卫衣/毛衣/衬衫 —— 贴身类，3 天一换）。

    这就是"**先算该带多少件，再排穿搭**"里的那一步：
    10 天的行程 → 至少 ⌈10/3⌉ = **4 件中间层**，不再允许一件卫衣穿满十天。

    单件的具体上限仍由 `wear_cap(it)` 逐件算（品类不同、上限不同）；
    这里只用来定"这一层至少几件"的下限，`shrink` 试删时不许跌破它。
    """
    return _layer_need(n_days, L2_WEAR_CAP)


def _temp_of(d: dict, default: float = 20.0) -> float:
    """这一天"出门那会儿"的温度（档位决定：早=最低 / 平均 / 午后=最高）。

    用于**打分与舒适度** —— 这个温度下穿着舒不舒服、好不好看。
    "穿几层"不用它，用 `_warm_of`（当天最冷那会儿）。
    """
    try:
        return float(d.get("temp"))
    except (TypeError, ValueError):
        return default


def _warm_of(d: dict, default: float = 20.0) -> float:
    """这一天"该按几度穿够"—— **用当天最低温**，不是平均也不是最高。

    用户原话："13-22 度也穿短袖这不合理吧"。
    一天 15~21℃，早上出门 15℃、中午 21℃，用一个温度描述整天必然出错：
    按 21℃ 排就是短袖，早上冻着；按 15℃ 排又会在中午热得脱不掉。

    所以拆成两件事：
      · **穿几层、够不够暖** → 看最冷的那会儿（这个函数）
      · **热不热、好不好看** → 看主温度（`_temp_of`，档位决定）
    两层约束各自用各自该看的极端值，不再互相将就。

    没有 `tmin` 时回落到 `temp`（手填温度的场景就是单值，没有区间）。
    """
    for key in ("tmin", "temp"):
        v = d.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


# ══════════════════════════════════════════════
# 候选池：按核心取，不用今日页那套推荐
# ══════════════════════════════════════════════

def _day_pool(closet, temp: float, occasion: Optional[str], weights: dict,
              worn: dict, seed: Optional[int] = 0,
              limit: int = ENUM_LIMIT,
              max_looks: int = POOL_MAX_LOOKS,
              per_look: int = VARIANTS_PER_LOOK,
              score_temp: Optional[float] = None,
              must: Optional[Iterable[int]] = None) -> dict:
    """这一天的候选池：**按「看起来是不是同一套」去重**（上衣 × 下装 × 鞋）。

    ⚠️ **两个温度参数是刻意的**（2026-09-21 按用户反馈拆开）：
      · `temp` —— **枚举**用。传当天最冷那会儿（`_warm_of`），因为它决定
        "这套够不够暖"（`rules.stack_warm_ok` 按它判）。15~21℃ 的一天，
        cold 那头就决定了当天需要几件上装。
      · `score_temp` —— **打分**用。传出门那会儿（`_temp_of`），
        "这个温度下穿着舒不舒服"由它说了算。
    默认两者相同（手填温度的场景只有一个数）。

    `must`（用户标的必带）在这里有一个**必须做对**的事：
    **含必带件的组合一定要进池子，不能被截断挤掉。**
    踩过：`max_looks=24` 是按上衣栈截断的，而 7 件上衣 × 各自的叠穿组合
    轻松超过 24 个栈 —— 分数低的那件上衣整个被挤出去，于是 `_cost` 里
    "必带优先"的奖励**根本无从下手**（候选都不在池子里）。
    所以去重键里带上"这套用到了哪些必带件"。

    ⚠️ **但"进池"只给保位，不给插队**（2026-09-22 修，用户原话：
    "这怎么选了一条裤子必选结果 7 天只穿一条裤子"）。
    原来的写法是把"命中必带"当**排序第一关键字**：命中的一律排在没命中的前面。
    看起来只是照顾一下，实际是**灾难** —— 一条裤子能跟每个上衣栈都搭，
    于是 24 个栈里每一个都有"含这条裤子"的变体，**保位变成垄断**：
    整个候选池 100% 含那条裤子（实测 72/72 套），
    天天只能从含它的套里挑，"必带"于是被理解成"每天必须穿"。
    正确做法是**每个必带件只保一个名额**（含它的最高分那套插到队首），
    剩下的名额一律按分数排 —— 必带件"排得上"，但不吃独食。

    ⚠️ 不能图省事直接调 `outfits.recommend`。它带**多样性剪枝**
    （"一套要跟已选的每一套至少差 2 件"），那是给今日页「三套互不相似」用的。
    放进多天排布里，小衣橱会被它砍到只剩两套候选 —— 举实测过的例子：
    8 件衣服内搭和中间层各只有 1 件可选，"至少差 2 件"就等价于
    "外层 / 下装 / 鞋三个槽位全换"，而三个二元槽位最多只能凑出 2 套。
    后果是 5 天里只排出 3 个不同核心，还会倒过来提示"你衣橱不够"。

    ⚠️ 也**不能按 `graph.core_of` 的核心去重**。核心只到"最外层上装"：
    「白T+皮衣+牛仔裤」和「灰T+皮衣+牛仔裤」是同一个核心，可排布要的
    恰恰是这两套。按核心去重会把它们合成一套，于是跨天只能靠换裤子凑数 ——
    排出来就是用户吐槽的"同样一身，只换一条同色裤子"。

    去重键 = (上衣栈, 核心, 命中的必带)，等价于"上衣 + 裤子 + 鞋"这三个
    看得见的变量；同一个键只留打分最高的那套（多层叠穿的那些变体顺手被折掉了）。

    截断**先按上衣铺开再截**：上衣之间大致按"这件上衣最好的一套有多好"排序，
    每件上衣最多留 `per_look` 套。反过来的话（先按套的分数排序再截断），
    分数最高的那两件上衣会占满整个池子，跨天不重样就无从谈起了。
    """
    # 「26℃穿衣法则」的舒适线。从 closet 上取，不在这里读配置 ——
    # 引擎判"够不够暖"和这里挑必带名额必须是同一条线。
    comfort = getattr(closet, "comfort", None)
    st = float(temp) if score_temp is None else float(score_temp)
    must_set = {int(i) for i in (must or ())}
    cores, variants = closet.core_and_variant(temp, occasion)
    rng = random.Random(seed)
    best: dict[tuple, tuple[float, dict, list[dict]]] = {}

    def hook(p: list[dict]) -> None:
        s = rec.score_outfit(p, st, occasion, weights, worn, set(), rng)
        hit = frozenset(int(x["id"]) for x in p) & must_set if must_set else frozenset()
        # 命中的必带进键：这样"含必带件的方案"不会被同栈的别套折叠掉
        k = (_look({"items": p}), graph.core_of(p), hit)
        cur = best.get(k)
        if cur is None or s["score"] > cur[0]:
            best[k] = (s["score"], s, list(p))

    closet.count_paths(temp, occasion, path_hook=hook, limit=limit)

    per: dict[tuple, list] = defaultdict(list)
    for (look, core_k, hit), row in best.items():
        per[(look, hit)].append(row)

    # 排序：**一律按分数**（tie-breaker 用 `look`）。
    # ⚠️ 这里刻意**不带**"命中必带优先"—— 见上面 docstring：
    # 插队会让必带件垄断整个池子，把"必带"变成"每天必须穿"。
    ranked = sorted(per.items(), key=lambda kv: (
        -max(r[0] for r in kv[1]),
        str(kv[0][0])))
    order = [k for k, _rows in ranked]

    # 必带件"保位"：每个必带件抢**一个**名额，插在最前面，其余名额按分数。
    # 保位不是优待、是防截断 —— 只要它在池里，"能穿就穿上"（`_cost` ⑥）才有着落。
    # ⚠️ 挑哪个组来保位有讲究：**先挑 gap 最小的那个组**，不能只挑分数最高的。
    # `_cost` 的保暖缺口是**第一关键字**，压过 ⑥ 的必带奖励 —— 保位保到一个
    # "单层上装"的组，那条裤子就会因为"那天不如穿两层"而永远选不上，
    # 最后被丢进「备着」（实测：必带裤子明明在池里，7 天一次都没穿上）。
    # 同一个组内的套共用一个上衣栈，所以 gap 整组一致，随便取一套算就行。
    # `must` 为空时 `keep` 为空、`order` 原样，**行为与改动前逐字一致**（不容许漂移）。
    if must_set:
        def _grp_gap(k: tuple) -> float:
            return _warm_short({"items": per[k][0][2]}, temp, comfort)

        keep: list[tuple] = []
        for mid in sorted(must_set):
            cands = [k for k in order if mid in k[1] and k not in keep]
            if not cands:
                continue
            keep.append(min(cands, key=lambda k: (_grp_gap(k),
                                                 -max(r[0] for r in per[k]))))
        keep_set = set(keep)
        order = keep + [k for k in order if k not in keep_set]

    picked: list[dict] = []
    for _k in order[:max_looks]:
        rows = per[_k]
        rows.sort(key=lambda r: -r[0])
        # 组内先按**裤子**去重：同一个上衣栈下，别让三条变体都是
        # 「同一条裤子 + 三双鞋」—— 那样跨天换裤子的选项会被整片挤掉，
        # 排出来就是用户看到的"上衣天天换、裤子一周不换"
        # （`rows[:3]` 里其实有别的裤子，只是分数都被这条挤在后面）。
        seen_l4: set = set()
        head: list = []
        tail: list = []
        for row in rows:
            key_l4 = tuple(sorted(int(it["id"]) for it in (row[2] or [])
                                  if it.get("slot") == "L4"))
            if key_l4 in seen_l4:
                tail.append(row)
            else:
                seen_l4.add(key_l4)
                head.append(row)
        # 换裤子的优先占位，占不满再从剩下的（同裤不同鞋）里按分数补齐
        for sc, s, p in (head + tail)[:per_look]:
            picked.append({
                "score": sc,
                "path": p,
                "items": [rec.public_item(it) for it in p],
                "reason": rec.make_reason(p, s, temp, occasion),
                "notes": s["cons"][:2],
            })

    return {"outfits": picked, "cores": cores, "variants": variants}


# ══════════════════════════════════════════════
# 贪心排布
# ══════════════════════════════════════════════

def _wear_violations(cand: dict, i: int, chosen: list, counts: dict) -> int:
    """这套衣服在第 i 天穿，违反"贴身层穿着规矩"几次。

      · 内搭不连穿两天（用户："短袖基本不会连续穿两天"）
      · 一件**贴身类上装**最多穿 `wear_cap(件)` 天 —— 内搭 2 天、卫衣/毛衣/衬衫 3 天、
        外套不限（`wear_cap` 按**品类**取，见那里的说明）。

    第二条不是洁癖，是**"旅行只带一件上衣"的解药**：
    试删只会问"每天还排得出吗"，不会问"十天穿同一件卫衣像话吗"。
    没有这条，试删会一路省到只剩一件内搭。

    ⚠️ 2026-09-28：原来只查 `WEAR_CAP`（只有 L1），于是 **L2 完全不设防** ——
    10 天排出「1 件卫衣穿 10/10 天」。现在改成按件取上限，L2 一并纳入。
    这条同时是**贪心愿意换衣服**的唯一动力：命中上限后每天 +60，
    而"换一件没穿过的"只贵 +1.5（`_cost` ④），差价足够让它主动轮换。

    `counts` 是**还没算上这一天的**累计次数（调用方在挑之前算好）。
    """
    v = 0
    for it in cand.get("items") or []:
        slot = it.get("slot")
        iid = int(it["id"])
        if slot in NO_ADJACENT:
            for j in (i - 1, i + 1):
                if 0 <= j < len(chosen) and chosen[j]:
                    if any(int(x["id"]) == iid
                           for x in (chosen[j].get("items") or [])
                           if x.get("slot") in NO_ADJACENT):
                        v += 1
        cap = wear_cap(it)
        if cap:
            n = int(counts.get(iid, 0))
            if n >= cap:
                v += 1 + (n - cap)
    return v


def _cost(cand: dict, used_looks: dict, used_cores: dict, used_bottoms: dict,
          item_days: dict, chosen: list, i: int, counts: dict,
          warm: float, must_pending: Optional[set] = None,
          comfort: Optional[float] = None) -> tuple:
    """返回 `(保暖缺口, 其余代价)` —— 越小越好。

    `warm` 是**当天最冷那会儿**（`_warm_of`），不是出门温度：判"冻不冻"看它。
    `must_pending` 是**用户标了必带、但还没排进任何一天**的那些件（见下 ⑥）。

    ⚠️ **返回元组、先比缺口，是刻意的。** 只要池子里还剩一件"够暖"的方案，
    就绝不会退到穿得单薄的那件上。写成加法（`gap * 45 + …`）不行：
    重复惩罚是**累加**的（核心重复 8 × 次数），实测 18℃ 的旅行里
    "卫衣 + 那条裤 + 那双鞋"这一个核心重复到第 4 次时累到 24+24=48，
    反超了 45 分的缺口罚分 —— 那天于是又排成了单穿短袖。
    缺口是"不该发生"，重复只是"不够好看"，两者不该放进同一个加法里。

    也**不做成硬过滤**（直接把单薄的候选剔掉）：万一整个衣橱就没有
    那个温度能加的层，硬过滤会让那天直接排不出来 —— 那是"有个坑不填就交白卷"。
    字典序正好：有够暖的就选，没有就把实话排在明面上，让 `_greedy` 记一条 warn。

    其余取舍：
    ⚠️ **一周和旅行共用这一套代价**，旅行不另开一档"能省就省"。
    踩过的坑：原来旅行档会把"复用已有件"算成加分，于是它乐于七天穿同一条裤子、
    同一双鞋 —— 而"少带件"根本不归它管：**试删（`shrink`）才是精确的那把刀**
    （它真的会问"不带这件还排得出吗"），让代价函数粗鲁地省，只会把计划排难看。
    """
    gap = _warm_short(cand, warm, comfort)
    ids = _ids(cand)
    cost = 0.0

    # ⓪ 贴身层规矩（不连穿、单件不超上限）—— 罚得最重，
    #    重到"只要还有别的选择就绝不会落上"，但又不做成硬过滤：
    #    内搭实在不够时，用户该看到的是"某天不得不重复穿"这句实话。
    cost += 60.0 * _wear_violations(cand, i, chosen, counts)

    # ⓪′ 贴身类上装**摊开用**（平方惩罚，L1 与 L2 都算）。
    #    ⚠️ 这条是"贪心看不到后几天"的解药，实测踩过：
    #    7 天、箱子里 4 件内搭，`_cost` 每天挑"当前最顺眼"的那两件，
    #    到第 6 天发现三件都用满 2 次、第 4 件（黑色字母印花T恤）一次没轮到 ——
    #    而它在每天的候选里都只差一点点分数。**第 6 天于是无解，只能违规。**
    #    平方让"穿过的"比"没穿过的"贵一点（0→0、1→4、2→16、3→36），
    #    内搭就自然轮着来了。外套/裤子鞋不算 —— 那几件重复穿是正常的。
    #
    #    ⚠️ 2026-09-28：**L2 原来完全不吃这条**，只吃 `_wear_violations` 那 60 分。
    #    60 分是**悬崖**：一旦所有候选都踩在悬崖上（L1 也到顶了），
    #    它就不再产生区分度，"该换哪一件"退化成比分数。
    #    实测：15℃ 10 天先排出 3 件卫衣轮换（3/3/2，正确），
    #    试删后**重排那一步**却退化回 2 件卫衣各穿 5 天。
    #    平方项是**斜坡**：穿得越多越贵，于是它自己去找那件没穿过的。
    for _it in cand.get("items") or []:
        _slot = _it.get("slot")
        if _slot in ("L1", "L2"):
            _n = int(counts.get(int(_it["id"]), 0))
            # L2 权重高得多（20 vs 4）：中间层件数比内搭少，而它要竞争的是
            # ①的"上衣栈重复"（24 分/次）—— 权重给到 6 时实测压不过，
            # 15℃ 的 10 天行程照样退化成 2 件卫衣各穿 5 天。
            cost += (4.0 if _slot == "L1" else 20.0) * _n * _n

    # ① 上衣重复 —— 用户认可的"算不算换了穿搭"就是看上衣，这是第一位的事
    cost += 24.0 * used_looks.get(_look(cand), 0)

    # ② 上衣一样、裤子和鞋也一样 → 隔几天又原样穿了一遍
    cost += 8.0 * used_cores.get(_core(cand), 0)

    # ③ 下装/鞋的**组合**轮着穿（权重远低于上衣，压不过"换上衣"这件事）。
    #    这是"七天同一条裤子"的对症药 —— 试删只保证"够穿"，不保证"不难看"。
    cost += 3.5 * used_bottoms.get(_bottoms(cand), 0)

    # ③′ 单条裤子 / 单双鞋轮着穿（按件平方罚）。
    #    ⚠️ 只有 ③ 是不够的，实测踩过：③ 罚的是"下装 + 鞋"这个**组合**，
    #    只要每天换双鞋，这个 key 就变了、罚分归零 —— 于是排出来的是
    #    **上衣天天换、裤子一周不换**（用户："7 天只穿一条裤子"）。
    #    平方和内搭那条同一个道理：穿过的比没穿过的贵一点（0→0、1→5、2→20、3→45），
    #    裤子就自然轮着来了。他有 9 条裤子排 7 天，本该一条轮到一次。
    #    鞋也用同一招但**力度减半**（2.5）：裤子比鞋更该换，而鞋两三天轮到一次就够。
    #    ⚠️ 这两条是配套的，只改裤子不改鞋就会把"不换的东西"从裤子挪到鞋上
    #    （实测：裤子修好的当天，7 天里有 6 天是同一双厚底运动鞋）。
    #    只算 L4 / L5，**不算 L4I**（秋裤连着穿是完全正常的）。
    for _it in cand.get("items") or []:
        _slot = _it.get("slot")
        if _slot in ("L4", "L5"):
            _n = int(counts.get(int(_it["id"]), 0))
            cost += (5.0 if _slot == "L4" else 2.5) * _n * _n

    # ④ 不重样优先，但也不该为了不重样把从没穿过的衣服硬塞进来
    cost += 1.5 * len([x for x in ids if x not in item_days])

    # ⑤ 本地打分的分数做最后的区分度
    cost -= float(cand.get("score") or 0.0) * 0.06

    # ⑥ 用户标的「必带」还没安排上的，优先排进来。
    #    只在同一层（gap 相同）里比 —— 热天硬把羽绒服排进去，不该压过"穿得合不合适"。
    #    排不进去的不会消失：`_greedy` 记下来，`_packing` 照样把它放进箱子（标「备着」）。
    if must_pending:
        cost -= 30.0 * sum(1 for x in ids if x in must_pending)
    return (gap, cost)


def _greedy(pools: list[list[dict]], temps: list[float], warms: list[float],
            keys: list[str], must: Optional[Iterable[int]] = None,
            comfort: Optional[float] = None) -> dict:
    """逐天挑一套，并把"每件衣服被排在第几天"记下来。

    排序规则：**先排候选最少的那天** —— 它最没得挑，先定下来，后面几天才有腾挪空间。
    候选数相同时先排最冷的那天（离舒适区越远越挑衣服，留给它的余地也最小）。

    `temps` 是打分用的出门温度，`warms` 是判"够不够暖"用的当天最低温
    （见 `_warm_of`）。两者刻意不同：15~21℃ 的一天，按 21℃ 挑好看的、
    按 15℃ 判冻不冻。

    `must` 是用户标了"必带"的件：**边挑边把它们用掉**（`must_pending`），
    用到的候选优先。排完还剩的（那个温度根本穿不上）交给 `_packing` 放进箱子。

    挑选时比的是 `_cost` 返回的 `(保暖缺口, 其余代价)` 元组 —— 先把"穿得单薄"
    这件事排干净，再谈好不好看（见 `_cost` 的说明）。

    `item_days` 在挑选过程中边挑边攒，它是打包清单的来源（穿过的才算进箱子）。
    """
    n = len(pools)
    order = sorted(range(n), key=lambda i: (len(pools[i]), -abs(warms[i] - 18.0), i))

    # 池子里**真正出现过**的内搭件数。它才代表"这个温度下能当内搭的有几件"。
    # ⚠️ 光数衣橱里有几件 L1 会严重高估：标了「只能单穿」的件一叠层就穿不上，
    #    而 14~18℃ 这类要叠层的天气恰恰把它们全排除在外。
    #    实测（真实衣橱 27 件）：8 件 L1 里 5 件带标签，能当内搭的只有 4 件，
    #    于是 10 天行程**必然**重复穿。而原来的文案只会说"你得带够 5 件" ——
    #    等于让用户去找一件他根本拿不出来的衣服（"缺件"和"拿不出来"是两回事）。
    _pool_l1 = {int(_it["id"]) for _pl in pools for _c in _pl
                for _it in _cand_items(_c) if _it.get("slot") == "L1"}

    used_looks: dict[tuple, int] = {}
    used_cores: dict[tuple, int] = {}
    used_bottoms: dict[tuple, int] = {}
    item_days: dict[int, list[str]] = defaultdict(list)
    chosen: list = [None] * n
    warns: list[str] = []
    thin_days: list[tuple[int, float]] = []   # 穿得单薄的天（i, 温度）
    repeat_days: list[int] = []               # 得重复穿同一件的天（i）
    repeat_caps: set[int] = set()             # 那几天里顶到上限的层（2=L1 的 cap，4=L2 的）
    must_pending: set[int] = {int(i) for i in (must or ())}

    for i in order:
        opts = pools[i]
        if not opts:
            warns.append(f"第 {i + 1} 天（{temps[i]:g}℃）算不出任何搭配 —— "
                         f"看衣橱体检里缺哪一层。")
            continue
        counts = {k: len(v) for k, v in item_days.items()}
        pick = min(opts, key=lambda c: _cost(c, used_looks, used_cores, used_bottoms,
                                            item_days, chosen, i, counts, warms[i],
                                            must_pending, comfort))
        chosen[i] = pick
        look = _look(pick)
        core = _core(pick)
        bot = _bottoms(pick)
        used_looks[look] = used_looks.get(look, 0) + 1
        used_cores[core] = used_cores.get(core, 0) + 1
        used_bottoms[bot] = used_bottoms.get(bot, 0) + 1
        for iid in _ids(pick):
            item_days[iid].append(keys[i] if i < len(keys) else "")
            must_pending.discard(iid)
        if _wear_violations(pick, i, chosen, counts):
            # ⚠️ 要说清是**哪一层**轮不过来。2026-09-28 L2 也开始吃上限之后，
            # 原来那句写死"内搭"的话会把中间层的重复算到内搭头上
            # （实测 15℃ 的 10 天行程里，真正顶到上限的是卫衣，不是内搭）。
            #
            # ⚠️ 这里**只收集、不发言**：10 天里可能有 3、4 天都在重复穿，
            # 逐天各印一条几乎相同的话，用户读到的是三遍同一件事
            # （「直接说重点」）。统一在循环后合成**一条**，把是哪几天一起列出来。
            repeat_days.append(i)
            for _it in _cand_items(pick):
                _c = wear_cap(_it)
                if _c and int(counts.get(int(_it["id"]), 0)) >= _c:
                    repeat_caps.add(_c)
        # 穿得单薄也说出来。引擎已经用 `stack_warm_ok` 保证了够暖，
        # 所以这句正常情况下不会出现；真的出现了，只剩一种原因：
        # **衣橱里那个温度下确实没有能加的中间层/外层**（不是排布偷懒）。
        # 这正是"失败要看得见"——不然用户只会以为程序算错了。
        if _warm_short(pick, warms[i], comfort):
            thin_days.append((i, warms[i],
                              not any(not _warm_short(c, warms[i], comfort)
                                      for c in opts)))

    if repeat_days:
        # ⚠️ **发两条，不是一条**（2026-09-29）：内搭和中间层是两件事，
        # 各说各的、各带各的标记 —— 这样 `_repair_l1` 修好内搭之后只撤内搭那句，
        # 不会把中间层那句（它仍然成立）一起吞掉。
        _n_rep = len(repeat_days)
        if WEAR_CAP["L1"] in repeat_caps:
            _need1 = _l1_need(n)
            if _pool_l1 and len(_pool_l1) < _need1:
                # 说清是"拿不出来"，不是"带得不够"：这两句对用户的下一步完全不同
                # （一个是去改标签/添衣服，一个是往箱子里再塞一件）。
                warns.append(
                    f"内搭不够换：这个温度下能当内搭的只有 {len(_pool_l1)} 件"
                    f"（标了「只能单穿」的一叠层就穿不上），"
                    f"而一件最多穿 {WEAR_CAP['L1']} 天、不连穿，"
                    f"{n} 天要 {_need1} 件才轮得开 —— 其中 {_n_rep} 天得重复穿。")
            else:
                warns.append(
                    f"内搭得带够 {_need1} 件"
                    f"（一件最多穿 {WEAR_CAP['L1']} 天、不连穿）"
                    f" —— 其中 {_n_rep} 天得重复穿。")
        if L2_WEAR_CAP in repeat_caps:
            warns.append(
                f"中间层得带够 {_l2_need(n)} 件"
                f"（一件最多穿 {L2_WEAR_CAP} 天） —— 其中 {_n_rep} 天得重复穿。")
        if not repeat_caps:
            # 兜底：真到了重复穿、却一层都归不出因时也得说一声 ——
            # 不能因为"分不出是哪层"就整条沉默（沉默 = 用户不知道为什么排成这样）。
            warns.append(
                f"内搭得带够 {_l1_need(n)} 件 —— 其中 {_n_rep} 天得重复穿。")

    if thin_days:
        where = "、".join(f"第 {i + 1} 天（最冷 {t:g}℃）" for i, t, _cg in thin_days[:4])
        if len(thin_days) > 4:
            where += " 等"
        if any(cg for _i, _t, cg in thin_days):
            warns.append(f"{where}只排出一件上装 —— 那天偏凉，"
                         f"但你衣橱里能加的那一层（中间层 / 外层）扛不住它，"
                         f"所以只能这么穿。想稳一点就添一件能穿到 "
                         f"{min(t for _i, t, _cg in thin_days):g}℃ 的外套。")
        else:
            warns.append(f"{where}只排出一件上装，偏凉 —— 加一件中间层或外套更稳。")

    return {"chosen": chosen, "used_looks": used_looks, "used_cores": used_cores,
            "item_days": item_days, "thin_days": thin_days,
            "must_left": must_pending,
            "warns": list(dict.fromkeys(warns))}


# ══════════════════════════════════════════════
# 旅行：逐件试删
# ══════════════════════════════════════════════

# 精简过程中会反复问"这批衣服在这个温度下，贴身层有哪几件真能穿"。
# 按 (单品集合, 温度, 场合) 缓存，一问一答都是小枚举，但同一集合会被问很多次。
_DOMAIN_CACHE: dict[tuple, frozenset] = {}
_DOMAIN_CACHE_MAX = 96


def _l1_domain(subset: list[dict], temp: float,
               occasion: Optional[str],
               comfort: Optional[float] = None) -> frozenset:
    """**只有这些衣服**，在这个温度下，贴身层有哪几件真能穿出门（成套，不是单看它自己）。

    直接问引擎，不走候选池。候选池是"挑过的"，用它判断"少带一件还行不行"
    会系统性低估 —— 池子里剩下的都是用了别的衣服的套。

    为什么只问贴身层：判可行性的约束全落在它身上（不连穿 + 单件上限）。
    下装和鞋只要有得穿就行，不参与判定，所以不必枚举全部核心。

    实现上是**逐件 `must={'L1': it}` + `limit=1`** 地问：
    `count_paths` 一命中上限就返回，单件只走一小段 DFS，
    比全量枚举一遍核心便宜一个量级。
    """
    # ⚠️ 缓存键必须含 comfort —— 改了舒适线，能穿的贴身层会变，
    # 键里没有它就会一路返回旧结果（"改了设置但计划没变"）。
    key = (tuple(sorted(int(it["id"]) for it in subset)),
           round(float(temp), 2), occasion or "",
           rules.comfort_of(comfort))
    hit = _DOMAIN_CACHE.get(key)
    if hit is not None:
        return hit

    out: set[int] = set()
    try:
        sub = graph.Closet(subset, comfort=comfort)
        for it in sub.items:
            if it["slot"] != "L1":
                continue
            if sub.count_paths(temp, occasion, must={"L1": it}, limit=1) > 0:
                out.add(int(it["id"]))
    except Exception:
        out = set()

    res = frozenset(out)
    if len(_DOMAIN_CACHE) > _DOMAIN_CACHE_MAX:
        _DOMAIN_CACHE.clear()
    _DOMAIN_CACHE[key] = res
    return res


def _solve_l1(domains: list, cap: int, budget: int = 20000) -> Optional[list[int]]:
    """按天给贴身层排一排：每天从当天的可用内搭里挑一件，要求不连穿、单件不超 cap 天。

    **找到一份合规排班就返回它**（第 i 天穿哪件内搭的 id；那天本来就没得穿的记 0）；
    找不到返回 None。回溯搜索，先试"这一趟用得最少"的那件 —— 把次数摊开最容易成功。
    节点数超预算就返回 None（保守：宁可多带一件，也不能让你没得穿）。

    `domains` 里的 **None 表示那天本来就算不出搭配**（衣橱在那个温度缺层位单品，
    跟带哪几件无关）→ 跳过它：既不占内搭，也不参与"不连穿"的相邻判定。
    实测踩过：有一天气温超出所有单品上限时，这个 None 不存在，整趟旅行
    会一件都省不下来（试删第一步就被那天否掉）。
    """
    n = len(domains)
    if not n:
        return []
    counts: dict[int, int] = defaultdict(int)
    used: list[int] = [0] * n
    nodes = [0]

    def dfs(i: int) -> bool:
        if i == n:
            return True
        nodes[0] += 1
        if nodes[0] > budget:
            return False
        if domains[i] is None:
            return dfs(i + 1)
        prev = used[i - 1] if i else 0
        for iid in sorted(domains[i], key=lambda x: (counts[x], str(x))):
            if iid == prev or counts[iid] >= cap:
                continue
            counts[iid] += 1
            used[i] = iid
            if dfs(i + 1):
                return True
            counts[iid] -= 1
            used[i] = 0
        return False

    return list(used) if dfs(0) else None


def _assign_l1(domains: list, cap: int, budget: int = 20000) -> bool:
    """存在解就 True。`_can_serve`（试删的可行性判定）用它。"""
    return _solve_l1(domains, cap, budget) is not None


# 贪心排布里那句"得重复穿同一件"的提示。修复成功后要按这个标记把它撤掉 ——
# 留着就会出现"明明已经改好了，页面还说你得重复穿"这种自相矛盾。
# ⚠️⚠️ 这个常量必须**逐字**是文案里的一段。2026-09-28 发现它写的是
# "得重复穿某件内搭"，而文案里实际是"得重复穿同一件了" —— 一个**从来没出现过**
# 的字符串。于是三处依赖它的逻辑**全部静默失效**：两处"撤掉旧警告"永远撤不掉，
# 一处"有旧警告就别再补一条"永远为真 → 页面上同一件事说两遍，还互相矛盾。
# 改文案时**必须同步改这里**（判据：`REPEAT_WARN_MARK in <文案>` 必须为真）。
# `test_plan.py` 有一条断言专门盯这件事。
#
# ⚠️⚠️ 2026-09-29：「重复穿」类提示改成**按层分成两条**（内搭一条、中间层一条）。
# 原因：`_repair_l1` 修的是**贴身层**，修好之后只该撤「内搭」那句 ——
# 中间层的重复不归它管。原来两句合成一条、撤除又用同一个总标记一刀切，
# 于是"修好内搭"会把"中间层也得带够"一起吞掉 → **漏报**。
# 三条标记（都必须**逐字**是文案里的子串）：
REPEAT_WARN_MARK = "得重复穿"          # 总判据：这是"重复穿类"的提示
L1_REPEAT_MARK = "内搭不够换"          # 内搭那句 · 分支一（拿不出来）
L1_REPEAT_MARK_ALT = "内搭得带够"      # 内搭那句 · 分支二（带得不够）
L2_REPEAT_MARK = "中间层得带够"        # 中间层那句


def _is_l1_repeat(w: str) -> bool:
    """这句是不是「内搭轮不过来」。`_repair_l1` 修好后只该撤这一类。"""
    return L1_REPEAT_MARK in w or L1_REPEAT_MARK_ALT in w


def _drop_l1_repeat(warns: list[str]) -> list[str]:
    """撤掉「内搭轮不过来」那一句。

    ⚠️ **只撤内搭，不碰中间层**（2026-09-29）：中间层的重复不归 `_repair_l1` 管。
    用总标记 `REPEAT_WARN_MARK` 一刀切，会把中间层那句一起吞掉。
    凡是"修好 A 顺手撤掉 B"的写法，都要先问一句：**B 真的也被修好了吗？**
    """
    return [w for w in warns if not _is_l1_repeat(w)]


def _pick_with_l1(pool: list[dict], l1_id: int, chosen: list, day: int,
                  warm: float, comfort: Optional[float] = None) -> Optional[dict]:
    """在这一天的候选里挑一套「内搭正好是 l1_id」的。

    排序（和 `_cost` 同源，**保暖永远是第一关键字**）：
      ① 保暖缺口 —— 不能为了把贴身层的规矩修好，就让人在 15℃ 单穿一件短袖。
         这一条是实测踩出来的：第一版只看"上衣栈用没用过"，结果 14/16/18℃
         那几天全被换成了单层，把"穿够两层"整个破坏掉。
      ② 这个上衣栈别的天用没用过（别为了修规矩把"不重样"搞砸）
      ③ **裤子和鞋别的天用得多不多** —— 换内搭不该把裤子轮换一起搞砸。
         ⚠️ 这一条是补上的（2026-09-22）：原来只看 ①②，于是它每次都挑
         "分数最高的那套"，而分数最高的那套总是同一条万能牛仔裤 ——
         `_greedy` 刚把 7 天排成 7 条不同的裤子，这里一趟就全换回去了。
         口径必须和 `_cost` ③/③′ 一致，否则两边互相拆台。
      ④ 本地分数
    """
    others: dict[tuple, int] = {}
    seen_pairs: dict[tuple, int] = {}
    seen_l45: dict[int, int] = {}
    for j, c in enumerate(chosen):
        if j != day and c:
            k = _look(c)
            others[k] = others.get(k, 0) + 1
            b = _bottoms(c)
            seen_pairs[b] = seen_pairs.get(b, 0) + 1
            for it in c.get("items") or []:
                if it.get("slot") in ("L4", "L5"):
                    iid = int(it["id"])
                    seen_l45[iid] = seen_l45.get(iid, 0) + 1

    def _repeat(c: dict) -> float:
        rep = 3.5 * seen_pairs.get(_bottoms(c), 0)
        for it in c.get("items") or []:
            slot = it.get("slot")
            if slot in ("L4", "L5"):
                n = seen_l45.get(int(it["id"]), 0)
                rep += (5.0 if slot == "L4" else 2.5) * n * n
        return rep

    best, best_key = None, None
    for c in pool or []:
        if not any(it.get("slot") == "L1" and int(it["id"]) == l1_id
                   for it in c.get("items") or []):
            continue
        key = (_warm_short(c, warm, comfort), others.get(_look(c), 0), _repeat(c),
               -float(c.get("score") or 0.0))
        if best_key is None or key < best_key:
            best, best_key = c, key
    return best


def _recount_item_days(chosen: list, keys: list[str]) -> dict:
    """按**最终排班**重算"每件衣服被排在第几天" —— 打包清单就是从它来的。

    贴身层被换过之后必须重算，否则箱子会按旧排班算：不是漏掉换进来的那件
    （清单和穿搭对不上，用户已经投诉过一次），就是留着已经没穿的件。
    """
    out: dict[int, list[str]] = defaultdict(list)
    for i, c in enumerate(chosen):
        if not c:
            continue
        for iid in _ids(c):
            out[iid].append(keys[i] if i < len(keys) else "")
    return out


def _repair_l1(pools: list[list[dict]], chosen: list, keys: list[str],
               warms: list[float], comfort: Optional[float] = None) -> int:
    """回头修贴身层：贪心排出来的可能超量，这里定点改掉。返回改了几套。

    **为什么非要有这一步**：`_greedy` 是逐天决策的，而且处理顺序按"哪天候选最少"
    排，所以它看不到全局。实测真实衣橱 7 天旅行：到最后一天时四件内搭全都到上限了，
    它只能违规 —— 排出一件 T 恤穿 3 天（还连穿了）。而这件事情**是有解的**
    （`_l1_need` 已经保证件数够），只是需要回头改前几天。

    做法：拿每天候选池里出现过的内搭当候选域，跑一次回溯（`_solve_l1`，跟试删
    共用同一套判定），再按结果把每天的内搭换掉。只动贴身层那一件，其它件不碰。

    ⚠️ **但前提是"真的有人超量了"**（`_l1_issues` 空就直接返回）。
    它是"救火"不是"重排"，见下面那句早退的说明。

    ⚠️ **域必须收窄到"那天保暖缺口最小的那批候选"**。第一版用了池子里出现过的
    全部内搭，结果 `_solve_l1` 给出了一份"数学上成立、落地时落不了"的排班：
    它让 15℃ 那天换成某件内搭，而那件在那个池子里只出现在**单穿**的套里 ——
    换过去等于让人在 15℃ 单穿短袖。实测就是这么差一步没修好（改了两处、
    剩下那处还是穿 3 天）。收窄之后，域里的每一件都有"穿得住"的落地方式。

    最后再兜一道：换完缺口不许变大（宁可重复穿，也不能冻着）。
    """
    n = len(chosen)
    if not n:
        return 0
    cap = max(1, int(WEAR_CAP.get("L1", 2)))

    # ⚠️ 现状没违规就**别动手**（2026-09-22 补）。
    # `_solve_l1` 只要求"存在一份分配"，不比较"是不是比现在这份更好" ——
    # 所以哪怕当天排得好好的，它也会给出另一份合法方案，然后这里逐天照换。
    # 换的时候整天的搭配都被 `_pick_with_l1` 重挑一遍，于是 `_greedy`
    # 刚排出来的裤子轮换被无声推翻（实测：7 天排成 7 条不同的裤子，
    # 这一趟换成同一条穿 5 天）。**修规矩的步骤不该顺手改别的东西。**
    if not _l1_issues(chosen):
        return 0

    domains: list = []
    for i, pool in enumerate(pools):
        warm = warms[i] if i < len(warms) else 20.0
        if not pool:
            domains.append(None)
            continue
        best_gap = min(_warm_short(c, warm, comfort) for c in pool)
        ids = {int(it["id"])
               for c in pool if _warm_short(c, warm, comfort) <= best_gap
               for it in c.get("items") or [] if it.get("slot") == "L1"}
        domains.append(frozenset(ids) or None)
    if not any(domains):
        return 0

    solution = _solve_l1(domains, cap)
    if solution is None:
        return 0        # 本来就没解（件数不够）→ 留给 warns 去说实话，别硬改

    swaps = 0
    for i, want in enumerate(solution):
        if not want or not chosen[i]:
            continue
        cur = {int(it["id"]) for it in chosen[i]["items"] if it.get("slot") == "L1"}
        if cur == {want}:
            continue
        warm = warms[i] if i < len(warms) else 20.0
        alt = _pick_with_l1(pools[i], want, chosen, i, warm, comfort)
        if alt is None:
            continue
        if _warm_short(alt, warm, comfort) > _warm_short(chosen[i], warm, comfort):
            continue    # 换完更冷了 → 不换，宁可重复穿
        chosen[i] = alt
        swaps += 1
    return swaps


def _l1_issues(chosen: list) -> dict[int, tuple[int, bool]]:
    """最终结果里贴身层还剩哪些违规：item_id → (穿了几次, 有没有连穿)。

    修复之后**必须拿最终结果复核一遍**。原因：`_repair_l1` 可能只改得动一部分
    （某天池子里没有那件内搭的落地方式），这时候如果因为"改过几套"就把提示撤掉，
    就成了一种新的静默失败 —— 页面看着正常，实际还是一件衣服穿三天。
    """
    days: dict[int, list[int]] = defaultdict(list)
    for i, c in enumerate(chosen):
        if not c:
            continue
        for it in c.get("items") or []:
            if it.get("slot") in NO_ADJACENT:
                days[int(it["id"])].append(i)
    cap = max(1, int(WEAR_CAP.get("L1", 2)))
    out: dict[int, tuple[int, bool]] = {}
    for iid, ds in days.items():
        adj = any(b - a == 1 for a, b in zip(ds, ds[1:]))
        if len(ds) > cap or adj:
            out[iid] = (len(ds), adj)
    return out


def _can_serve(by_id: dict, temps: list[float], items: set[int],
               occasion: Optional[str] = None,
               base_ok: Optional[list] = None,
               cap_gate: bool = True,
               comfort: Optional[float] = None) -> bool:
    """只带 `items` 这些衣服，这几天还能不能**每天各一套、而且穿得住**？

    判定三层，从便宜到贵：

      1. **每天都有合法搭配** —— 有哪天的可用内搭是空的，直接否
      2. **贴身层的总容量够不够** —— N 天至少要有 ⌈N/2⌉ 件内搭。
         **这一条就是"7 天只带一件 T 恤"的解药**：光要求"每天有得穿"的话，
         一件 T 恤配一条裤子也能穿满七天，试删会一路砍到那儿去。
      3. **真排一遍**（不连穿 + 单件不超上限），回溯搜

    ⚠️ 第 1 条的例外：**如果某天在原清单下本来就算不出搭配**（那个温度缺层位
    单品），那天不算"因为带少了"。否则试删会被它整体卡死 —— 实测真实衣橱
    有一天 31℃ 排不出来，那一趟旅行就一件都省不下来（`dropped_count=0`）。
    这种天按 None 交给 `_assign_l1` 跳过，容量也只按"能排出来的天"算。

    ⚠️ **但这个例外必须拿 `base_ok`（原始清单下每天排不排得出来）当尺子**，
    不能只看"这天现在是空的"。踩过（2026-09-22）：原来的写法对**所有**空天一律
    豁免，于是"砍完之后某天穿不了"和"衣橱本来就穿不了"被混成一件事 ——
    实测试删把两件卫衣全砍了，12℃ 那天（<14℃ 必需 L2+L3）直接没得穿，
    却因为"那天是空的"被当成不可抗力放行；紧接着重排填不满 7 天，
    代码按"宁可多带一件"退回原清单 → **一件都没省下来**（用户：
    "我希望少带衣服，结果要我带 7 条裤子"）。有 `base_ok` 之后，
    "本来能穿、砍完不能穿"一律否掉，只有本来就穿不了的天才算不可抗力。
    """
    subset = [by_id[i] for i in sorted(items) if i in by_id]
    if not subset:
        return False

    cap = max(1, int(WEAR_CAP.get("L1", 2)))
    n_l1 = sum(1 for it in subset if it["slot"] == "L1")

    domains: list = [_l1_domain(subset, t, occasion, comfort) or None for t in temps]
    if base_ok is not None:
        for i, d in enumerate(domains):
            if d is None and i < len(base_ok) and base_ok[i]:
                return False        # 本来穿得了、砍完穿不了 → 就是砍多了
    serve_days = [d for d in domains if d]
    if not serve_days:
        # 一天都排不出来 —— 那是缺层位单品，不是"带少了"。放行，
        # 让排布和提示去说这件事，别让它把"少带一件"整个功能关掉。
        return True
    if not cap_gate:
        # **原始清单本身**就轮不过来（内搭件数不够，比如他那 7 件 T 恤里有 4 件
        # 标了"只能单穿"，12℃ 只剩 3 件能当内搭）→ 容量和排班都不该当闸。
        # 踩过：拿容量当硬闸时，这种行程的 12 次试删全被判 False，
        # 一件都省不下来 —— 代价却是"本来就不可能修好的那件事"。
        # 现在放行：裤子鞋子照省，内搭有 `_keep_floor` 兜着，
        # 而"某件内搭得重复穿"由 warns 说实话（不能因为省不了就干脆不省）。
        return True
    if n_l1 * cap < len(serve_days):
        return False
    return _assign_l1(domains, cap)


def _keep_floor(slot: str, n_days: int, coldest: Optional[float] = None,
                comfort: Optional[float] = None,
                base_warmth: float = 0.0) -> int:
    """常识下限：有些东西再省也不该省。

      · 内搭：一件最多穿 2 天 → N 天至少 ⌈N/2⌉ 件（跟 `_can_serve` 的容量判定同源）
      · 中间层：一件最多穿 `L2_WEAR_CAP`(3) 天 → N 天至少 ⌈N/3⌉ 件。
        **2026-09-28 补**：少了它，试删会把贪心辛苦换出来的那几件卫衣又砍回去 ——
        `_can_serve` 只问"还排得出吗"，不问"十天穿同一件像话吗"
        （实测漏掉的正是这一条：18℃ 10 天排出 1 件卫衣穿 10/10 天）。
      · 下装：出门 4 天以上带两条 —— 一条身上、一条换洗，这是常识不是浪费
      · 鞋：至少一双
      · `TOP`：行程里最冷那天**光靠内搭垫不够**（26℃法则算出来的），就不能只剩内搭 ——
        **引擎允许"不穿外套"（23℃ 不穿很正常，这条是刻意的），
        但它不会主动替你想"18℃ 出门要加一件"。**
        踩过两次：第一次分界写死 17℃，于是 15℃ 的旅行会把卫衣和夹克全删掉；
        第二次连 17℃ 都没兜住 —— 18℃ 的旅行照样删光，重排出来每天一件短袖
        （用户原话："18 度怎么可能只穿短袖"，而他看到的正是旅行页）。

    调用方会再取 min(下限, 清单里本来就有的件数)，所以**不会凭空要求一件没带的东西**
    （比如整套清单里根本没有中间层时，这条就不生效）。
    """
    if slot == "L1":
        return _l1_need(n_days)
    if slot == "L2":
        return _l2_need(n_days)
    if slot == "L4":
        return 2 if n_days >= 4 else 1
    if slot == "L5":
        return 1
    if slot == "TOP":
        # 「26℃穿衣法则」口径：最冷那天**光靠内搭垫不够**，才需要留一件能加的。
        # 原来是写死的 WARM_LINE=20 —— 那样的线有两处（引擎 + 这里），迟早漂移。
        if coldest is None:
            return 0
        need = rules.comfort_of(comfort) - float(coldest)
        return 1 if need > base_warmth else 0
    return 0


def _l1_warmths(by_id: dict, pool) -> list[float]:
    """这批件里**内搭**的保暖值 —— 用来判"光靠内搭垫不垫得住最冷那天"。

    单看件数会误判：一件厚内搭能顶两件薄的。`_keep_floor("TOP")` 用它当基准。
    """
    out: list[float] = []
    for iid in pool:
        it = by_id.get(int(iid)) or {}
        if it.get("slot") == "L1":
            out.append(rules.warmth(it))
    return out


def _toppers_warm_enough(by_id: dict, pool, coldest: Optional[float]) -> int:
    """这批件里，**最冷那天真的穿得上**的中间层/外层有几件。

    口径用 `rules.item_temp_ok`（不传 path = 假设它暴露在外）——
    和引擎判"这件今天能不能穿"完全一致。它在这里实际卡的是**上限**：
    一件标 10~15℃ 的薄开衫在 18℃ 的行程里等于没有（那天穿它太热），不该算进来。
    """
    if coldest is None:
        return 0
    n = 0
    for iid in pool:
        it = by_id.get(int(iid)) or {}
        if it.get("slot") in ("L2", "L3") and rules.item_temp_ok(it, float(coldest)):
            n += 1
    return n


def shrink(by_id: dict, temps: list[float], items: set[int],
           used_days: Optional[dict] = None,
           occasion: Optional[str] = None,
           max_rounds: int = 4,
           warms: Optional[list[float]] = None,
           protect: Optional[set] = None,
           comfort: Optional[float] = None) -> tuple[set[int], list[int]]:
    """逐件试删：能从打包清单里拿掉的就拿掉。

    这就是"少带一件"的实现 —— 不是估算，是真的问一遍
    "不带它，这几天还能不能每天穿出不一样的一套"。

    试删顺序：**出场最少的那件先试**。它最可疑 —— 带了三天只穿一次的衣服，
    多半是可以不带的。

    但有三道闸：
      · `_can_serve` —— 拿掉之后还排得出「每天一套 + 内搭轮得过来」
      · `_keep_floor` —— 常识下限（内搭 ⌈N/2⌉ 件、裤子 4 天以上两条、鞋一双、
        冷天留一件能加的）。**没有它，纯按"还能不能排出来"试删，会把七天的清单
        压成"一件 T 恤 + 一条裤子"，或者 15℃ 出门不带外套。**
      · `protect` —— **用户手动标的「必带」，一件都不许试删。**
        这类件常常正好是"只出场一次"的那种（新买的外套、专门配的鞋），
        按出场次数排序时它们排在**最前面**，不保护就第一个被砍。

    `warms`（当天最低温）只用于算"这趟最冷几度" → 决定要不要留一件能加的，
    缺省就用 `temps`。
    """
    items = set(int(i) for i in items)
    used_days = used_days or {}
    protect = {int(i) for i in (protect or set())}
    n_days = len(temps)
    coldest = min(warms or temps) if (warms or temps) else None
    dropped: list[int] = []

    def slot_of(iid: int) -> str:
        return str((by_id.get(iid) or {}).get("slot") or "")

    # 下限按"清单里本来就有的件数"封顶（没有的层不设下限）
    base: dict[str, int] = defaultdict(int)
    for iid in items:
        base[slot_of(iid)] += 1
    floors = {
        "L1": min(_keep_floor("L1", n_days), base["L1"]),
        # 中间层的下限 —— 和 L1 同一个道理：N 天要换着穿，就至少得有 ⌈N/3⌉ 件。
        "L2": min(_keep_floor("L2", n_days), base["L2"]),
        "L4": min(_keep_floor("L4", n_days), base["L4"]),
        "L5": min(_keep_floor("L5", n_days), base["L5"]),
        # 上衣那层按"扛得住最冷那天"来数，不能只数件数 ——
        # 一件标着 10~15℃ 的薄开衫在 18℃ 的清单里等于没有。
        "TOP": min(_keep_floor("TOP", n_days, coldest, comfort,
                               max(_l1_warmths(by_id, items), default=0.0)),
                   _toppers_warm_enough(by_id, items, coldest)),
    }

    def floor_ok(pool: set[int]) -> bool:
        cnt: dict[str, int] = defaultdict(int)
        for iid in pool:
            cnt[slot_of(iid)] += 1
        if (cnt["L1"] < floors["L1"] or cnt["L2"] < floors["L2"]
                or cnt["L4"] < floors["L4"] or cnt["L5"] < floors["L5"]):
            return False
        if _toppers_warm_enough(by_id, pool, coldest) < floors["TOP"]:
            return False
        return True

    def order(u: set[int]) -> list[int]:
        return sorted(u, key=lambda i: (len(used_days.get(i) or []), i))

    # "在**原始清单**下，每天排不排得出搭配" —— 试删的尺子。
    # 有了它，"砍完之后某天穿不了"才不会被误当成"衣橱本来就穿不了"（见 `_can_serve`）。
    #
    # ⚠️ 判**当天最冷那会儿**（`warms`），不是出门温度。候选池就是按 warms 建的
    #    （`build_plan` 步①），可行性判定必须用同一个口径 —— 踩过：这里用 temps，
    #    于是 15℃ 的下午被当成"不需要中间层"，试删把两件卫衣全砍了，
    #    而那天早上 12℃（<14℃ 必需 L2+L3）根本穿不出门。**这是同一类错误第四次**：
    #    改了一个温度口径，另一处没跟着改（详见 `rules.item_temp_ok` 那张清单）。
    judge = list(warms) if warms else list(temps)
    _base_sub = [by_id[i] for i in sorted(items) if i in by_id]
    base_ok = [bool(_l1_domain(_base_sub, t, occasion, comfort)) for t in judge]
    # 原始清单本身够不够轮换（内搭件数 × 上限 ≥ 能排的天数）。
    # 不够时说明"这件事本来就做不到"，不能让容量判定把试删焊死（见 `_can_serve`）。
    _cap = max(1, int(WEAR_CAP.get("L1", 2)))
    _n_l1 = sum(1 for iid in items if slot_of(iid) == "L1")
    cap_gate = _n_l1 * _cap >= sum(1 for d in base_ok if d)

    for _ in range(max_rounds):
        changed = False
        for iid in order(items):
            if iid in protect:
                continue                       # 用户标的必带，不许试删
            trial = items - {iid}
            if not trial:
                continue
            if not floor_ok(trial):
                continue
            if _can_serve(by_id, judge, trial, occasion, comfort=comfort,
                          base_ok=base_ok, cap_gate=cap_gate):
                items = trial
                dropped.append(iid)
                changed = True
        if not changed:
            break
    return items, dropped


# ══════════════════════════════════════════════
# 打包清单
# ══════════════════════════════════════════════

def _packing(closet, item_days: dict[int, list[str]],
             extra_ids: Optional[Iterable[int]] = None) -> tuple[list[dict], list[dict]]:
    """打包清单 = 排布里穿过的每一件。

    `extra_ids` 是**必带、但一次都没排进穿搭**的那些（比如夏天硬要带件外套）。
    它们照样进箱子 —— 用户说"必带"就是必带，穿不穿是另一回事，
    `wear_count=0` + `must_only=True`，前端标成「备着」。
    这也是"清单和穿搭完全对得上"那条断言的新例外，测试里已跟着改。
    """
    by_id = {int(it["id"]): it for it in closet.items}
    rows = []
    seen: set[int] = set()
    for iid, ds in item_days.items():
        it = by_id.get(int(iid))
        if not it:
            continue
        seen.add(int(iid))
        rows.append({
            "id": int(iid), "name": it.get("name"), "slot": it.get("slot"),
            "category": it.get("category"), "color_main": it.get("color_main"),
            "color_hex": it.get("color_hex"), "is_neutral": it.get("is_neutral"),
            "img_display": it.get("img_display"),
            "days": ds, "wear_count": len(ds),
        })
    for iid in (extra_ids or ()):
        iid = int(iid)
        if iid in seen:
            continue
        it = by_id.get(iid)
        if not it:
            continue
        seen.add(iid)
        rows.append({
            "id": iid, "name": it.get("name"), "slot": it.get("slot"),
            "category": it.get("category"), "color_main": it.get("color_main"),
            "color_hex": it.get("color_hex"), "is_neutral": it.get("is_neutral"),
            "img_display": it.get("img_display"),
            "days": [], "wear_count": 0, "must_only": True,
        })
    rows.sort(key=lambda r: (SLOT_ORDER.index(r["slot"]) if r["slot"] in SLOT_ORDER else 9,
                             -r["wear_count"], r.get("name") or ""))
    groups = []
    for slot in SLOT_ORDER:
        g = [r for r in rows if r["slot"] == slot]
        if g:
            groups.append({"slot": slot, "name": SLOT_LABEL.get(slot, slot), "items": g})
    return rows, groups


# ══════════════════════════════════════════════
# 提示（本地生成，不依赖模型）
# ══════════════════════════════════════════════

def _tips(plan_days: list[dict], rows: list[dict], mode: str,
          dropped: list[int], closet,
          must: Optional[set] = None, must_left: Optional[set] = None) -> list[str]:
    tips: list[str] = []
    temps = [d["temp"] for d in plan_days if d.get("temp") is not None]
    # 温差要用**当天最低 / 最高**那两个数，不是折算出来的单值 ——
    # 用户看到的"13~22℃"就是这个区间，提示里得说同一件事。
    los = [d["tmin"] for d in plan_days if d.get("tmin") is not None]
    his = [d["tmax"] for d in plan_days if d.get("tmax") is not None]
    planned = [d for d in plan_days if d.get("outfit")]

    # 分段行程（两个旅游地）：先把"箱子只有一份"说清楚 —— 用户看到两个城市时
    # 最容易担心的就是这个（"难道要带两箱？"）。清单本来就是按整趟行程一起算的。
    segs: list[str] = []
    for d in plan_days:
        s = d.get("seg")
        if s and (not segs or segs[-1] != s):
            segs.append(str(s))
    if len(segs) >= 2:
        tips.append(f"这趟分 {len(segs)} 段（{' → '.join(segs)}）—— "
                    f"但**箱子只有一份**：清单是按整趟行程一起算的，"
                    f"冷的那段叠穿、热的那段脱掉，用的是同一批衣服。")

    # 必带先说 —— 用户勾的时候最关心的就是"这几件到底带上没有"
    if must:
        by_id = {int(it["id"]): (it.get("name") or "") for it in closet.items}
        left = sorted(int(i) for i in (must_left or ()))
        used = sorted(int(i) for i in must if int(i) not in set(left))
        head = f"必带 {len(must)} 件"
        if used and left:
            tips.append(
                f"{head}：{'、'.join(by_id.get(i, '') for i in used)} "
                f"排进了这几天的穿搭；"
                f"{'、'.join(by_id.get(i, '') for i in left)} "
                f"这几天的搭配排不进它，照样放进箱子备着。")
        elif used:
            tips.append(f"{head}都排进穿搭了 —— "
                        f"{'、'.join(by_id.get(i, '') for i in used)}。")
        else:
            # 一件都没穿上（这几天的温度压根不对）—— 这时候**不能**再写
            # "某某排进了这几天的穿搭"，那会拼出"必带 2 件： 排进了…"这种
            # 冒号后面空着的句子（空名字拼进模板，实测出现过）。
            tips.append(
                f"{head}：{'、'.join(by_id.get(i, '') for i in left)} "
                f"这几天的温度都穿不上它，但还是给你塞进箱子了 —— "
                f"你要带的，就不替你省。")

    # 清单里到底有没有"能加的那一层"。**凡是要提外套/中间层的句子，
    # 先过这一关** —— 踩过的坑：这些句子原来只按温度区间无条件生成，
    # 于是 31℃ 的夏天清单里一件外套都没有，页面却在说
    # "内搭多带一件换洗比多带一件外套有用"，用户读到的就是
    # "你说要带外套，穿搭里一件都没有"。宁可不说，也不能说没有的东西。
    toppers = [r for r in rows if r["slot"] in ("L2", "L3")]
    top_names = "、".join(str(r.get("name") or "") for r in toppers[:2])

    if temps:
        lo_t = min(los) if los else min(temps)
        hi_t = max(his) if his else max(temps)
        if toppers and hi_t - lo_t >= 12:
            tips.append(f"行程里最冷 {lo_t}℃、最暖 {hi_t}℃，差 {hi_t - lo_t} 度 —— "
                        f"{top_names} 要选能随时脱、随手搭在胳膊上的穿法。")
        elif toppers and hi_t - lo_t >= 6:
            tips.append(f"一天里 {lo_t}~{hi_t}℃，{top_names} 一件就够应付 —— "
                        f"早晚套上，中午脱下来（**穿几层是按当天最冷那会儿算的**）。")
        elif not toppers and lo_t <= 17:
            tips.append(f"行程里最冷 {lo_t}℃，但这趟清单里没有能加的那一层"
                        f"（中间层 / 外套）—— 那几天只能靠单件内搭。"
                        f"怕冷就添一件扛得住 {lo_t}℃ 的薄外套。")
        if lo_t <= 5:
            if toppers:
                tips.append(f"最低 {lo_t}℃，外层要够厚 —— 只看这一条，别的都可以将就。")
            else:
                tips.append(f"最低 {lo_t}℃，而清单里没有能扛这个温度的外层 —— "
                            f"这一趟会很冷，先别急着定。")
        if hi_t >= 28:
            tips.append(f"最高 {hi_t}℃，内搭多带一件换洗最实用"
                        f"（这个温度用不上外套）。")

    wet = [d for d in plan_days if d.get("wet")]
    if wet:
        names = "、".join(str(d.get("weekday") or d.get("date")) for d in wet[:4])
        tips.append(f"{len(wet)} 天下雨（{names}）—— 带伞，鞋别挑浅色和麂皮的。")

    looks = [_look(d["outfit"]) for d in planned]
    n_looks = len(set(looks))

    if mode == MODE_TRIP and rows:
        n_days = len(planned)
        uses = sum(r["wear_count"] for r in rows)
        tips.append(f"带 {len(rows)} 件，{n_days} 天里上衣不重样 {n_looks} 天 —— "
                    f"平均每件出场 {round(uses / len(rows), 1)} 次。")
        once = [r for r in rows if r["wear_count"] == 1]
        if once and n_days > 3:
            tips.append(f"{len(once)} 件只出场一次（"
                        f"{'、'.join(r['name'] for r in once[:4])}"
                        f"{'…' if len(once) > 4 else ''}）—— 逐件试过拿掉，"
                        f"拿掉就凑不齐这几天的搭配，否则它们不会留在清单里。")
        n_l1 = sum(1 for r in rows if r["slot"] == "L1")
        tips.append(f"内搭带了 {n_l1} 件（{n_days} 天至少要 {_l1_need(n_days)} 件）—— "
                    f"一件最多穿 {WEAR_CAP['L1']} 天，且不连穿两天。")
    elif rows:
        msg = f"这 {len(planned)} 天用到 {len(rows)} 件衣服，上衣不重样 {n_looks} 天"
        once = [r for r in rows if r["wear_count"] == 1]
        if once and len(planned) > 3:
            msg += f"，其中 {len(once)} 件只出现一次"
        tips.append(msg + "。")

    if dropped:
        tips.append(f"试删掉了 {len(dropped)} 件：不带它们，这趟照样每天穿得不一样。")

    if len(planned) < len(plan_days):
        tips.append(f"有 {len(plan_days) - len(planned)} 天排不出搭配 —— "
                    f"不是没衣服，是那个温度下缺一个层位的合格单品。")

    # 上衣重复：诚实说出来，别让用户自己发现。
    # 注意口径 —— **换裤子不算换穿搭**，同一件上衣穿第二次就是"看着像同一套"。
    dup = len(looks) - n_looks
    if dup:
        tips.append(f"有 {dup} 天跟别的天是同一件上衣（只换了裤子/鞋，那不算换了穿搭）——"
                    f" 这 {len(planned)} 天里只上过 {n_looks} 件上衣，"
                    f"想天天不一样就得再添上衣。")
    return tips


def _summary(plan_days: list[dict], rows: list[dict], mode: str) -> str:
    """给模型写的一句话总结（可选，失败静默）。"""
    from . import vision
    body = []
    for d in plan_days:
        if not d.get("outfit"):
            continue
        items = "、".join(it.get("name") or "?" for it in d["outfit"]["items"])
        body.append(f"{d.get('weekday') or d.get('date')} {d.get('temp')}℃"
                    f"{' ' + d['text'] if d.get('text') else ''}：{items}")
    if len(body) < 2:
        return ""
    return vision.summarize_plan(body, rows, mode)


# ══════════════════════════════════════════════
# 对外
# ══════════════════════════════════════════════

def build_plan(closet, days: list[dict], occasion: Optional[str] = None,
               weights: Optional[dict] = None, worn: Optional[dict] = None,
               mode: str = MODE_WEEK, seed: Optional[int] = 0,
               pool_size: int = POOL_MAX_LOOKS,
               use_llm: bool = True,
               must_ids: Optional[Iterable[int]] = None) -> dict:
    """排出多天的穿搭计划。

    `days` 只要给出 [{"date": "2026-09-21", "temp": 16, "text": "多云", ...}, ...]，
    其余字段（天气、星期）都是可选的展示用信息。**带 `tmin`/`tmax` 更好，见下。**

    `must_ids` = 用户手动标的「**必带**」（旅行页那个选择器）。语义：

      · **能穿就一定穿上去** —— 排布时优先挑含它的搭配（`_cost` ⑥）
      · **试删一件都不许砍** —— 这类件常常正好"只出场一次"，按出场次数排序
        它们排在最前面，不保护就第一个被删掉
      · **实在穿不上（温度不对）也照样进箱子**，标成「备着」（`wear_count=0`）

    第三条是关键：`_packing` 原来只收"排布里穿过的件"，于是"必带但没排上"
    的那件会**无声消失** —— 用户明明勾了，清单里却没有。宁可在清单里多一行「备着」，
    也不能让勾选失效。

    **两个温度，别混用**（2026-09-21 按用户反馈"13-22 度也穿短袖"拆开）：
      · `temps`（出门那会儿，档位决定）= 打分 / 舒适度 / 好不好看；
      · `warms`（当天最低温）= 穿几层、够不够暖、要不要留一件能加的。
    没有 `tmin` 时 `warms` 自动回落到 `temps`（手填温度是单值，没有区间）。

    口径（见模块开头）：跨天"算不算换了穿搭"看**上衣**（`n_looks`），
    贴身层一件最多穿 `WEAR_CAP['L1']` 天且不连穿。
    """
    mode = MODE_TRIP if mode == MODE_TRIP else MODE_WEEK
    weights = weights or {"item": {}, "pair": {}}
    worn = worn or {}
    days = [dict(d) for d in (days or []) if d]
    if not days:
        return {"ok": False, "reason": "没有可排的日期。", "days": [], "packing": [],
                "packing_groups": [], "tips": [], "warnings": []}

    # 必带：只认真的在衣橱里的 id（前端可能带着已删单品的旧 id）
    have = {int(it["id"]) for it in closet.items}
    must = {int(i) for i in (must_ids or ())} & have

    # 「26℃穿衣法则」的体感舒适温度。**从 closet 上取**，不在这里读配置 ——
    # 排布用的引擎和判"够不够暖"的辅助函数必须用同一条线，否则会互相打架。
    comfort = getattr(closet, "comfort", None)

    temps = [_temp_of(d) for d in days]
    warms = [_warm_of(d) for d in days]
    keys = [str(d.get("date") or "") for d in days]

    # ── 1. 每天各取候选池（按"看得见的组合"去重，见 _day_pool）──
    #     枚举用 warms（决定"允不允许穿外套"这个层位存在，
    #     15~21℃ 的一天早上 15℃ 就需要那个层位），打分用 temps。
    pools: list[list[dict]] = []
    for i, d in enumerate(days):
        res = _day_pool(closet, warms[i], occasion, weights, worn, seed,
                        max_looks=pool_size, score_temp=temps[i], must=must)
        pools.append(res["outfits"])
        d["_cores"] = res["cores"]
        d["_variants"] = res["variants"]

    empty_days = [i for i, p in enumerate(pools) if not p]

    # ── 2. 贪心排一遍（temps 判好不好看，warms 判够不够暖）──
    g = _greedy(pools, temps, warms, keys, must, comfort=comfort)
    warns = list(g["warns"])
    cur_pools = pools          # g 是从哪批候选池排出来的（旅行重排后要跟着换）

    # ── 3. 先回头修一遍贴身层，**再**谈少带件 ──
    #     ⚠️ 顺序不能反（2026-09-22 修）。`_can_serve`（试删的可行性判定）会算
    #     "内搭容量够不够"（⌈N/2⌉ 件 × 每件最多 2 天），而 `_greedy` 逐天决策、
    #     可能排出一件 T 恤穿 3 天 —— 真实衣橱实测就排出来过。
    #     **拿一份自身就违规的清单去试删，容量判定必然为 False：
    #     实测 12 次 `_can_serve` 全是 False，一件都砍不掉**，用户看到的是
    #     "旅行要带 7 条裤子"，而"少带件"这个功能静默失效（第 10 次静默失败）。
    #     先把贴身层修成合规的（它就是"这几天排得出来"的见证），试删才有意义。
    if _repair_l1(cur_pools, g["chosen"], keys, warms, comfort=comfort):
        g["item_days"] = _recount_item_days(g["chosen"], keys)
        # ⚠️ 只撤「内搭」那句 —— 中间层的重复不归 `_repair_l1` 管（见 `_drop_l1_repeat`）。
        warns = _drop_l1_repeat(warns)

    # ── 4. 旅行：逐件试删，能少带就少带 ──
    dropped: list[int] = []
    if mode == MODE_TRIP:
        used = {int(i) for i in g["item_days"]}
        if used:
            by_id = {int(it["id"]): it for it in closet.items}
            kept, dropped = shrink(by_id, temps, used, g["item_days"], occasion,
                                   warms=warms, protect=must, comfort=comfort)
            if kept != used:
                # 用精简后的衣服**重新排**，而不是拿原来的候选池去筛 ——
                # 池子里的套多半用到了被砍掉的衣服，筛完会所剩无几。
                # 重排仍然用同一套代价：箱子已经定了，这一步只管排得好看。
                sub = graph.Closet([by_id[i] for i in sorted(kept) if i in by_id],
                                   comfort=comfort)
                pools2 = []
                for i, d in enumerate(days):
                    r2 = _day_pool(sub, warms[i], occasion, weights, worn, seed,
                                   max_looks=pool_size, score_temp=temps[i], must=must)
                    pools2.append(r2["outfits"])
                g2 = _greedy(pools2, temps, warms, keys, must, comfort=comfort)
                # 精简后反而排得更差了（少一天）→ 退回精简前的结果。
                # 宁可多带一件，也不能因为"省地方"让你有一天没得穿。
                if (sum(1 for c in g2["chosen"] if c)
                        >= sum(1 for c in g["chosen"] if c)):
                    g = g2
                    cur_pools = pools2
                    # ⚠️ **先撤掉旧的那条同类警告，再合并**（2026-09-29 修）。
                    # 直接 `warns + g2["warns"]` 会让同一件事说两遍：
                    # 两次 `_greedy` 拿的是**不同的候选池**（第一次是全体，
                    # 第二次是试删后"只含箱子里那几件"的池子），因此"哪一层顶到上限"
                    # 未必相同 —— 实测 10 天场景里，第一次报「内搭不够换…」，
                    # 第二次报「内搭不够换…；中间层得带够 3 件…」，两条都留下，
                    # 用户读到的是同一件事的两种说法（还互相看起来像矛盾）。
                    # 最终清单出自 g2，所以**以 g2 的说法为准**。
                    warns = [w for w in warns if REPEAT_WARN_MARK not in w]
                    warns = list(dict.fromkeys(warns + list(g2["warns"])))
                else:
                    dropped = []

    # ── 5. 箱子定了、按箱子重排之后，贴身层可能又违规 → 再修一遍 ──
    #     （`cur_pools` 这时已经是"只含箱子里的件"的池子，改不出箱外的东西。
    #      没违规时 `_repair_l1` 直接早退，所以这一步几乎不花时间。）
    if _repair_l1(cur_pools, g["chosen"], keys, warms):
        g["item_days"] = _recount_item_days(g["chosen"], keys)
        # 同上：只撤内搭那句。（箱子定了之后重排，中间层那句由 `_greedy` 自己给。）
        warns = _drop_l1_repeat(warns)

    # 修完必须拿**最终结果**复核一遍：修不动的时候要说实话，不能因为"改过几套"
    # 就把提示撤掉（那就是一种新的静默失败）
    issues = _l1_issues(g["chosen"])
    if issues and not any(_is_l1_repeat(w) for w in warns):
        n_ok = sum(1 for c in g["chosen"] if c)
        worst = max(issues.values(), key=lambda x: x[0])
        # 同上：先分清"衣橱里能当内搭的本来就少"还是"带得不够"。
        # `cur_pools` 这时只含箱子里的件，正好是"实际能用的那几件"。
        _avail = len({int(_it["id"]) for _pl in cur_pools for _c in _pl
                      for _it in _cand_items(_c) if _it.get("slot") == "L1"})
        _need_ok = _l1_need(n_ok)
        if _avail and _avail < _need_ok:
            warns.append(
                f"贴身层轮不过来：{len(issues)} 件内搭穿超了（最多的一件穿了 "
                f"{worst[0]} 天{'、还连穿了' if worst[1] else ''}）—— "
                f"{n_ok} 天要有 {_need_ok} 件才轮得开，而这个温度下能当内搭的"
                f"只有 {_avail} 件（其余标了「只能单穿」，一叠层就穿不上）。"
                f"想不重样，去「改字段」把标签改掉，或者再添两件。")
        else:
            warns.append(
                f"贴身层轮不过来：{len(issues)} 件内搭穿超了（最多的一件穿了 "
                f"{worst[0]} 天{'、还连穿了' if worst[1] else ''}）—— "
                f"{n_ok} 天至少要有 {_need_ok} 件内搭，而且每天都得留得出别的选择。")

    chosen = g["chosen"]
    item_days = g["item_days"]
    # 必带但一次都没排上的（温度不对）→ 照样进箱子，标「备着」
    must_left = {int(i) for i in (g.get("must_left") or set())}
    rows, groups = _packing(closet, item_days, extra_ids=must_left)

    plan_days: list[dict] = []
    for i, d in enumerate(days):
        pick = chosen[i]
        plan_days.append({
            "date": d.get("date"), "weekday": d.get("weekday") or weekday_of(d.get("date")),
            "temp": d.get("temp"), "temp_am": d.get("temp_am"), "temp_pm": d.get("temp_pm"),
            "tmin": d.get("tmin"), "tmax": d.get("tmax"),
            "text": d.get("text") or "", "code": d.get("code"),
            "wet": bool(d.get("wet")), "precip": d.get("precip"),
            # 分段的旅行：这一天待在哪一段（叫"京都"还是"大阪"）。
            # 纯粹是展示用的透传 —— 排布一点不看它（引擎不知道"城市"）。
            "seg": d.get("seg"),
            "cores": d.get("_cores"), "variants": d.get("_variants"),
            "outfit": pick,
            "empty_reason": None if pick else ("no_legal_outfit" if i in empty_days else "no_pick"),
        })

    planned = [d for d in plan_days if d["outfit"]]
    looks = [_look(d["outfit"]) for d in planned]
    worn_ids = {int(i) for i in item_days}
    tips = _tips(plan_days, rows, mode, dropped, closet,
                 must=must, must_left=must_left)
    summary = _summary(plan_days, rows, mode) if use_llm else ""

    def _name(i: int) -> str:
        return next((str(x.get("name") or "") for x in closet.items
                     if int(x["id"]) == int(i)), "")

    return {
        "ok": True, "mode": mode, "occasion": occasion,
        "days": plan_days,
        "packing": rows,
        "packing_groups": groups,
        "n_days": len(plan_days), "n_planned": len(planned),
        "n_items": len(rows),
        # 主角是 n_looks（上衣不重样）—— "算不算换了穿搭"看上衣。
        # n_cores 留着：它是"外套+下装+鞋"的组合数，比 looks 粗一级，
        # 两个数一起看能看出"是不是只换了裤子"。
        "n_looks": len(set(looks)),
        "n_cores": len({_core(d["outfit"]) for d in planned}),
        "l1_cap": int(WEAR_CAP.get("L1", 2)),
        "l1_need": _l1_need(len(planned)),
        # 必带：勾了几件 / 真排进穿搭的 / 只进箱子没穿上的（标「备着」）
        "must_ids": sorted(must),
        "must_used": sorted(must & worn_ids),
        "must_used_names": [_name(i) for i in sorted(must & worn_ids)],
        "must_backup": sorted(must_left),
        "must_backup_names": [_name(i) for i in sorted(must_left)],
        "dropped_count": len(dropped),
        "dropped": [r for r in (
            {"id": i, "name": _name(i)} for i in dropped)],
        "warnings": warns,
        "tips": tips,
        "summary": summary,
        "summary_used": bool(summary),
    }


# ══════════════════════════════════════════════
# 天气 → days
# ══════════════════════════════════════════════

def days_from_weather(fc: dict, start: Optional[str] = None,
                      count: Optional[int] = None) -> list[dict]:
    """把 weather.forecast() 的结果转成 build_plan 要的 days。

    顺手做了两件小事：切出从 `start` 开始的那一段，以及把 15 天以上的长预报截断
    （预报越远越不准，排到第 14 天之后没有意义）。
    """
    rows = list(fc.get("days") or [])
    if start:
        s = str(start)[:10]
        rows = [r for r in rows if str(r.get("date") or "")[:10] >= s]
    if count:
        try:
            rows = rows[:max(1, int(count))]
        except (TypeError, ValueError):
            pass
    return [dict(r) for r in rows]
