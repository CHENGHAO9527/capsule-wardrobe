"""生成一份种子衣橱，用于跑通并验证算法。

用法：
    python scripts/seed.py --reset     清空后写入 26 件
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

# name, slot, category, color_main, hex, neutral, formality, tmin, tmax,
# thickness, collar, length, sleeve, fit, material, price, wear
SEED = [
    # ── L1 内搭 ──
    ("米白棉T恤",     "L1", "T恤",     "米白", "#EDE6D6", 1, 2, 18, 32, 2, "圆领", "常规", "短袖", "合身", "棉",   89,  46),
    ("白衬衫",        "L1", "衬衫",     "白",   "#F5F5F0", 1, 3,  8, 30, 2, "翻领", "常规", "长袖", "合身", "棉",  199,  55),
    ("藏蓝针织衫",    "L1", "薄针织",   "藏蓝", "#1F2A44", 1, 3,  5, 20, 3, "圆领", "常规", "长袖", "合身", "羊毛", 299,  28),
    ("浅灰条纹T恤",   "L1", "T恤",     "浅灰", "#BFBFBA", 1, 2, 18, 32, 2, "圆领", "常规", "短袖", "宽松", "棉",   79,  19),
    ("黑色高领打底",  "L1", "薄针织",   "黑",   "#1C1C1A", 1, 3,  0, 15, 2, "高领", "常规", "长袖", "修身", "混纺", 159,  22),

    # ── L2 中间层 ──
    ("深灰针织开衫",  "L2", "针织开衫", "深灰", "#4A4A48", 1, 3,  5, 20, 3, "开领", "常规", "长袖", "合身", "羊毛", 399,  12),
    ("米色圆领毛衣",  "L2", "毛衣",     "米白", "#EDE6D6", 1, 3, -2, 15, 4, "圆领", "常规", "长袖", "宽松", "羊毛", 459,  17),
    ("藏青卫衣",      "L2", "卫衣",     "藏蓝", "#1F2A44", 1, 2,  5, 20, 4, "连帽", "常规", "长袖", "宽松", "棉",  249,  31),
    ("驼色厚衬衫",    "L2", "厚衬衫",   "驼色", "#B08968", 1, 3,  0, 18, 3, "翻领", "常规", "长袖", "合身", "混纺", 329,   9),

    # ── L3 外层 ──
    ("浅灰西装外套",  "L3", "西装外套", "浅灰", "#BFBFBA", 1, 4,  8, 24, 3, "开领", "常规", "长袖", "合身", "混纺", 899,  21),
    ("卡其风衣",      "L3", "风衣",     "卡其", "#C3A87B", 1, 3,  5, 20, 3, "翻领", "长",   "长袖", "宽松", "混纺", 1299,  6),
    ("深蓝牛仔夹克",  "L3", "夹克",     "牛仔蓝", "#4A6B8A", 0, 2, 8, 22, 3, "翻领", "短",  "长袖", "合身", "牛仔", 599,  14),
    ("黑色羊毛大衣",  "L3", "大衣",     "黑",   "#1C1C1A", 1, 4, -5, 12, 4, "翻领", "长",  "长袖", "宽松", "羊毛", 2199,  33),
    ("藏蓝轻薄羽绒",  "L3", "羽绒服",   "藏蓝", "#1F2A44", 1, 2, -10,  6, 5, "立领", "常规", "长袖", "宽松", "羽绒", 1099,  25),

    # ── L4 下装 ──
    ("藏蓝直筒裤",    "L4", "直筒裤",   "藏蓝", "#1F2A44", 1, 3, -5, 30, 3, "", "长",   "", "合身", "混纺", 499,  68),
    ("卡其直筒裤",    "L4", "直筒裤",   "卡其", "#C3A87B", 1, 3, -5, 30, 3, "", "长",   "", "合身", "棉",   399,  24),
    ("深蓝牛仔裤",    "L4", "牛仔裤",   "牛仔蓝", "#4A6B8A", 0, 2, -5, 32, 3, "", "长",  "", "合身", "牛仔", 599,  57),
    ("黑色西裤",      "L4", "西裤",     "黑",   "#1C1C1A", 1, 4, -5, 28, 3, "", "长",   "", "修身", "混纺", 699,  11),
    ("灰色休闲裤",    "L4", "直筒裤",   "中灰", "#8A8A85", 1, 2,  0, 30, 2, "", "长",   "", "宽松", "棉",   259,   8),
    ("酒红长裙",      "L4", "半身裙",   "酒红", "#7A2B33", 0, 3, 10, 28, 2, "", "长",   "", "合身", "化纤", 459,   4),

    # ── L5 鞋 ──
    ("棕色乐福鞋",    "L5", "乐福鞋",   "棕色", "#6B4A2F", 1, 3, -5, 30, 3, "", "", "", "合身", "皮革", 899,  52),
    ("白色运动鞋",    "L5", "运动鞋",   "白",   "#F5F5F0", 1, 2, -5, 35, 2, "", "", "", "合身", "皮革", 699,  78),
    ("黑色皮鞋",      "L5", "皮鞋",     "黑",   "#1C1C1A", 1, 4, -5, 30, 3, "", "", "", "合身", "皮革", 1099, 13),
    ("马丁靴",        "L5", "靴子",     "黑",   "#1C1C1A", 1, 2, -10, 12, 4, "", "", "", "宽松", "皮革", 1299, 19),

    # ── 刻意放的孤岛件，用来验证 ΔN = 0 的判定 ──
    ("荧光绿卫衣",    "L2", "卫衣",     "橄榄绿", "#39FF14", 0, 2,  5, 20, 4, "连帽", "常规", "长袖", "宽松", "棉",  199,   1),
    ("亮片礼服长裙",  "L4", "半身裙",   "酒红", "#7A2B33", 0, 5, 10, 28, 2, "", "及踝", "", "修身", "化纤", 1899,  1),
]

FIELDS = ["name", "slot", "category", "color_main", "color_hex", "is_neutral",
          "formality", "temp_min", "temp_max", "thickness", "collar",
          "length_band", "sleeve", "fit", "material", "purchase_price", "wear_count"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="先清空 items 表")
    args = ap.parse_args()

    db.init_db()
    conn = db.get_conn()
    try:
        if args.reset:
            conn.execute("DELETE FROM outfit_items")
            conn.execute("DELETE FROM prebuy_evals")
            conn.execute("DELETE FROM items")
            conn.execute("DELETE FROM wear_log")
            conn.commit()

        existing = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        if existing and not args.reset:
            print(f"items 表已有 {existing} 行，未清空的话会重复写入。加 --reset 重新来。")
            return

        for row in SEED:
            data = dict(zip(FIELDS, row))
            data["stage"] = "owned"
            cols = ", ".join(data)
            ph = ", ".join("?" * len(data))
            conn.execute(f"INSERT INTO items ({cols}) VALUES ({ph})", list(data.values()))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        print(f"写入完成，items 共 {n} 行")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
