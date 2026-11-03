# -*- coding: utf-8 -*-
"""主色提取（本地，零成本，确定性）。

为什么不用模型报颜色：模型的色值不可靠，还受打光和背景影响，同一件衣服两次问可能给两个
答案。而颜色直接决定 R4（撞色）和中性色判定，一个错误值会污染之后所有的搭配判断。
所以：**颜色永远本地算，模型只负责语义字段。**

步骤：
  1. 缩到 160px（快，且足以代表整体色）
  2. 四角采样估背景色（假设是纯色背景，这也是拍摄规范要求的）
  3. 丢掉与背景接近的像素 —— 等价于一次廉价抠图，不需要 rembg/PyTorch
  4. 12 位色直方图取最大簇，再把簇周围的邻域像素一起平均（抗噪，不取到单一极值像素）
  5. 转到 CIELAB 找最近的调色板颜色 —— 比 RGB 欧氏距离准得多，
     否则「深灰」和「藏蓝」这种暗色很容易互相抢

输出两个色值：
  · name/hex   → 调色板颜色，入库用。全局统一，两件同色衣服必然完全相同，
                 替代率判重和撞色规则才有稳定的比较基准
  · observed   → 实测色值，只给界面显示色块用，不入库
"""

from __future__ import annotations

from .catalog import COLORS


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(round(x)))) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _srgb_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb) -> tuple[float, float, float]:
    """sRGB → CIELAB（D65）。"""
    r, g, b = (_srgb_to_linear(x) for x in rgb)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    x /= 0.95047
    z /= 1.08883

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)

    fx, fy, fz = f(x), f(y), f(z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def _lab_distance(a, b) -> float:
    # 加权欧氏距离（近似 ΔE）。L 权重压低一点：明度受打光影响最大，
    # 让色相/彩度主导匹配，否则白天和晚上拍同一件衣服会匹配到不同颜色名。
    dl = (a[0] - b[0]) * 0.6
    da = (a[1] - b[1])
    db = (a[2] - b[2])
    return (dl * dl + da * da + db * db) ** 0.5


_PALETTE_LAB = [(n, h, neu, rgb_to_lab(hex_to_rgb(h))) for n, h, neu in COLORS]


def nearest_palette(rgb) -> tuple[str, str, int, float]:
    """返回 (颜色名, 调色板hex, 是否中性, 距离)。"""
    lab = rgb_to_lab(rgb)
    best = min(_PALETTE_LAB, key=lambda p: _lab_distance(lab, p[3]))
    return best[0], best[1], best[2], _lab_distance(lab, best[3])


def _dist2(a, b) -> int:
    return sum((int(x) - int(y)) ** 2 for x, y in zip(a, b))


def _background_color(img) -> tuple[int, int, int]:
    """四角各取一块小样，取中位数当背景色。"""
    w, h = img.size
    pad = max(2, min(w, h) // 25)
    patches = [
        img.crop((0, 0, pad, pad)),
        img.crop((w - pad, 0, w, pad)),
        img.crop((0, h - pad, pad, h)),
        img.crop((w - pad, h - pad, w, h)),
    ]
    px: list[tuple[int, int, int]] = []
    for p in patches:
        px.extend(p.getdata())
    if not px:
        return (255, 255, 255)
    px.sort()
    mid = px[len(px) // 2]
    return mid  # type: ignore[return-value]


def _ring_uniformity(img) -> tuple[float, tuple[int, int, int]]:
    """最外一圈像素相对其中位数的平均偏差。

    用来判断"纯色背景"这个前提是否成立。干净的纯色背景偏差接近 0；
    **手机截图**（顶部状态栏 + 底部导航栏）偏差会非常大 ——
    这时四角采到的"背景色"其实是那两条 UI 栏，靠它推前景会把颜色算反
    （实测：黑色裤子被测成 #FEFEFE）。
    """
    w, h = img.size
    band = max(1, min(w, h) // 30)
    px: list[tuple[int, int, int]] = []
    for box in ((0, 0, w, band), (0, h - band, w, h),
                (0, 0, band, h), (w - band, 0, w, h)):
        px.extend(img.crop(box).getdata())
    if not px:
        return 999.0, (255, 255, 255)
    med = sorted(px)[len(px) // 2]
    dev = sum(_dist2(p, med) ** 0.5 for p in px) / len(px)
    return dev, med


def _dominant_of(pixels: list) -> tuple[tuple[float, float, float], int]:
    """一批像素里最大的色簇。12 位直方图取最大格，再把该格 3×3×3 邻域并进来平均 ——
    邻域平均是为了抗噪，不会取到某一个极值像素。返回 (均值, 该簇像素数)。"""
    if not pixels:
        return (255.0, 255.0, 255.0), 0

    hist: dict[tuple[int, int, int], int] = {}
    sums: dict[tuple[int, int, int], list[int]] = {}
    for p in pixels:
        k = (p[0] >> 4, p[1] >> 4, p[2] >> 4)
        hist[k] = hist.get(k, 0) + 1
        s = sums.get(k)
        if s is None:
            sums[k] = [p[0], p[1], p[2], 1]
        else:
            s[0] += p[0]
            s[1] += p[1]
            s[2] += p[2]
            s[3] += 1

    top = max(hist.items(), key=lambda kv: kv[1])[0]
    acc = [0, 0, 0]
    cnt = 0
    for k, s in sums.items():
        if all(abs(k[i] - top[i]) <= 1 for i in range(3)):
            acc[0] += s[0]
            acc[1] += s[1]
            acc[2] += s[2]
            cnt += s[3]
    if cnt == 0:
        return (255.0, 255.0, 255.0), 0
    return (acc[0] / cnt, acc[1] / cnt, acc[2] / cnt), cnt


def _center_box(img, frac: float = 0.6):
    w, h = img.size
    bw, bh = max(1, int(w * frac)), max(1, int(h * frac))
    return img.crop(((w - bw) // 2, (h - bh) // 2, (w + bw) // 2, (h + bh) // 2))


def dominant_color(img, bg_tolerance: int = 62) -> dict:
    """从一张 PIL 图里提取主色。

    分两种情况，先判断**"纯色背景"这个前提成不成立**：

    ① 外圈基本是纯色（偏差小）→ 正常走"背景色 → 取非背景的最大色簇"。
       这条对"衣服几乎占满画面"也免疫：四角采到的是衣服，那就没有非背景像素，
       自动落到中心区域。

    ② 外圈不是纯色（偏差大）→ 这张图没有干净背景，多半是**手机截图**
       （顶部状态栏 + 底部导航栏各一条）。这时四角取到的"背景"是那两条 UI 栏，
       继续走下去会把颜色整个算反 —— 实测黑色裤子会测成 #FEFEFE。
       直接改用中心 60% 区域取色，并打上 framed=True 让上层提示用户传原图。
    """
    small = img.convert("RGB").copy()
    small.thumbnail((160, 160), 1)  # 1 = BILINEAR，比默认重采样快
    px = list(small.getdata())
    if not px:
        return _fallback()
    total = len(px)

    ring_dev, ring_med = _ring_uniformity(small)
    framed = ring_dev > 25.0

    spatial_fallback = False
    if framed:
        box_px = list(_center_box(small, 0.6).getdata()) or px
        observed, cnt = _dominant_of(box_px)
        source = box_px
        spatial_fallback = True
    else:
        bg = ring_med
        tol2 = bg_tolerance * bg_tolerance
        non_bg = [p for p in px if _dist2(p, bg) > tol2]
        if len(non_bg) >= max(24, total * 0.02):
            observed, cnt = _dominant_of(non_bg)
            source = non_bg
        else:
            # 全图都和背景色接近 —— 衣服颜色和背景几乎一样（白衬衫拍在白底上）。
            # 靠颜色分不开，改用空间先验：拍摄规范要求衣服居中垂直俯拍。
            box_px = list(_center_box(small, 0.46).getdata()) or px
            observed, cnt = _dominant_of(box_px)
            source = box_px
            spatial_fallback = True

    # share 是"最大色簇在这批候选像素里占多少"，不是占整张画面的多少 ——
    # 否则走中心区域时最多只能到 0.21（46%×46%），永远过不了可信阈值。
    share = cnt / max(1, len(source))
    name, pal_hex, neutral, dist = nearest_palette(observed)

    # 彩度极低的一律按中性处理 —— 调色板里有个别彩色的深色（如墨绿），
    # 在低光照片里容易被误判，用实测彩度兜一道
    lab = rgb_to_lab(observed)
    chroma = (lab[1] ** 2 + lab[2] ** 2) ** 0.5
    if chroma < 11.0:
        neutral = 1

    return {
        "name": name,
        "hex": pal_hex,
        "neutral": int(neutral),
        "observed_hex": rgb_to_hex(observed),
        "share": round(share, 3),
        "palette_distance": round(dist, 1),
        "spatial_fallback": spatial_fallback,
        "framed": framed,
        "ring_dev": round(ring_dev, 1),
        # 带边框的图（截图）颜色不可信，直接标不可信，让上层提示用户
        "confident": bool(not framed and share >= 0.45 and dist <= 26.0),
    }


def _fallback() -> dict:
    return {"name": "", "hex": "", "neutral": 0, "observed_hex": "",
            "share": 0.0, "palette_distance": 999.0, "spatial_fallback": False,
            "framed": False, "ring_dev": 0.0, "confident": False}
