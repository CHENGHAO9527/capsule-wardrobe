# -*- coding: utf-8 -*-
"""找重复件：同一件衣服被录了两次（多半是同一个商品从两个链接各录了一遍）。

**只报告，不删。** 删衣服不可逆，而且"看起来像"和"就是同一件"是两回事
（同款不同色、同色不同款都极常见）。这里只把可疑的对子连证据列出来，由人拍板。

判据的主路是**逐像素 RMS**：把两张原图各自裁掉截图外壳、归一化到 32×32 灰度后
逐像素比。实测同一张图 RMS = 0.0，不同的图 RMS > 25，中间几乎没有灰区。
指纹（dHash）做辅路。

为什么要两路：dHash 只看左右明暗的相对关系，两张不同的白底商品图可能给出相同指纹 ——
最初只用 8×8 dHash，29 件里报了 28 组，等于没筛。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from . import db, imgs

# 逐像素 RMS 阈值：越小越严。超过它就认为不是同一张图。
RMS_MAX = 14.0
# 指纹汉明距离阈值（256 位）
HASH_MAX = 34


def _dhash(im: Image.Image, size: int = 17) -> int:
    g = ImageOps.fit(im.convert("L"), (size, size - 1), Image.LANCZOS)
    px = g.load()
    bits = 0
    for y in range(size - 1):
        for x in range(size - 1):
            bits = (bits << 1) | (1 if px[x, y] > px[x + 1, y] else 0)
    return bits


def _sig(im: Image.Image) -> tuple[int, list[int]]:
    fit = ImageOps.fit(im.convert("L"), (32, 32), Image.LANCZOS)
    return _dhash(im), list(fit.getdata())


def _rms(a: list[int], b: list[int]) -> float:
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _brief(it: dict) -> dict:
    return {"id": it["id"], "name": it["name"], "slot": it["slot"],
            "category": it.get("category"), "color_main": it.get("color_main"),
            "color_hex": it.get("color_hex"), "img_display": it.get("img_display"),
            "formality": it.get("formality"), "thickness": it.get("thickness"),
            "temp_min": it.get("temp_min"), "temp_max": it.get("temp_max"),
            "purchase_price": it.get("purchase_price"),
            "tags": it.get("tags")}


def find(items: list[dict], img_dir: Path | None = None) -> list[dict]:
    """返回可疑对子。items 只该传在库件（候选件不参与 —— 它们在衣橱外面）。"""
    base = img_dir or db.IMG_DIR
    sigs: list[tuple[dict, int, list[int]]] = []
    for it in items:
        raw = it.get("img_raw")
        if not raw:
            continue
        p = base / Path(raw).name
        if not p.exists():
            continue
        try:
            with Image.open(p) as f:
                im = ImageOps.exif_transpose(f).convert("RGB")
            clean, _box, _trim = imgs.strip_bands(im)
            h, px = _sig(clean)
        except Exception:
            continue
        sigs.append((it, h, px))

    out: list[dict] = []
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            a, ha, pa = sigs[i]
            b, hb, pb = sigs[j]
            rms = _rms(pa, pb)
            hd = _hamming(ha, hb)
            if rms > RMS_MAX and hd > HASH_MAX:
                continue
            same_cat = (a.get("slot") == b.get("slot")
                        and a.get("category") == b.get("category"))
            # 「像素完全一样」是最硬的证据；只有指纹接近、像素不像的降级为「看着像」
            exact = rms <= 1.0 and hd <= 4
            out.append({
                "a": _brief(a), "b": _brief(b),
                "rms": round(rms, 1), "hash_dist": hd,
                "same_category": same_cat,
                "exact": exact,
                "verdict": ("同一张图 —— 基本可以确定是重复录入" if exact
                            else "看着像，建议自己再对一眼"),
            })

    out.sort(key=lambda x: (not x["exact"], x["rms"]))
    return out
