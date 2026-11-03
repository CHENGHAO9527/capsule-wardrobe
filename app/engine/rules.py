"""硬规则引擎 —— 判物理可行性。

规则错了是穿出门难看的物理错误，写死比交给模型稳定一百倍。
见 docs/02-推荐引擎算法流程.md 阶段 2。

通用规则 R1-R5 + 叠穿规则 D1-D7。
唯一的软规则（体积与轮廓）不在这里，交给大模型。
"""

from __future__ import annotations

import colorsys
import json
import re
from functools import lru_cache
from typing import Optional

# ── 层序：由内到外 ──
LAYER_ORDER = ["L1", "L2", "L3", "L4I", "L4", "L5"]
# L1 内搭 / L2 中间层 / L3 外层 / L4I **内穿下装**（秋裤、打底裤）/ L4 下装 / L5 鞋 / ACC 配饰
#
# L4I 排在 L4 **前面**：枚举时先内后外，`_layer_index` 也靠这个顺序判"外面还有没有东西"。
# 它跟 L4 的关系，就等于 L1 跟 L3 的关系 —— **必须被盖住**。
# 用户的诉求：「秋裤按 L4 录进来就必然被当成外穿下装，30℃ 那天排出一条秋裤」。

# 参与叠层的层（L4/L5 是独立层，不参与厚度单调链）
STACK_LAYERS = ["L1", "L2", "L3"]

# 衣长档位排序（D3）
LENGTH_RANK = {"短": 1, "常规": 2, "长": 3, "及踝": 4}

# 领型分组（D2）
COLLAR_HIGH = {"高领"}
COLLAR_FLIP = {"翻领"}

# 「套头」外层（D7）：必须先钻过头才能穿上去的那一类 —— 会把内层那片门襟整个压住。
# ⚠️ 这是**穿法**分类，不是风格分类，别往里加"看起来厚"的品类：
# 皮衣有拉链、厚衬衫有整排扣子，它们都是**敞开**穿的，门襟看得见也不硌。
PULLOVER_CATS = {"毛衣", "卫衣"}

# 场合 → 正式度许可区间
OCCASION_FORMALITY = {
    "居家": (1, 2),
    "运动": (1, 2),
    "休闲": (1, 3),
    "通勤": (2, 4),
    "约会": (2, 4),
    "商务": (3, 5),
    "正式": (4, 5),
}

DEFAULT_FORMALITY = 3
DEFAULT_THICKNESS = 3


# ══════════════════════════════════════════════
# 色彩工具
# ══════════════════════════════════════════════

def hsv_from_hex(hex_str: Optional[str]) -> Optional[tuple[float, float, float]]:
    """#RRGGBB → (h 0-1, s 0-1, v 0-1)。解析失败返回 None。"""
    if not hex_str:
        return None
    s = hex_str.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    return colorsys.rgb_to_hsv(r, g, b)


def hue_delta(h1: float, h2: float) -> float:
    """色相环上的最短夹角（度）。"""
    d = abs(h1 - h2) * 360
    return min(d, 360 - d)


# ══════════════════════════════════════════════
# 穿搭标签（会改变搭配结果，不是装饰）
# ══════════════════════════════════════════════
#
# 用户需求原话：「同样是短袖，有些是夏天单穿的，不适合秋天作为内搭，因为版型不合适。」
#
# **这个表里只放引擎真的会读的标签。** 放一个没有效果的标签进来，
# 用户会以为它生效了 —— 这个项目已经栽过三次"看起来接上了、实际没启动"的跟头
# （img_* 被静默丢弃 / UPSERT 吞增量 / thinking 默认开启）。
#
# 标签只对 L1、L2 有意义：
#   · L3 是外层，定义上就不可能被盖住
#   · L4/L5 根本不参与叠穿
# 所以 UI 也只在 L1/L2 上显示按钮，避免出现"点了没反应"的控件。

TAG_SOLO = "solo_only"      # 只能单穿：不能被任何上装盖住
TAG_INNER = "never_outer"   # 只作内搭：必须被盖住，不能当最外层

# 互斥标签：两个都打上 = 既不能被盖住、又必须被盖住，这件衣服会被夹死，
# 一条搭配都进不去。前端切换时会自动摘掉另一个。
TAG_MUTEX = {TAG_SOLO: TAG_INNER, TAG_INNER: TAG_SOLO}

TAGS = [
    {"key": TAG_SOLO, "name": "只能单穿",
     "desc": "不能被中间层或外套盖住 —— 只在它自己当最外层时出现",
     "slots": ["L1", "L2"]},
    {"key": TAG_INNER, "name": "只作内搭",
     "desc": "必须被盖住 —— 不会单独作为最外层出现",
     "slots": ["L1", "L2"]},
]
TAG_NAMES = {t["key"]: t["name"] for t in TAGS}


@lru_cache(maxsize=1024)
def _parse_tags_cached(raw: str) -> frozenset[str]:
    """tags 字段 → 标签集合。接受 JSON 数组，也接受逗号/顿号分隔的手写值。

    **解析失败一律当空集。** 标签是附加约束，读不懂就当没有 ——
    不能让一条脏标签把整件衣服从所有搭配里静默踢出去。
    """
    raw = (raw or "").strip()
    if not raw:
        return frozenset()
    try:
        val = json.loads(raw)
    except Exception:
        val = None
    if isinstance(val, list):
        return frozenset(str(x).strip() for x in val if str(x).strip())
    return frozenset(p.strip() for p in re.split(r"[,，、;；]", raw) if p.strip())


def item_tags(it: dict) -> frozenset[str]:
    """取一件单品的标签集合。带缓存，DFS 叶子判定里反复取也不心疼。"""
    raw = it.get("tags")
    if isinstance(raw, (list, tuple, set)):
        return frozenset(str(x).strip() for x in raw if str(x).strip())
    return _parse_tags_cached(str(raw or ""))


def dump_tags(keys) -> str:
    """标签集合 → 入库用的 JSON 数组字符串。空集合存空串。"""
    seen: set[str] = set()
    out: list[str] = []
    for k in keys or []:
        k = str(k).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return json.dumps(out, ensure_ascii=False) if out else ""


def tag_conflict(path: list[dict]) -> Optional[str]:
    """标签与层位的冲突。整套判定，返回 None 表示通过。

    **判据只写在这一处**，由 `outfit_ok` 调用。写两遍就会漂移 ——
    温度口径就是这么踩了三次（改了一个，另外两处没跟着改，放宽被静默抵消）。

    · 「只能单穿」的件不能被任何上装盖住 —— 版型偏大的短袖夏天单穿没问题，
      塞进卫衣/外套里会鼓成一团。
    · 「只作内搭」的件必须被盖住 —— 反过来，打底衫不该单穿出门。
    """
    stack = [it for it in path if it.get("slot") in STACK_LAYERS]
    if not stack:
        return None
    top = max(stack, key=lambda it: LAYER_ORDER.index(it["slot"]))
    for it in stack:
        t = item_tags(it)
        if not t:
            continue
        name = it.get("name") or "这件"
        if TAG_SOLO in t and it is not top:
            return f"「{name}」标了只能单穿，不能被中间层或外套盖住"
        if TAG_INNER in t and it is top:
            return f"「{name}」标了只作内搭，不能单独当最外层"
    return None


# ══════════════════════════════════════════════
# 通用规则 R1-R5
# ══════════════════════════════════════════════

def r1_same_slot(a: dict, b: dict) -> Optional[str]:
    """R1 同槽位不搭。配饰除外。"""
    if a["slot"] == b["slot"] and a["slot"] != "ACC":
        return "同槽位"
    return None


def r2_temp(a: dict, b: dict) -> Optional[str]:
    """R2 两件**都露在外面**时，温度区间必须有交集。

    原来的写法是"先把被覆盖层的下限放开到 -99，再取交集" —— 那是给
    「分层阶梯」打的补丁（长袖 T 恤标 15~30℃，可「T恤+卫衣+羽绒服」正是冬天标配）。

    换成「26℃穿衣法则」之后，上装之间的温度兼容由 `stack_warm_ok`
    （够不够暖）+ 最外层上限（会不会太热）统一负责，成对这里**只管两个都暴露的层**：
    L4 下装 × L5 鞋，以及 L4 自己之间。要防的是「夏天短裤 + 冬天长靴」这种错配。
    L4I 被外裤盖住，跟谁都不冲突。
    """
    HIDDEN = TOP_SLOTS + ("L4I",)
    if a.get("slot") in HIDDEN or b.get("slot") in HIDDEN:
        return None
    lo = max(_temp_min(a), _temp_min(b))
    hi = min(_temp_max(a), _temp_max(b))
    if hi < lo:
        return "温度区间不相交"
    return None


def r3_formality_gap(a: dict, b: dict, max_gap: int = 2) -> Optional[str]:
    """R3 正式度差 ≤ 2。`HIDDEN_SLOTS`（秋裤）不参与 —— 它不决定今天正式不正式。"""
    if a.get("slot") in HIDDEN_SLOTS or b.get("slot") in HIDDEN_SLOTS:
        return None
    if abs(_formality(a) - _formality(b)) > max_gap:
        return f"正式度差过大（{_formality(a)} vs {_formality(b)}）"
    return None


def r4_colors(a: dict, b: dict) -> Optional[str]:
    """R4 两个高饱和色不搭（色相差 > 90°）。R5 中性色无限兼容。

    `HIDDEN_SLOTS`（秋裤）直接放行 —— 被外裤完全盖住，撞不撞色都看不见。
    """
    if a.get("is_neutral") or b.get("is_neutral"):
        return None
    if a.get("slot") in HIDDEN_SLOTS or b.get("slot") in HIDDEN_SLOTS:
        return None
    ha = hsv_from_hex(a.get("color_hex"))
    hb = hsv_from_hex(b.get("color_hex"))
    if not ha or not hb:
        return None
    if ha[1] > 0.5 and hb[1] > 0.5 and hue_delta(ha[0], hb[0]) > 90:
        return "撞色"
    return None


# ══════════════════════════════════════════════
# 叠穿规则 D1-D6（内层 inner，外层 outer）
# ══════════════════════════════════════════════

def d1_thickness(inner: dict, outer: dict) -> Optional[str]:
    """D1 厚度单调：内薄外厚。"""
    if _thickness(outer) < _thickness(inner):
        return "厚度倒挂（内厚外薄）"
    return None


def d2_collar(inner: dict, outer: dict) -> Optional[str]:
    """D2 领型兼容。

    只有「高领内搭 + 翻领衬衫」这种同属内/中层的组合才会领子打架。
    外层大衣的翻领是敞开的 —— 高领配翻领大衣是经典组合，不能拦。
    圆领内搭 + 开领外套永远成立。
    """
    ci, co = (inner.get("collar") or ""), (outer.get("collar") or "")
    if ci in COLLAR_HIGH and co in COLLAR_FLIP and outer.get("slot") in ("L1", "L2"):
        return "领型冲突（高领 + 翻领衬衫）"
    return None


def d3_length(inner: dict, outer: dict) -> Optional[str]:
    """D3 长度关系：外套下摆 ≥ 内层下摆。

    已放宽一档：短款外套配常规内搭（衬衫下摆露出）是常见穿法，
    严格 >= 会误杀。差 2 档以上才判冲突。
    """
    ri = LENGTH_RANK.get(inner.get("length_band") or "", 2)
    ro = LENGTH_RANK.get(outer.get("length_band") or "", 2)
    if ro < ri - 1:
        return "长度冲突（外套明显短于内层）"
    return None


def d5_formality_mono(inner: dict, outer: dict) -> Optional[str]:
    """D5 正式度单调：外层不低于内层。

    已放宽：允许一档倒挂。羽绒服（休闲）里穿衬衫是完全正当的穿法，
    严格单调会误杀整个冬季衣橱。倒挂 2 档以上才拦。
    西装外套 + 卫衣 由 R3（整套极差 ≤ 2）负责拦，不靠这条。
    """
    if _formality(inner) - _formality(outer) > 1:
        return "正式度倒挂（外比内随意两档以上）"
    return None


def d6_sleeve(inner: dict, outer: dict) -> Optional[str]:
    """D6 袖型。短袖内搭 + 长袖外套永远安全。

    已放宽：长袖 + 长袖只在两件都修身时判冲突
    （常规衬衫配西装外套是正当穿法，不该拦）。
    """
    si, so = (inner.get("sleeve") or ""), (outer.get("sleeve") or "")
    if si == "长袖" and so == "长袖":
        if inner.get("fit") == "修身" and outer.get("fit") == "修身":
            return "袖型冲突（两件修身长袖叠穿）"
    return None


# ══════════════════════════════════════════════
# 组合判定
# ══════════════════════════════════════════════

def d7_placket(inner: dict, outer: dict) -> Optional[str]:
    """D7 门襟 × 套头：**开领**（有门襟）的内搭不能被"套头"外层盖住。

    用户原话：「我有一件亨利领的衣服，它既不是只能单穿，也不是只能做内搭。
    它可以穿在外面，也可以作为内搭在外面穿个夹克；但是，它不能作为内搭
    在外面再穿个卫衣。这个地方你自己想一想，应该怎么去解决。」

    **他说的是"怎么穿上身"，不是"好不好看"** —— 所以解在规则里，不该逼他
    在两个标签之间找落脚点（`solo_only` / `never_outer` 都是**绝对**判定，
    而这件衣服是"相对外层的穿法而定"，本质上不是标签能表达的东西）：
      · **套头**外层（卫衣 / 毛衣）得先钻过头 → 内层那片门襟被整个压住，
        硌、皱，而且领口以下全看不见；
      · **敞开**的外层（夹克 / 大衣 / 皮衣 / 衬衫…）门襟照露，完全没问题。

    ⚠️ **"有门襟"只用 `collar == "开领"` 判**，这是模型已经替我们分好的：
    真实衣橱里「黑色亨利领长袖」被识别成 `开领`（`L1/T恤/薄`），
    而两件衬衫一律是 `翻领`。
    ⚠️ **翻领不能拦**：翻领会从套头领口露出来，那正是"衬衫 + 毛衣"穿法的全部意义；
    拦了就把一整个经典组合误杀。`d2_collar` 只拦"高领 + 翻领衬衫"也是同理 ——
    **能露出来的领子不是问题，被压住的才有问题。**
    """
    if (inner.get("collar") or "") != "开领":
        return None
    if (outer.get("category") or "") not in PULLOVER_CATS:
        return None
    name_i = inner.get("name") or "这件"
    name_o = outer.get("name") or "外面那件"
    return (f"「{name_i}」的开领（门襟）会被「{name_o}」整个压住 —— "
            f"套头穿进去要钻过头，门襟又全看不见；想叠它得配敞开的外套")


STACK_CHECKS = (d1_thickness, d2_collar, d5_formality_mono, d6_sleeve, d7_placket)


def pair_ok(a: dict, b: dict, adjacent: bool = True) -> Optional[str]:
    """两件能否共存。返回 None 表示通过，否则返回冲突原因。

    adjacent=True 表示 a、b 在层序上相邻（会跑叠穿规则）。
    """
    for check in (r1_same_slot, r2_temp, r3_formality_gap, r4_colors):
        reason = check(a, b)
        if reason:
            return reason
    if adjacent:
        inner, outer = (a, b) if _layer_index(a) <= _layer_index(b) else (b, a)
        for check in STACK_CHECKS:
            reason = check(inner, outer)
            if reason:
                return reason
    return None


def outfit_ok(path: list[dict], temp: Optional[float] = None,
              occasion: Optional[str] = None,
              comfort: Optional[float] = None) -> Optional[str]:
    """整套校验（DFS 落叶子时调用）。

    补几条成对判定管不到的整体约束：
      · 整套正式度极差 ≤ 2
      · 每一层都适用于当天（口径见 `item_temp_ok`：上装判"够不够暖 / 会不会太热"，
        下半身判自己的双边区间）
      · 场合的正式度许可区间
      · 穿搭标签的层位约束（「只能单穿」不能被盖住 / 「只作内搭」必须被盖住）
    """
    if not path:
        return "空搭配"

    forms = [_formality(it) for it in path if it.get("slot") not in HIDDEN_SLOTS]
    if forms and max(forms) - min(forms) > 2:
        return f"整套正式度极差 {max(forms) - min(forms)} 过大"

    reason = tag_conflict(path)
    if reason:
        return reason

    if temp is not None:
        bad = [it for it in path if not item_temp_ok(it, temp, path, comfort)]
        if bad:
            # 说清楚是"不适用"还是"不够暖" —— 光看"不适用"用户不知道差在哪
            tops = [it for it in path if it.get("slot") in TOP_SLOTS]
            if tops and not stack_warm_ok(tops, temp, comfort):
                gap = comfort_of(comfort) - temp - stack_warmth(tops)
                return (f"这套上装不够暖：体感还差 {gap:.0f}℃"
                        f"（{temp:g}℃ + 保暖 {stack_warmth(tops):g} < "
                        f"{comfort_of(comfort):g}）")
            return f"「{bad[0].get('name', '某件')}」不适用 {temp}℃"

    if occasion:
        lo, hi = OCCASION_FORMALITY.get(occasion, (1, 5))
        avg = sum(forms) / len(forms) if forms else 3.0
        if not (lo - 0.5 <= avg <= hi + 0.5):
            return f"正式度 {avg:.1f} 不符合「{occasion}」场合"

    return None


# ══════════════════════════════════════════════
# 内部取值（字段缺失时给安全默认值）
# ══════════════════════════════════════════════

def _temp_min(it: dict) -> int:
    v = it.get("temp_min")
    return -10 if v is None else int(v)


def _temp_max(it: dict) -> int:
    v = it.get("temp_max")
    return 40 if v is None else int(v)


# 温度字段缺失时的兜底 → 视为全域适用，不因为缺数据把单品误杀
def temp_min(it: dict) -> int:
    return _temp_min(it)


def temp_max(it: dict) -> int:
    return _temp_max(it)


# ══════════════════════════════════════════════
# 温度：「26℃穿衣法则」（2026-09-23 换掉原来的"分层阶梯"）
# ══════════════════════════════════════════════
#
#     当日气温 + 衣物保暖值之和 = 体感舒适线（`prefs.comfort()`，可调，默认 22）
#
# 三条：
#   ① 够不够暖 ── 气温 + Σ(上装保暖值) ≥ 舒适线
#   ② 会不会太热 ── 气温 ≤ **最外层上装**的上限（只看露在最外面的那件）
#   ③ 下半身只参与搭配 ── L4/L5 各自区间双边成立，**不参与保暖加总**
#
# ⚠️ ② 必须**只看最外层**。第一版拿"体感温度"去比被盖住那件的上限，结果是
#    越冷越判不合法（8℃ 三层被判成体感 32℃ 超了 T 恤的 30℃，5℃ 反倒合法）——
#    **曲线非单调**。被盖住的层不参与"太热"判定。
#
# 为什么换掉原来的"分层阶梯"（`layer_plan` 在 24/18/14 三档切层数 + 每层各自双边校验）：
# 实测（27 件真实衣橱，逐度扫核心组合数）24→23℃ +50%、**14→13℃ −81%**，
# 而且**规则和数据打架** —— 库里的衬衫标着 10~25℃，24℃ 却不许出现第二层。
# 完整论证与实测数字见 MEMORY-DETAIL §四。

# 上半身：**只有这三层参与保暖加总**
TOP_SLOTS = ("L1", "L2", "L3")

# 字段缺省时按厚度兜底（老数据、新品类还没填保暖值）。
# 数值对齐法则那张表：薄棉毛衫 1 / 厚棉毛衫 2 / 抓绒~薄外套 3 / 厚羊毛衫~絮棉衣 5 / 羽绒 8
_WARMTH_BY_THICKNESS = {1: 1, 2: 2, 3: 3, 4: 5, 5: 8}
DEFAULT_WARMTH = 2

# 舒适线的默认值与硬边界。**真正的值走 `prefs.comfort()`**；
# 这里只是"没传就是它"的兜底 —— 让 rules 保持纯函数、可单测。
# 边界和 `prefs` 的 clamp 保持一致，防止调用方塞进一个荒唐的数。
DEFAULT_COMFORT = 22.0
COMFORT_MIN, COMFORT_MAX = 18.0, 28.0


# **完全**看不见的层：颜色不外露、正式度也不参与。
# 秋裤穿在西裤里面，跟别的件撞不撞色没人看得见，也没人拿它衡量"今天够不够正式" ——
# 拿它参与判定只会凭空砍掉合法搭配（西裤 + 秋裤本是冬天标配）。
# 内搭 L1 不在此列：领口袖口会露出来，颜色确实有关系。
HIDDEN_SLOTS = ("L4I",)


def warmth(it: dict) -> float:
    """这一件能替体感垫掉多少度。**只有上装算数，下半身恒 0**。

    字段没填就按厚度兜底 —— 兜底值必须**确定性**：同一个厚度永远同一个数，
    否则"改了别的字段搭配就变"这种幽灵 bug 又会回来。
    """
    if it.get("slot") not in TOP_SLOTS:
        return 0.0
    v = it.get("warmth")
    if v is None:
        v = _WARMTH_BY_THICKNESS.get(_thickness(it), DEFAULT_WARMTH)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(DEFAULT_WARMTH)


def comfort_of(comfort: Optional[float] = None) -> float:
    """归一化舒适线。传 None 就用默认值（测试和脚本不必每次都给）。"""
    if comfort is None:
        return DEFAULT_COMFORT
    try:
        return max(COMFORT_MIN, min(COMFORT_MAX, float(comfort)))
    except (TypeError, ValueError):
        return DEFAULT_COMFORT


def stack_warmth(path: list[dict]) -> float:
    """整套上装的保暖值之和。"""
    return sum(warmth(it) for it in path if it.get("slot") in TOP_SLOTS)


def stack_warm_ok(path: list[dict], temp: float,
                  comfort: Optional[float] = None) -> bool:
    """① 够不够暖 —— 就是「26℃穿衣法则」本体。"""
    return temp + stack_warmth(path) >= comfort_of(comfort)


def _outermost_top(path: list[dict]) -> Optional[dict]:
    tops = [it for it in path if it.get("slot") in TOP_SLOTS]
    if not tops:
        return None
    return max(tops, key=_layer_index)


def item_temp_ok(it: dict, temp: float, path: Optional[list[dict]] = None,
                 comfort: Optional[float] = None) -> bool:
    """单件在这一天（这一套）里合适吗。

    · 下半身 L4/L5：**双边**校验自己的区间（防"夏天棉裤 / 冬天凉鞋"），
      但**不参与保暖加总** —— 用户口径："下半身只考虑搭配"。
    · L4I 秋裤：只校验上限（它永远被外裤盖住，冷不该由它负责）。
    · 上装：
        - 会不会太热 —— **只有露在最外面的那件**要满足 `temp ≤ 它的上限`
        - 够不够暖 —— 在 L1 上做一次整套判定（DFS 时 L1 一定在 path 里）
    """
    slot = it.get("slot")
    if slot == "L4I":
        return temp <= _temp_max(it)
    if slot in ("L4", "L5"):
        return _temp_min(it) <= temp <= _temp_max(it)
    if slot not in TOP_SLOTS:
        return True

    outer = _outermost_top(path or [it])
    if outer is not None and outer.get("id") == it.get("id") and temp > _temp_max(it):
        return False                     # 露在外面的那件自己说"这个温度穿我太热"

    if slot == "L1":
        return stack_warm_ok(path or [it], temp, comfort)
    return True


def temp_ok(it: dict, temp: float) -> bool:
    """候选生成阶段的粗筛。**宁松勿紧** —— 精确判定在叶子上（`item_temp_ok`）。

    上装这里**只能**判上限：它到底会不会被盖住、整套够不够暖，要等成型才知道。
    筛太紧会静默砍掉合法搭配 —— 历史上"8℃ 的 T恤+卫衣+夹克"就是这么被误杀的
    （卫衣上限 20、下限 5，两道都卡）。所以上装给一条宽上限放行。
    下半身倒是可以在这里判死：它们的区间与整套无关。
    """
    slot = it.get("slot")
    if slot in ("L4", "L5"):
        return _temp_min(it) <= temp <= _temp_max(it)
    if slot == "L4I":
        return temp <= _temp_max(it)
    return temp <= _temp_max(it) + 12


def _formality(it: dict) -> int:
    v = it.get("formality")
    return DEFAULT_FORMALITY if v is None else int(v)


def _thickness(it: dict) -> int:
    v = it.get("thickness")
    return DEFAULT_THICKNESS if v is None else int(v)


def _layer_index(it: dict) -> int:
    try:
        return LAYER_ORDER.index(it["slot"])
    except ValueError:
        return 99  # ACC 之类，排在最后
