# -*- coding: utf-8 -*-
"""生成 App 图标（PWA / 安卓主屏 / iOS 主屏幕）。

    python scripts/make_icons.py

设计跟全站一致：纸感底 `#f4f0ea` + **唯一墨黑** `#191714` 的细线，零阴影。
画的是**衣架**（三角 + 横杆 + 钩），不用文字 —— 本机只有宋体，
细笔画缩到主屏那 48px 会糊掉，而衣服品类本来就是靠"看"的。

产出（都落在 `web/` 下，经 `/static` 暴露）：

    web/icon-192.png            安卓主屏 / 浏览器标签页
    web/icon-512.png            PWA 安装、启动画面
    web/icon-maskable-512.png   安卓自适应图标（内容收进安全圆）
    web/icon-180.png            iOS「添加到主屏幕」

⚠️ **先按 4 倍画再缩** —— 直接画 192 的细线锯齿很明显。
⚠️ maskable 那版有**两套"安全圆"，别记混**（实测数据，别凭感觉调）：

   · **PWA**（manifest 里 `purpose: maskable`）→ 安全圆 = 图标直径的 **80%**
     衣架最外点是底部横杆两端，半径 190.9/256；安全半径 204.8 → **允许 scale ≤ 1.07**
   · **安卓原生** adaptive icon（108dp 画布 / 66dp 安全圆）→ 只允许 **scale ≤ 0.82**

   所以这里取 **1.00**：内容占画布 57% 宽，在 PWA 里既不越界也不显小。
   ⚠️ 真去手搭原生安卓工程的话，**这一版会被切角**，要按 0.82 重出一份。
   （以前写的是 0.85 —— 实测两套都不合：PWA 里只占 48% 显小，原生里照样越界。）
⚠️ 改了倍率之后**一定要把成品套进圆形裁切看一眼** —— 安全区是算出来的，但"看着别扭"
   只能靠眼睛。第一版漏了底下的横杆，画出来是个"Λ"，代码读着完全没问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

BASE = 512          # 设计坐标系
SS = 4              # 超采样倍数
BG = "#f4f0ea"      # 纸感底色
INK = "#191714"     # 唯一行动色

# ── 512 坐标系里的几何 ──
#   衣架 = 一个闭合的三角（两肩 + 底横杆）+ 一截竖杆 + 顶上那个钩。
APEX = (256, 218)       # 三角的顶点（两肩交汇处）
LEFT = (110, 366)       # 左肩
RIGHT = (402, 366)      # 右肩

HOOK_C = (256, 172)     # 钩子的圆心
HOOK_R = 27
# 从正下方（90°）顺时针绕到右下 30°（写成 390° 让 PIL 知道要跨过 0°）——
# 留出的那 60° 缺口就是钩尖，开口朝右下才像"挂在杆上"，不是一根带圆头的钉子。
HOOK_A0, HOOK_A1 = 90, 390
STEM_TOP = (256, 199)   # 竖杆顶端，正好落在钩子圆的下端点上

STROKE = 20             # 线宽（512 坐标系）

TARGETS = [
    ("icon-512.png", 512, 1.00),
    ("icon-192.png", 192, 1.00),
    ("icon-180.png", 180, 1.00),      # iOS 不看 manifest 的图标
    # PWA 的 maskable 安全圆是 80% 直径，1.00 就够（详见文件头的实测数据）
    ("icon-maskable-512.png", 512, 1.00),
]


def draw_hanger(d: ImageDraw.ImageDraw, s: float) -> None:
    """s = 512 坐标 → 画布坐标的总倍率（含超采样）。"""
    mid = BASE * SS / 2

    def P(p):
        return (mid + (p[0] - BASE / 2) * s, mid + (p[1] - BASE / 2) * s)

    w = max(2, round(STROKE * s))

    # 闭合三角：最后一个点回到 LEFT。用 joint="curve" 让顶点是圆的，
    # 再把三个顶点各补一个小圆点 —— PIL 的 joint 只处理中间的顶点，
    # 收尾那个点不补就会缺一角。
    tri = [P(LEFT), P(APEX), P(RIGHT), P(LEFT)]
    d.line(tri, fill=INK, width=w, joint="curve")
    for v in (LEFT, APEX, RIGHT):
        cx, cy = P(v)
        r = w / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=INK)

    # 竖杆
    d.line([P(STEM_TOP), P(APEX)], fill=INK, width=w)

    # 钩子
    cx, cy = P(HOOK_C)
    r = HOOK_R * s
    d.arc([cx - r, cy - r, cx + r, cy + r], HOOK_A0, HOOK_A1, fill=INK, width=w)


def make(size: int, scale: float) -> Image.Image:
    canvas = BASE * SS
    im = Image.new("RGB", (canvas, canvas), BG)
    draw_hanger(ImageDraw.Draw(im), SS * scale)
    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    if not WEB.is_dir():
        print(f"[错误] 找不到 web 目录：{WEB}")
        return 1

    for name, size, scale in TARGETS:
        out = WEB / name
        make(size, scale).save(out, "PNG", optimize=True)
        print(f"  {name:26s} {size}x{size}  scale={scale:.2f}  "
              f"{out.stat().st_size / 1024:.1f} KB")

    print()
    print("  图标已生成。改了几何后重跑本脚本即可，其余文件不用动。")
    print("  ⚠️ 跑完打开 web/icon-512.png 看一眼 —— 几何画错时代码是读不出来的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
