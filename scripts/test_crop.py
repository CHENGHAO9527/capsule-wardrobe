# -*- coding: utf-8 -*-
"""图片标准化自检 + **跨模块符号存在性检查**。

为什么要有这个脚本：
    裁剪功能曾经**静默失效了整整一版** —— `main.py` 调的是 `imgs.strip_bands`，
    而那个函数早已改名成 `crop_to_content`。AttributeError 被上传接口里一个
    裸 `except Exception` 吞掉，于是每张展示图都是没裁过的整屏截图。
    用户看到的是「图很小」和「图片里面没裤子只有文字」，而日志里一个字都没有。

    所以这里做两件事：
      ① 拿**构造出来的**截图验证裁剪真的裁对了（含三种典型版式 + 干净商品照）
      ② 扫描所有 `模块.符号` 引用，确认符号真的存在 —— 这一条能拦住
         「改了名没改调用方」这一整类 bug，不只图片这一处

用法：python scripts/test_crop.py [--out 文件]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import catalog, colorlab, db, dupes, imgs, outfits, vision  # noqa: E402
from app.engine import graph, prebuy, rules  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

LINE = "─" * 64
_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"    {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails.append(label)


# ══════════════════════════════════════════════
# 构造三种典型截图
# ══════════════════════════════════════════════

def _photo(w: int, h: int, bg=(250, 250, 250), garment=(60, 62, 70)) -> Image.Image:
    """白底 + 中间一件"衣服"（深色块），模拟商品照。"""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    bw, bh = int(w * 0.30), int(h * 0.55)
    d.rounded_rectangle([(w - bw) // 2, (h - bh) // 2, (w + bw) // 2, (h + bh) // 2],
                        radius=w // 30, fill=garment)
    return im


def _screenshot(photo_pos: str, with_strip: bool = False) -> Image.Image:
    """拼一张 1080×2400 的淘宝详情截图。

    photo_pos：
      'middle' —— 上下都是外壳（状态栏 + 价格/标题/购买栏），照片夹在中间
      'top'    —— 照片贴着顶端（1:1 主图），下面全是外壳
    """
    W, H = 1080, 2400
    im = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(im)

    # 顶部状态栏：整行近黑，带一点亮色文字
    for y in range(0, 40):
        d.text((60 + y, 8), "18:06", fill=(230, 230, 230))
    d.text((520, 8), "5G  100%", fill=(230, 230, 230))

    if photo_pos == "middle":
        py0, py1 = 340, 1560
    else:
        py0, py1 = 0, 1080
    ph = _photo(W, py1 - py0, garment=(150, 152, 160) if photo_pos == "top" else (58, 60, 68))
    im.paste(ph, (0, py0))

    y = py1
    if with_strip:
        # 规格缩略图条：浅底 + 几个分离的小方块，上方一条扁平浅色分隔带
        d.rectangle([0, y, W, y + 22], fill=(236, 236, 236))     # 扁平分隔带
        y += 22
        d.rectangle([0, y, W, y + 120], fill=(244, 244, 244))
        for k in range(4):
            x = 60 + k * 250
            d.rectangle([x, y + 16, x + 60, y + 104], fill=(120, 122, 128))
        y += 120

    # 价格 / 标题 / 参数：暗底白字
    d.rectangle([0, y, W, H], fill=(12, 12, 14))
    for k in range(9):
        d.text((70, y + 60 + k * 130), "IFASHION 宽松阔腿卫裤 重磅简约潮男女同款",
               fill=(235, 235, 235))
        d.text((70, y + 96 + k * 130), "¥118  已售0  48小时内发货", fill=(240, 240, 240))
    # 底部橙色购买按钮
    d.rounded_rectangle([430, H - 190, 1050, H - 90], radius=16, fill=(255, 140, 0))
    d.text((600, H - 155), "立即购买", fill=(255, 255, 255))
    return im


def test_synthetic() -> None:
    print(LINE)
    print("① 构造截图 —— 裁剪有没有真的把外壳削掉")
    print(LINE)

    cases = [
        ("上下都是外壳（照片夹中间）", _screenshot("middle"), (0.10, 0.40), (0.55, 0.78)),
        ("照片贴顶端（1:1 主图）", _screenshot("top"), (0.00, 0.03), (0.38, 0.52)),
        ("外壳 + 规格缩略图条", _screenshot("middle", with_strip=True), (0.10, 0.40), (0.50, 0.72)),
    ]
    for label, im, top_ok, bot_ok in cases:
        clean, box, trim = imgs.strip_bands(im)
        H = im.height
        t, b = box[1] / H, box[3] / H
        print(f"\n  {label}  原图 {im.size} → {clean.size}  留 {trim['keep'] * 100:.0f}%")
        print(f"    裁切范围 y {t * 100:.1f}% ~ {b * 100:.1f}%")
        check(trim["cropped"], "真的裁动了（不是原样返回）")
        check(top_ok[0] <= t <= top_ok[1], f"上边界落在 {top_ok[0] * 100:.0f}~{top_ok[1] * 100:.0f}%")
        check(bot_ok[0] <= b <= bot_ok[1], f"下边界落在 {bot_ok[0] * 100:.0f}~{bot_ok[1] * 100:.0f}%")
        check(b - t > 0.15, "留下的区域够大")

    print("\n  干净商品照不许把衣服切掉 ——")
    clean_photo = _photo(1200, 1500)
    got, box, trim = imgs.strip_bands(clean_photo)

    def dark_pixels(im: Image.Image) -> int:
        g = im.convert("L")
        return sum(1 for v in g.getdata() if v < 120)

    before, after = dark_pixels(clean_photo), dark_pixels(got)
    print(f"    衣服像素 {before} → {after}（裁切 {box}）")
    check(after >= before * 0.995, "衣服一个像素都没被切掉")
    check(trim["garment_ok"], "判定「有衣服」")
    # 左右只允许削很窄的白边：全白画布上留白再多也不能靠裁它换尺寸
    check(box[2] - box[0] >= clean_photo.width * 0.85, "左右没有大砍（白边不是衣服，但也不值得为它冒险）")

    print("\n  compose 出来的画布必须是 4:5（前端缩略图按 4:5 开框）——")
    c = imgs.compose(_photo(600, 900), 540)
    check(c.size == (540, 675), f"尺寸 {c.size}")

    print("\n  深色模式截图：画布内部不许留黑线 ——")
    # 手机上深色模式的淘宝截图，照片上下紧贴着一大片纯黑的 App 背景。
    # 踩过：`strip_bands` 末尾的 `pad_y`（往外多留 0.8%，本意是别切到照片第一行）
    # 正好这 0.8% 就是纯黑；而 `compose` 是「等比缩放 + 居中留白」，
    # 裁切框的上下边落在画布**内部** —— 于是衣服上下各横一条黑线。
    # 实测真实数据：108 张展示图里 24 张中招（位置 2%~98% 不等，随截图而异）。
    dm = Image.new("RGB", (1080, 2400), (0, 0, 0))
    dm.paste(_photo(1080, 1100), (0, 620))
    got, box, trim = imgs.strip_bands(dm)
    canvas = imgs.compose(got, 540)

    def wide_dark_rows(im: Image.Image, thr: int = 60) -> list[int]:
        """横贯整条都暗的行 —— 衣服不会横贯整条，黑边才会。"""
        g = im.convert("L")
        w, h = g.size
        px = g.load()
        out = []
        for y in range(h):
            vals = [px[x, y] for x in range(0, w, 4)]
            if sum(vals) / len(vals) < thr:
                out.append(y)
        return out

    dark = wide_dark_rows(canvas)
    print(f"    裁切 {box} → 画布 {canvas.size}，横贯暗行 {len(dark)}/{canvas.height}")
    check(not dark, "画布内部没有横贯整条的黑线")
    # 反向：这次裁剪一开始就真的是"照片夹在黑色之间"，不是整张黑
    check(box[3] - box[1] < 2400 * 0.62, "留下的确实是照片那一段")


# ══════════════════════════════════════════════
# 符号存在性：拦住「改了名没改调用方」
# ══════════════════════════════════════════════

MODULES = {
    "imgs": imgs, "db": db, "colorlab": colorlab, "catalog": catalog,
    "vision": vision, "outfits": outfits, "dupes": dupes,
    "graph": graph, "prebuy": prebuy, "rules": rules,
}


def test_symbols() -> None:
    print()
    print(LINE)
    print("② 跨模块符号存在性 —— 所有 `模块.符号` 引用都要真的存在")
    print(LINE)

    checked = 0
    missing: list[str] = []
    unimported: list[str] = []
    for path in sorted(list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))):
        src = path.read_text(encoding="utf-8")
        # 全程用 ast，不做正则。
        # 正则有两个坑：① 会把文件名（`imgs.py`）和文档串里的 `vision.rank_outfits`
        # 当成代码引用；② 认不出 `from . import (a, b)` 这种多行 import
        # （实测漏报 88 处）。ast 只给真正的语法节点，两个坑一起没了。
        tree = ast.parse(src)
        scope: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    scope.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    scope.add(a.asname or a.name)
        wildcard = "*" in scope

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            mod, sym = node.value.id, node.attr
            if mod not in MODULES:
                continue
            checked += 1
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            if not hasattr(MODULES[mod], sym):
                missing.append(f"{where}  {mod}.{sym}")
            # 光有属性还不够 —— main.py 里 `dupes.find(...)` 写对了、`dupes` 却没 import，
            # 属性检查全绿、接口一调就 NameError 500。所以模块名本身也必须在作用域里。
            elif not wildcard and mod not in scope:
                unimported.append(f"{where}  {mod} 没被 import 进来")

    print(f"  扫了 {checked} 处引用，缺失符号 {len(missing)} 处，缺 import {len(unimported)} 处")
    for x in missing + unimported:
        print(f"    ✗ {x}")
    check(not missing and not unimported, "没有指向不存在符号的调用，也没有漏 import")

    print("\n  上传接口不许把裁剪包进裸 except（那正是它静默失效一版的原因）——")
    upload_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    seg = upload_src[upload_src.find("async def api_upload"):]
    seg = seg[:seg.find("async def api_vision", 10)] if "async def api_vision" in seg else seg
    check("imgs.strip_bands" in seg, "上传接口确实调用了 imgs.strip_bands")
    hard = re.search(r"strip_bands\([^)]*\)\s*\n\s*except", seg) is None
    check(hard, "strip_bands 没有被 try/except 包住")


# ══════════════════════════════════════════════
# 真实数据
# ══════════════════════════════════════════════

def test_real() -> None:
    print()
    print(LINE)
    print("③ 真实衣橱 —— 原图还在就一定能重算，且原始文件不被改动")
    print(LINE)

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM items WHERE stage='owned' AND img_raw IS NOT NULL AND img_raw<>''"
    ).fetchall()
    conn.close()

    if not rows:
        print("  没有带原图的在库件，跳过。")
        return

    n_crop = n_keep = n_bad = 0
    sizes_before = {}
    for r in rows:
        p = ROOT / "data" / "images" / Path(r["img_raw"]).name
        if not p.exists():
            continue
        sizes_before[r["id"]] = p.stat().st_size
        with Image.open(p) as f:
            im = f.convert("RGB")
        clean, box, trim = imgs.strip_bands(im)
        if trim["cropped"]:
            n_crop += 1
        else:
            n_keep += 1
        if not trim["garment_ok"] or clean.width * clean.height < im.width * im.height * 0.10:
            n_bad += 1
            print(f"    ! id={r['id']} {r['name']} 留 {trim['keep'] * 100:.0f}% "
                  f"有衣服={trim['garment_ok']}")
    print(f"  {len(sizes_before)} 张原图：裁动 {n_crop} 张 · 原样保留 {n_keep} 张 · 可疑 {n_bad} 张")
    check(n_bad == 0, "没有一张被裁到「看不出是衣服」")
    check(all((ROOT / 'data' / 'images' / Path(r['img_raw']).name).stat().st_size == s
              for r, s in [(r, sizes_before.get(r['id'], 0)) for r in rows]
              if s and (ROOT / 'data' / 'images' / Path(r['img_raw']).name).exists()),
          "原图文件大小没被改动（原图是唯一可复原的东西，任何时候都不该动）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.out:
        fh = open(args.out, "w", encoding="utf-8")
        old = sys.stdout

        class Tee:
            def write(self, s):
                old.write(s)
                fh.write(s)

            def flush(self):
                old.flush()
                fh.flush()
        sys.stdout = Tee()

    try:
        test_synthetic()
        test_symbols()
        test_real()
        print()
        print(LINE)
        print(f"结论：{'全部通过' if not _fails else str(len(_fails)) + ' 项未通过'}")
        for f in _fails:
            print(f"  ✗ {f}")
        print(LINE)
    finally:
        sys.stdout = old if args.out else sys.stdout
        if args.out:
            fh.close()
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
