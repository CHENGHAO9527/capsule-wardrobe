# -*- coding: utf-8 -*-
"""品类字典 + 自动填值表。

这里是「自动识别」的零配置兜底：只要用户选出槽位和品类，其余字段全部有合理默认值，
一条记录立刻就能参与搭配计算。模型只是把这最后一步也省掉。

温度区间的给法有一条不直觉但要紧的规则 —— 见 L1_BASE_SPAN 的注释。
"""

from __future__ import annotations

SLOT_NAMES = {"L1": "内搭", "L2": "中间层", "L3": "外层",
              "L4I": "内穿下装", "L4": "下装", "L5": "鞋", "ACC": "配饰"}

CATEGORIES = {
    "L1": ["T恤", "衬衫", "薄针织", "吊带", "Polo衫"],
    "L2": ["毛衣", "卫衣", "针织开衫", "马甲", "厚衬衫"],
    "L3": ["西装外套", "夹克", "风衣", "大衣", "羽绒服", "皮衣"],
    # L4I 是**内穿的裤子**（2026-09-21 按用户要求新增的槽位）：
    # 它必须被 L4 盖住，外面看不出来，所以颜色/正式度都不参与判定。
    "L4I": ["秋裤", "保暖裤", "打底裤", "绒裤"],
    "L4": ["直筒裤", "西裤", "牛仔裤", "阔腿裤", "半身裙", "短裙", "短裤", "长裤"],
    "L5": ["运动鞋", "乐福鞋", "皮鞋", "靴子", "帆布鞋", "凉鞋"],
    "ACC": ["包", "腰带", "围巾", "帽子", "墨镜"],
}

# (名称, 十六进制, 是否中性色)
COLORS = [
    ("白", "#F5F5F0", 1), ("米白", "#EDE6D6", 1), ("浅灰", "#BFBFBA", 1),
    ("中灰", "#8A8A85", 1), ("深灰", "#4A4A48", 1), ("黑", "#1C1C1A", 1),
    ("藏蓝", "#1F2A44", 1), ("卡其", "#C3A87B", 1), ("驼色", "#B08968", 1),
    ("棕色", "#6B4A2F", 1), ("酒红", "#7A2B33", 0), ("墨绿", "#2F4A3A", 0),
    ("橄榄绿", "#6B7A4A", 0), ("牛仔蓝", "#4A6B8A", 0), ("天蓝", "#7FB3D5", 0),
    ("浅粉", "#E8C4C8", 0), ("鹅黄", "#E8D48A", 0), ("橙", "#D97A3A", 0),
]

COLLARS = ["圆领", "V领", "高领", "翻领", "开领", "连帽", "无领", "立领"]
LENGTHS = ["短", "常规", "长", "及踝"]
SLEEVES = ["无袖", "短袖", "长袖", "可卷"]
FITS = ["修身", "合身", "宽松", "廓形"]
MATERIALS = ["棉", "羊毛", "化纤", "混纺", "皮革", "牛仔", "真丝", "亚麻", "羽绒"]
PATTERNS = ["纯色", "条纹", "格纹", "印花", "标识"]

STATUSES = ["in_wardrobe", "laundry", "dry_clean", "stored", "repair", "lent_out", "discarded"]
STATUS_NAMES = {
    "in_wardrobe": "在库", "laundry": "待洗", "dry_clean": "干洗中",
    "stored": "换季收箱", "repair": "待修", "lent_out": "借出", "discarded": "已弃",
}

# 内搭不设温度门槛，全年有效。
#
# 为什么：内搭的体感温度由外层决定 —— 0℃ 穿一件薄 T 恤 + 毛衣 + 大衣是正当穿法，
# 短袖内搭本来就是叠穿的常见组成。如果给 T 恤填 18~32，那么到了 8℃ 这一层就没有
# 合法单品，整套搭配直接枚举不出来（表现为「冬天 0 套」，看着像 bug）。
# 季节差异交给 `thickness` 表达，那才是它该管的事。
L1_BASE_SPAN = (-10, 38)

# 品类 → 其余字段的默认值
# (warmth 保暖值℃, formality 正式度, thickness 厚度, temp_min, temp_max,
#  collar 领型, sleeve 袖型, length 衣长, fit 版型, material 材质, pattern 图案)
#
# ⚠️ `warmth` 是 **「26℃穿衣法则」** 的保暖值（2026-09-23 加的），单位就是摄氏度：
#
#     当日气温 + 衣物保暖值之和 = 26℃（体感舒适线，可调）
#
# 法则原表（人民日报 / 科普中国 / 央广网多源一致）：
#   较厚羽绒服 9｜薄款羽绒服 6｜稍厚絮棉衣 5｜厚羊毛衫 4｜棉背心 4｜
#   抓绒衣服 / 薄外套 3｜厚棉毛衫 2｜薄棉毛衫（薄秋衣）1
# 表里没点名的品类，按"相当于哪一件"折算；法则本身**不随厚度变**，
# 所以同是卫衣、厚薄同值 —— 想区分就逐件改（`/wardrobe` 已入库列表的「改字段」）。
#
# ⚠️ **只有上装（L1/L2/L3）的保暖值参与计算**，下半身（L4I/L4/L5）恒为 0 ——
# 用户口径："下半身只考虑搭配"。裤子鞋子仍按各自的温度区间参与判定，
# 只是不参与"够不够暖"的加总。
#
# 引擎侧口径见 `rules.stack_warm_ok`；`rules.warmth()` 在字段为空时按厚度兜底。
CATEGORY_DEFAULTS: dict[str, dict] = {
    # ── L1 内搭 ──
    "T恤":      dict(warmth=1, formality=2, thickness=2, temp_min=L1_BASE_SPAN[0], temp_max=L1_BASE_SPAN[1],
                     collar="圆领", sleeve="短袖", length_band="常规", fit="合身", material="棉", pattern="纯色"),
    "衬衫":     dict(warmth=2, formality=3, thickness=2, temp_min=L1_BASE_SPAN[0], temp_max=L1_BASE_SPAN[1],
                     collar="翻领", sleeve="长袖", length_band="常规", fit="合身", material="棉", pattern="纯色"),
    "薄针织":   dict(warmth=2, formality=3, thickness=2, temp_min=L1_BASE_SPAN[0], temp_max=L1_BASE_SPAN[1],
                     collar="圆领", sleeve="长袖", length_band="常规", fit="合身", material="混纺", pattern="纯色"),
    "吊带":     dict(warmth=0, formality=2, thickness=1, temp_min=L1_BASE_SPAN[0], temp_max=L1_BASE_SPAN[1],
                     collar="无领", sleeve="无袖", length_band="短", fit="合身", material="化纤", pattern="纯色"),
    "Polo衫":   dict(warmth=1, formality=3, thickness=2, temp_min=L1_BASE_SPAN[0], temp_max=L1_BASE_SPAN[1],
                     collar="翻领", sleeve="短袖", length_band="常规", fit="合身", material="棉", pattern="纯色"),

    # ── L2 中间层 ──
    "毛衣":     dict(warmth=4, formality=3, thickness=3, temp_min=-5, temp_max=18,
                     collar="圆领", sleeve="长袖", length_band="常规", fit="合身", material="羊毛", pattern="纯色"),
    "卫衣":     dict(warmth=4, formality=1, thickness=4, temp_min=-5, temp_max=20,
                     collar="连帽", sleeve="长袖", length_band="常规", fit="宽松", material="棉", pattern="纯色"),
    "针织开衫": dict(warmth=3, formality=3, thickness=3, temp_min=0, temp_max=22,
                     collar="开领", sleeve="长袖", length_band="常规", fit="合身", material="混纺", pattern="纯色"),
    "马甲":     dict(warmth=4, formality=3, thickness=3, temp_min=-5, temp_max=18,
                     collar="V领", sleeve="无袖", length_band="常规", fit="合身", material="羊毛", pattern="纯色"),
    "厚衬衫":   dict(warmth=3, formality=3, thickness=3, temp_min=-5, temp_max=18,
                     collar="翻领", sleeve="长袖", length_band="常规", fit="合身", material="棉", pattern="纯色"),

    # ── L3 外层 ──
    "西装外套": dict(warmth=3, formality=5, thickness=3, temp_min=8, temp_max=25,
                     collar="翻领", sleeve="长袖", length_band="常规", fit="合身", material="混纺", pattern="纯色"),
    "夹克":     dict(warmth=3, formality=2, thickness=3, temp_min=0, temp_max=22,
                     collar="立领", sleeve="长袖", length_band="常规", fit="合身", material="化纤", pattern="纯色"),
    "风衣":     dict(warmth=4, formality=4, thickness=3, temp_min=2, temp_max=20,
                     collar="翻领", sleeve="长袖", length_band="长", fit="合身", material="混纺", pattern="纯色"),
    "大衣":     dict(warmth=7, formality=4, thickness=5, temp_min=-15, temp_max=15,
                     collar="翻领", sleeve="长袖", length_band="长", fit="合身", material="羊毛", pattern="纯色"),
    "羽绒服":   dict(warmth=9, formality=2, thickness=5, temp_min=-20, temp_max=10,
                     collar="立领", sleeve="长袖", length_band="常规", fit="宽松", material="羽绒", pattern="纯色"),
    "皮衣":     dict(warmth=4, formality=3, thickness=4, temp_min=-5, temp_max=18,
                     collar="翻领", sleeve="长袖", length_band="常规", fit="合身", material="皮革", pattern="纯色"),

    # ── L4I 内穿下装（秋裤）──
    # 温度区间照"单穿舒不舒服"给，但**实际不靠它控制季节**：
    # `graph.layer_plan` 只在 14℃ 以下才让 L4I 进候选，
    # 而 `rules.item_temp_ok` 对 L4I **只校验上限**（它永远被外裤盖住，
    # 冷不该由它负责 —— 跟内搭 L1 同理）。
    # 先前那件秋裤按 L4 录、沿用"直筒裤 -5~34℃"，于是 30℃ 被排出来；
    # 有了独立槽位，那种事从根上不会发生，**不用去改用户已有的数据**。
    "秋裤":     dict(warmth=0, formality=1, thickness=4, temp_min=-10, temp_max=14,
                     collar="", sleeve="", length_band="长", fit="修身", material="混纺", pattern="纯色"),
    "保暖裤":   dict(warmth=0, formality=1, thickness=4, temp_min=-15, temp_max=12,
                     collar="", sleeve="", length_band="长", fit="修身", material="化纤", pattern="纯色"),
    "打底裤":   dict(warmth=0, formality=2, thickness=3, temp_min=-5, temp_max=18,
                     collar="", sleeve="", length_band="长", fit="修身", material="混纺", pattern="纯色"),
    "绒裤":     dict(warmth=0, formality=1, thickness=5, temp_min=-20, temp_max=10,
                     collar="", sleeve="", length_band="长", fit="修身", material="化纤", pattern="纯色"),

    # ── L4 下装 ──
    "直筒裤":   dict(warmth=0, formality=3, thickness=3, temp_min=-5, temp_max=34,
                     collar="", sleeve="", length_band="长", fit="合身", material="混纺", pattern="纯色"),
    "西裤":     dict(warmth=0, formality=4, thickness=3, temp_min=0, temp_max=32,
                     collar="", sleeve="", length_band="长", fit="修身", material="混纺", pattern="纯色"),
    "牛仔裤":   dict(warmth=0, formality=2, thickness=3, temp_min=-8, temp_max=34,
                     collar="", sleeve="", length_band="长", fit="合身", material="牛仔", pattern="纯色"),
    "阔腿裤":   dict(warmth=0, formality=3, thickness=3, temp_min=0, temp_max=32,
                     collar="", sleeve="", length_band="长", fit="宽松", material="混纺", pattern="纯色"),
    "半身裙":   dict(warmth=0, formality=3, thickness=2, temp_min=8, temp_max=32,
                     collar="", sleeve="", length_band="长", fit="合身", material="混纺", pattern="纯色"),
    "短裙":     dict(warmth=0, formality=2, thickness=2, temp_min=15, temp_max=35,
                     collar="", sleeve="", length_band="短", fit="合身", material="混纺", pattern="纯色"),
    # 短裤：用户 2026-09-21 说"后面我会录短裤" —— 31℃ 以上排不出搭配的唯一解药。
    # 上限给到 38℃，别让它重蹈"下限卡死、整个夏天没得穿"的覆辙。
    "短裤":     dict(warmth=0, formality=1, thickness=1, temp_min=20, temp_max=38,
                     collar="", sleeve="", length_band="短", fit="宽松", material="棉", pattern="纯色"),
    "长裤":     dict(warmth=0, formality=3, thickness=3, temp_min=-5, temp_max=34,
                     collar="", sleeve="", length_band="长", fit="合身", material="棉", pattern="纯色"),

    # ── L5 鞋 ──
    "运动鞋":   dict(warmth=0, formality=2, thickness=2, temp_min=-8, temp_max=35,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "乐福鞋":   dict(warmth=0, formality=3, thickness=3, temp_min=2, temp_max=32,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "皮鞋":     dict(warmth=0, formality=4, thickness=3, temp_min=2, temp_max=32,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "靴子":     dict(warmth=0, formality=3, thickness=4, temp_min=-12, temp_max=20,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "帆布鞋":   dict(warmth=0, formality=1, thickness=2, temp_min=5, temp_max=34,
                     collar="", sleeve="", length_band="", fit="合身", material="棉", pattern="纯色"),
    "凉鞋":     dict(warmth=0, formality=1, thickness=1, temp_min=22, temp_max=40,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),

    # ── ACC 配饰 ──
    "包":       dict(warmth=0, formality=3, thickness=2, temp_min=-10, temp_max=40,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "腰带":     dict(warmth=0, formality=3, thickness=2, temp_min=-10, temp_max=40,
                     collar="", sleeve="", length_band="", fit="合身", material="皮革", pattern="纯色"),
    "围巾":     dict(warmth=0, formality=3, thickness=3, temp_min=-15, temp_max=12,
                     collar="", sleeve="", length_band="", fit="合身", material="羊毛", pattern="纯色"),
    "帽子":     dict(warmth=0, formality=2, thickness=2, temp_min=-10, temp_max=32,
                     collar="", sleeve="", length_band="", fit="合身", material="棉", pattern="纯色"),
    "墨镜":     dict(warmth=0, formality=2, thickness=1, temp_min=5, temp_max=40,
                     collar="", sleeve="", length_band="", fit="合身", material="化纤", pattern="纯色"),
}

# 槽位兜底：品类没填时用该槽位第一个品类的默认值
SLOT_FALLBACK = {slot: cats[0] for slot, cats in CATEGORIES.items()}


def defaults_for(slot: str, category: str | None = None) -> dict:
    """取 (槽位, 品类) 的默认字段。品类不认识时退到槽位里第一个品类。"""
    if category and category in CATEGORY_DEFAULTS:
        return dict(CATEGORY_DEFAULTS[category])
    return dict(CATEGORY_DEFAULTS[SLOT_FALLBACK.get(slot, "T恤")])


def category_of(slot: str, category: str | None) -> str:
    """把品类规整到该槽位的合法值上。"""
    cats = CATEGORIES.get(slot, [])
    if category in cats:
        return category
    return cats[0] if cats else ""


# 颜色词 → 调色板颜色名。顺序有讲究：长的、更具体的放前面
# （"米白"要在"白"之前，"深灰"要在"灰"之前，否则全被短词抢走）。
#
# 用途只有一个：**当图片测色明确不可信时**（比如上传的是手机截图，
# 四角取到的是状态栏和导航栏，颜色会整个测反），退而从模型给出的命名里取颜色。
# 模型是"看过图之后"给出的名字，在这个场景下比一个已知错误的像素测量可靠得多。
# 正常原图的测色不会走这条路径。
COLOR_WORDS = [
    ("米白", "米白"), ("砂白", "米白"), ("燕麦", "米白"), ("奶白", "米白"),
    ("白色", "白"), ("纯白", "白"), ("藏青", "藏蓝"), ("藏蓝", "藏蓝"),
    ("深蓝", "藏蓝"), ("浅蓝", "天蓝"), ("蓝色", "天蓝"), ("天蓝", "天蓝"),
    ("牛仔蓝", "牛仔蓝"), ("深灰", "深灰"), ("炭灰", "深灰"), ("浅灰", "浅灰"),
    ("灰色", "中灰"), ("卡其", "卡其"), ("驼色", "驼色"), ("驼", "驼色"),
    ("棕色", "棕色"), ("咖啡", "棕色"), ("酒红", "酒红"), ("墨绿", "墨绿"),
    ("橄榄绿", "橄榄绿"), ("浅粉", "浅粉"), ("粉色", "浅粉"), ("鹅黄", "鹅黄"),
    ("黄色", "鹅黄"), ("橙色", "橙"), ("黑色", "黑"), ("黑", "黑"),
    ("白", "白"), ("灰", "中灰"), ("绿", "墨绿"), ("红", "酒红"), ("蓝", "天蓝"),
]

_COLOR_HEX = {name: (hexv, neutral) for name, hexv, neutral in COLORS}


def color_from_name(name: str | None) -> str | None:
    """从一件衣服的名字里抽颜色词，返回调色板颜色名。抽不到返回 None。"""
    if not name:
        return None
    for kw, pal in COLOR_WORDS:
        if kw in name and pal in _COLOR_HEX:
            return pal
    return None


def nearest_category(hint: str | None) -> str | None:
    """把模型给的自由品类名（"长袖衬衫""针织衫"…）映射到我们的枚举上。

    只做包含匹配，够用了 —— 猜不出来时返回 None，让调用方退回槽位默认值。
    """
    if not hint:
        return None
    h = str(hint).strip()
    flat: list[str] = [c for cats in CATEGORIES.values() for c in cats]
    if h in flat:
        return h
    # 长的是"更具体"的名字，优先匹配，避免"短裙"被"裙"抢走
    for c in sorted(flat, key=len, reverse=True):
        if c in h:
            return c
    alias = {
        "针织衫": "薄针织", "T恤衫": "T恤", "短袖": "T恤", "短T": "T恤",
        "上衣": "T恤", "外套": "夹克", "风衣外套": "风衣", "棉服": "羽绒服",
        "羽绒": "羽绒服", "休闲裤": "直筒裤", "运动裤": "长裤", "西服": "西装外套",
        "裙子": "半身裙", "运动休闲鞋": "运动鞋", "高跟鞋": "皮鞋",
        "羊毛衫": "毛衣", "帽衫": "卫衣", "背包": "包",
        # 单字兜底放最后 —— "鞋" 会命中"高跟鞋"，所以必须让长键先匹配（见下）
        "裤子": "长裤", "鞋": "运动鞋", "衫": "衬衫", "裙": "半身裙", "裤": "长裤",
    }
    # 按 key 长度倒序：否则 "鞋" 会抢在 "高跟鞋" 前面命中，把高跟鞋判成运动鞋。
    for k in sorted(alias, key=len, reverse=True):
        if k in h:
            return alias[k]
    return None
