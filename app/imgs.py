# -*- coding: utf-8 -*-
"""图片标准化：从手机截图里裁出**商品照片本身**，再放进 4:5 白底画布。

为什么需要这一步 —— 三个版本的教训，每一版都是被真实数据打回来的：

  ① 第一版：把竖长图缩进正方形再补白边。竖构图左右各留 40% 白，
     衣服只占中间一小条，用户的原话是「图太小」。
  ② 第二版：按「最大色簇的外接框」裁（`_bbox_of`）。对淘宝详情页截图**完全失效** ——
     照片的背景色（白 / 浅灰）在顶部缩略图条、价格区白字里反复出现，
     所以那个色的外接框必然框住整张图，每张都 `keep ≈ 1.0` 被判「本来就是干净的整图」。
     实测 29 张里只裁动了有效的 3 张。更糟的是 id=86：算法挑中的最大黑簇，
     外接框正好是**底部「标题 + 价格 + 参数」那一整块文字区**，
     于是裁完只剩文字 —— 用户的原话是「有几件裤子的图片里面没裤子只有文字」。
  ③ 现在这版：**逐行剖面**。淘宝详情截图的结构是固定的，而 chrome（界面外壳）
     和照片的像素特征差异极大：

        ┌────────────────┐
        │ 顶部状态栏（近黑）    │  ← chrome
        ├────────────────┤
        │                │
        │    商品照片      │  ← 要留下的
        │                │
        ├────────────────┤
        │ 缩略图条 / 价格 / 标题 │  ← chrome
        │ 参数 / 客服栏 / 购买钮 │
        └────────────────┘

     chrome 行只可能是三种样子：**扁平近黑**（状态栏、黑边）、
     **暗底白字**（标题、参数、客服栏）、**高饱和**（橙色「加入购物车」）。
     照片行正相反。于是从上下两端各往中间走，遇到第一个「不像 chrome」的行就停。

**只动几何，绝不动内容。** 不抠图、不重绘、不调色 —— 衣服的颜色、图案、纹理
必须原样保留，否则标准图和实物对不上，整套搭配逻辑就失去意义（见 docs/00）。
"""

from __future__ import annotations

from PIL import Image, ImageOps

# 剖面分辨率。宽度要够大才能区分「这一整行都是黑的」（状态栏）和
# 「只有衣服是黑的」（深色卫衣）—— 太窄会把两者混为一谈。
_PROF_W = 96
_PROF_H = 120

# 顶部最多裁掉的比例。照片本身可能整体偏暗，必须封顶防止吃掉衣服。
_TOP_MAX = 0.34
# 底部最多裁掉的比例。淘宝那套「缩略图条+价格+标题+参数+购买栏」很长。
_BOT_MAX = 0.62
# 左右最多各裁掉的比例。**卡得很小是有意的**：这一刀只在外面套着一层窄白边时有用，
# 而真实的商品照左右本来就是干净的摄影棚白墙 —— 裁它换来的尺寸收益很小，
# 却可能把白衣服的袖子边缘削掉。自检里「干净商品照不许被切到衣服」那条就是为它设的。
_SIDE_MAX = 0.06

# 裁完至少留这么高，否则认定「没找到可用的照片」，原样返回。
_MIN_KEEP = 0.22

# chrome 行占整图的比例超过它 → 整张图基本是界面外壳，很可能没拍到衣服。
_NO_GARMENT_SHARE = 0.87

# 收敛时允许「走过头」的行数比例。
# 底部外壳里夹着一条灰色提示条（「商品已经下架啦」），它既不是纯黑也不是暗底白字，
# 严格逐行判定会在那里断掉 —— 实测让 id=86 的底部只裁到 90%，价格/标题全留着。
# 给几个"不担保"的名额，让它能跨过这种夹层继续往上走。
_SLACK_BOT = 0.045
_SLACK_TOP = 0.03


def _row_profile(im: Image.Image, h: int = _PROF_H, w: int = _PROF_W) -> list[tuple[float, float, float, float]]:
    """逐行统计 → [(亮度均值, 亮度标准差, 亮像素占比, 平均饱和度)]。

    这四个量足以把「界面外壳」和「照片」分开：
      · 状态栏 / 黑边：均值极低、标准差极小（整行纯黑）
      · 标题 / 参数：  均值低、亮像素很少（只有白字是亮的）
      · 橙色购买钮：  饱和度高
      · 照片：         以上都不满足
    """
    small = im.convert("RGB").resize((w, h), Image.BILINEAR)
    px = small.load()
    out = []
    for y in range(h):
        s = s2 = 0.0
        bright = 0
        sat = 0.0
        for x in range(w):
            r, g, b = px[x, y]
            lum = (299 * r + 587 * g + 114 * b) / 1000.0
            s += lum
            s2 += lum * lum
            if lum > 140:
                bright += 1
            mx = max(r, g, b)
            if mx:
                sat += (mx - min(r, g, b)) / mx
        m = s / w
        var = s2 / w - m * m
        out.append((m, var ** 0.5 if var > 0 else 0.0, bright / w, sat / w))
    return out


def _trim_spec_strip(im: Image.Image, bot: int, n: int) -> int:
    """底部外壳上方还挂着一条淘宝的「规格缩略图条」，把它也削掉。

    为什么单独处理：那条缩略图条里放的就是这件衣服**同一张图的小图**，
    背景色、厚度、明暗和上面的商品照几乎一样 —— 逐行统计区分不了，
    `_walk` 只能停在它下面。实测 id=86 的剖面：

        43.8~45.0%  L≈207~233  sd≈0.5~5    ← 照片自己的底部留白（扁平）
        45.4~50.0%  L≈186~197  sd≈33~48    ← 缩略图条（几个小方块，强起伏）
        50.4~51.2%  L≈197~199  sd≈0.8~22   ← 条下面的浅色边
        51.7%       L=53 → 3    sd=0        ← 价格区，外壳开始

    唯一稳定的判据是**第二条**：缩略图条上方有一条**扁平的浅色分隔带**。
    照片内部的起伏是连续的，不会有「强起伏带」紧贴着「扁平带」再紧贴着「外壳」。
    三个条件同时成立才动，宁可漏裁也不误裁（误裁会切掉衣服）。
    """
    h = im.height
    if bot <= 0 or h < 200:
        return bot
    # 只看底部 18%，按 0.4% 一行取剖面
    span = max(2, int(h * 0.18))
    y0 = max(0, bot - span)
    rows = max(2, int((bot - y0) / h * 250))
    band = im.crop((0, y0, im.width, bot)).resize((_PROF_W, rows), Image.BILINEAR)
    px = band.load()
    stats = []
    for y in range(rows):
        vals = [(299 * px[x, y][0] + 587 * px[x, y][1] + 114 * px[x, y][2]) / 1000
                for x in range(_PROF_W)]
        m = sum(vals) / _PROF_W
        var = sum((v - m) ** 2 for v in vals) / _PROF_W
        stats.append((m, var ** 0.5 if var > 0 else 0.0))

    def px_of(i: int) -> int:
        return y0 + int(round(i * (bot - y0) / rows))

    i = rows - 1
    while i >= 0 and stats[i][1] < 18:      # 跳过条下方的浅色边 / 扁平行
        i -= 1
    if i < 1:
        return bot
    end = i                                  # 强起伏带的下沿
    # 不限制明暗：缩略图条的底色有的页面是浅灰、有的是黑的，两者都遇到过
    while i >= 0 and stats[i][1] >= 18:
        i -= 1
    start = i + 1                            # 强起伏带的上沿
    if end - start + 1 < 2 or (end - start + 1) > rows * 0.75:
        return bot                           # 太短多半是噪点，太长是照片本身
    above = stats[i] if i >= 0 else (0.0, 99.0)
    if not (above[0] > 140 and above[1] < 7):   # 上方必须是一条扁平浅色分隔带
        return bot
    cut = px_of(start)
    if cut <= 0 or bot - cut > h * 0.14:
        return bot
    return cut


def _is_chrome(st: tuple[float, float, float, float], top: bool) -> bool:
    """这一行像不像界面外壳。

    三条件对应三种锚点（见模块头部那张图）：
      · 扁平近黑 —— 状态栏、黑边、纯黑页脚
      · 低饱和是前提 —— **顶部尤其需要**。状态栏只可能是黑/灰，绝不会有颜色；
        少了这一条，一张木地板背景的商品照会被从顶上削掉一大块
        （实测 id=106 那双鞋，木地板 sd=28 / sat=0.5 正好撞上「扁平近黑」）
      · 高饱和 —— 橙色「加入购物车 / 立即购买」，只可能在底部
      · 暗底白字 —— 标题、参数、客服栏，只可能在底部。
        **顶部不启用**：照片本身也可能整体偏暗（深色卫衣、黑色皮衣），
        从顶上按这条裁会把衣服削掉。
    """
    m, sd, bright, sat = st
    if m < 60 and sd < 45 and sat < 0.28:
        return True
    if not top:
        if sat > 0.45:                  # 橙色购买按钮
            return True
        if m < 110 and bright < 0.30:   # 暗底白字：标题、参数、客服栏
            return True
    return False


def _walk(chrome: list[bool], n: int, from_top: bool, limit: float, slack_ratio: float) -> int:
    """从一端往里走，返回「外壳」结束的位置。

    允许在连续 chrome 中间夹入少量非 chrome 行（`slack`）—— 见 _SLACK_BOT 的说明。
    只有真正走到过 chrome 行，结果才会离开端点，所以对「本来就没有外壳」的
    干净商品照一定是原样返回（实测 1279×1706 那四双鞋都没有被误裁）。
    """
    slack = max(1, int(n * slack_ratio))
    rng = range(n) if from_top else range(n - 1, -1, -1)
    box = int(n * limit)
    told = 0
    edge = 0 if from_top else n
    miss = 0
    for y in rng:
        if from_top and y > box:
            break
        if (not from_top) and y < box:
            break
        if chrome[y]:
            edge = y + 1 if from_top else y
            miss = 0
        else:
            miss += 1
            if miss > slack:
                break
        told += 1
    return edge


def _col_profile(im: Image.Image, w: int = _PROF_W) -> list[tuple[float, float]]:
    """逐列统计 → [(亮度均值, 亮度标准差)]。用来削掉左右两侧的纯色留白。"""
    h = max(8, int(w * im.height / max(1, im.width)))
    h = min(h, _PROF_H)
    small = im.convert("L").resize((w, h), Image.BILINEAR)
    px = small.load()
    out = []
    for x in range(w):
        s = s2 = 0.0
        for y in range(h):
            v = px[x, y]
            s += v
            s2 += v * v
        m = s / h
        var = s2 / h - m * m
        out.append((m, var ** 0.5 if var > 0 else 0.0))
    return out


def _is_blank_col(st: tuple[float, float]) -> bool:
    """纯色留白列：**纯度极高**的空白，而且几乎没有起伏。

    阈值卡得很紧（>246 / <16）是有意的：商品照左右两侧本来就常是一片干净的
    摄影棚白墙，卡松了会把衣服两边各削掉一块。宁可不裁。
    """
    m, sd = st
    return sd < 6 and (m > 246 or m < 16)


def _flat_dark_run(band: Image.Image, y_lo: int, y_hi: int, from_bottom: bool,
                   cols: int = 64, thr: float = 60.0, sd_thr: float = 26.0) -> int:
    """在 `[y_lo, y_hi)` 这段**真实像素行**里数连续几个「扁平近黑」行。

    1 行分辨率 —— 专门用来给 `_row_profile` 的粗估**收尾**：
    剖面只有 200 行，在 1350 高的图上每行覆盖约 7 个像素，
    所以粗估给的边界总差 1~2 行，窄窄一条黑线留在画布内部。
    """
    hh = y_hi - y_lo
    if hh <= 0:
        return 0
    strip = band.crop((0, y_lo, band.width, y_hi)).convert("L")
    strip = strip.resize((cols, hh), Image.BILINEAR)
    px = strip.load()
    ys = range(hh - 1, -1, -1) if from_bottom else range(hh)
    k = 0
    for y in ys:
        vals = [px[x, y] for x in range(cols)]
        m = sum(vals) / cols
        sd = (sum((v - m) ** 2 for v in vals) / cols) ** 0.5
        if not (m < thr and sd < sd_thr):
            break
        k += 1
    return k


def _shave_dark_edges(band: Image.Image, max_ratio: float = 0.05) -> tuple[int, int]:
    """削掉裁切结果上/下**仍然是纯黑扁平行**的那几行 → 返回 (削上, 削下) 像素数。

    为什么需要（实测踩出来的）：`strip_bands` 最后会给裁切框往外多留 0.8%
    （`pad_y`），那一点点外扩本意是别让剖面精度把照片的第一行切掉。
    可**深色模式截图的截图**里，照片上下紧接着的就是纯黑的 App 背景 ——
    这 0.8% 正好是黑边。而 `compose` 是「等比缩放 + 居中留白」，
    裁切框的上下边落在画布**内部**，于是衣服上下各横着一条黑线：
    实测 108 张展示图里 **24 张**中招（黑线位置 2%~98% 不等，随截图而异）。

    两遍：先按 200 行剖面粗估（快），再按真实像素行收尾（准）。
    判据只有"扁平近黑"这一个，且带两道保险，宁可漏削也不误削：
      · 整段里暗行占比 > 70% 就整个跳过 —— 那是**暗底商品照**（黑背景拍的鞋、
        深色大衣），削它就是把衣服切掉。
      · 单侧最多削 5%。
    """
    h = band.height
    if h < 40:
        return 0, 0
    rows = _row_profile(band, h=200)
    n = len(rows)

    def flat_dark(st: tuple[float, float, float, float]) -> bool:
        m, sd, _bright, _sat = st
        return m < 60 and sd < 26

    if sum(1 for st in rows if flat_dark(st)) > n * 0.7:
        return 0, 0

    lim = max(1, int(n * max_ratio))
    t = 0
    while t < lim and flat_dark(rows[t]):
        t += 1
    b = n
    while b > n - lim and flat_dark(rows[b - 1]):
        b -= 1

    lim_px = max(1, int(h * max_ratio))
    t_px = int(t * h / n)
    b_px = h - int((n - b) * h / n)          # 保留区的底边
    # 收尾：粗估的位置继续往里走，直到真实像素行不再是扁平近黑
    t_px = min(lim_px, t_px + _flat_dark_run(band, t_px, min(h, t_px + lim_px), False))
    b_px = max(h - lim_px, b_px - _flat_dark_run(band, max(0, b_px - lim_px), b_px, True))
    return t_px, h - b_px


def strip_bands(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int], dict]:
    """裁掉截图外壳，留下商品照片。

    返回 `(新图, 裁切框, 诊断)`。诊断里几个字段是给上传页做提示用的：
      · `cropped`     是否真的裁动了
      · `keep`        保留比例
      · `chrome_share`整图里外壳行占的比例（很高 = 多半根本没拍到衣服）
      · `garment_ok`  是否找到了像样的照片
    没裁动时框为全图，调用方照旧能拿到可用图片。
    """
    im = img.convert("RGB")
    w, h = im.size
    full = (0, 0, w, h)
    meta = {"cropped": False, "keep": 1.0, "chrome_share": 0.0,
            "garment_ok": True, "box": list(full)}
    if w < 48 or h < 48:
        return im, full, meta

    rows = _row_profile(im)
    n = len(rows)
    chrome = [_is_chrome(st, False) for st in rows]
    top_chrome = [_is_chrome(st, True) for st in rows]
    meta["chrome_share"] = round(sum(chrome) / n, 3)

    # ── 从上下两端各往中间走，停在外壳结束的地方 ──
    top = _walk(top_chrome, n, True, _TOP_MAX, _SLACK_TOP)
    bot = _walk(chrome, n, False, 1 - _BOT_MAX, _SLACK_BOT)

    if bot - top < n * _MIN_KEEP:
        return im, full, meta

    strip_cut = _trim_spec_strip(im, int(bot * h / n), n)
    if strip_cut < int(bot * h / n):
        meta["spec_strip"] = True
        bot = strip_cut * n / h

    # ── 左右留白（有的截图外面还套了一层白边）──
    band = im.crop((0, int(top * h / n), w, int(bot * h / n)))
    cols = _col_profile(band)
    m = len(cols)
    left = 0
    lim = int(m * _SIDE_MAX)
    while left < lim and _is_blank_col(cols[left]):
        left += 1
    right = m
    rfloor = int(m * (1 - _SIDE_MAX))
    while right > rfloor and _is_blank_col(cols[right - 1]):
        right -= 1

    pad_x, pad_y = int(w * 0.008), int(h * 0.008)
    box = (max(0, int(left * w / m) - pad_x),
           max(0, int(top * h / n) - pad_y),
           min(w, int(right * w / m) + pad_x),
           min(h, int(bot * h / n) + pad_y))

    if box[2] - box[0] < w * _MIN_KEEP or box[3] - box[1] < h * _MIN_KEEP:
        return im, full, meta

    # ── 削掉贴边的黑边 ──
    # 见 `_shave_dark_edges`：pad_y 把深色模式截图的 App 黑背景带进了画面，
    # 而 compose 的"居中留白"会让它显形成衣服上下的一条黑线。
    shave_t, shave_b = _shave_dark_edges(im.crop(box))
    if shave_t or shave_b:
        box = (box[0], box[1] + shave_t, box[2], box[3] - shave_b)
        meta["shaved"] = [shave_t, shave_b]

    meta["cropped"] = box != full
    meta["keep"] = round((box[2] - box[0]) * (box[3] - box[1]) / (w * h), 3)
    meta["box"] = list(box)
    # 留下的这一段里还有多少「像内容」的行 —— 太少说明裁下来的还是文字
    kept = chrome[int(box[1] * n / h):max(1, int(box[3] * n / h))]
    content_share = 1 - (sum(kept) / len(kept)) if kept else 0.0
    # 两个条件同时成立才敢说「没拍到衣服」。只看其中一个会误伤暗色单品的照片
    # （实测 id=105 藏青连帽卫衣，深色衣服让 83% 的行都像外壳）。
    meta["garment_ok"] = not (meta["chrome_share"] >= _NO_GARMENT_SHARE
                              and content_share < 0.30)
    meta["content_share"] = round(content_share, 3)
    return im.crop(box), box, meta


def compose(img: Image.Image, width: int = 1080, ratio: float = 1.25,
            margin: float = 0.96) -> Image.Image:
    """把图放进 4:5 竖版白底画布。内容占 96%，只留很窄的白边。

    为什么是 4:5 而不是 1:1：衣服照片基本都是竖构图（裤子、大衣尤其），
    塞进正方形左右会空出一大片，看起来就是「图太小」。
    前端缩略图也按 4:5 开框，两边一致 —— 不变形，也不用 `object-fit` 硬裁。
    """
    height = int(width * ratio)
    fitted = ImageOps.contain(img, (max(1, int(width * margin)),
                                    max(1, int(height * margin))), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    canvas.paste(fitted, ((width - fitted.width) // 2,
                          (height - fitted.height) // 2))
    return canvas
