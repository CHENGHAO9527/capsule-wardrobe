# -*- coding: utf-8 -*-
"""自动识别链路自检：主色提取 + 品类字典 + 模型输出校验。

不需要任何 API Key —— 校验那部分用伪造的模型输出来测，
因为真正会出问题的地方不是网络，是**模型胡说八道的时候我们怎么兜**。

    python scripts/test_auto.py --out auto.out.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from app import catalog, colorlab, vision  # noqa: E402

LINES: list[str] = []


def say(s: str = "") -> None:
    LINES.append(s)


# ══════════════════════════════════════════════
# 1. 主色提取
# ══════════════════════════════════════════════

def make_photo(fill, bg=(255, 255, 255), size=420) -> Image.Image:
    """造一张"纯色背景 + 中间一件衣服"的假照片。"""
    im = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(im)
    m = size // 5
    d.rounded_rectangle([m, m // 2, size - m, size - m // 2], radius=18, fill=fill)
    return im


# (画出来的颜色, 期望匹配到的调色板名, 背景)
CASES = [
    ((31, 42, 68), "藏蓝", (255, 255, 255)),      # 深藏青
    ((28, 28, 26), "黑", (255, 255, 255)),        # 黑
    ((195, 168, 123), "卡其", (255, 255, 255)),   # 卡其
    ((237, 230, 214), "米白", (255, 255, 255)),   # 米白（和背景很接近）
    ((138, 138, 133), "中灰", (255, 255, 255)),   # 中灰
    ((122, 43, 51), "酒红", (255, 255, 255)),     # 酒红
    ((74, 107, 138), "牛仔蓝", (255, 255, 255)),  # 牛仔蓝
    ((138, 138, 133), "中灰", (18, 18, 18)),      # 深色背景 + 深色衣服
    ((31, 42, 68), "藏蓝", (200, 200, 200)),      # 灰背景
    # 手机截图：上下各一条深色 UI 栏。四角取背景色会取到 UI 栏，
    # 靠它推前景会把颜色整个算反（实测黑色裤子测成 #FEFEFE）。
    ((28, 28, 26), "黑", "screenshot"),
]


def make_screenshot(fill) -> Image.Image:
    """造一张手机截图：顶部状态栏 + 中间照片 + 底部导航栏。"""
    w, h = 360, 800
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 56], fill=(0, 0, 0))              # 状态栏
    d.rectangle([0, h - 90, w, h], fill=(70, 40, 22))       # 导航栏
    d.rounded_rectangle([70, 200, w - 70, 620], radius=16, fill=fill)
    return im


def test_color() -> int:
    say("【主色提取】")
    bad = 0
    for rgb, want, bg in CASES:
        img = make_screenshot(rgb) if bg == "screenshot" else make_photo(rgb, bg)
        got = colorlab.dominant_color(img)
        ok = got["name"] == want
        if not ok:
            bad += 1
        tag = "ok  " if ok else "FAIL"
        label = "手机截图" if bg == "screenshot" else f"bg{bg}"
        say(f"  {tag} rgb{rgb} {label} → {got['name']}({got['hex']}) "
            f"实测{got['observed_hex']} 占比{got['share']} 外圈偏差{got.get('ring_dev')} "
            f"截图={got.get('framed')} 可信={got['confident']}")
    return bad


# ══════════════════════════════════════════════
# 2. 品类字典
# ══════════════════════════════════════════════

def test_catalog() -> int:
    say()
    say("【品类字典】")
    bad = 0

    # 每个品类的默认值必须齐全，否则自动填值会留下空洞
    need = {"formality", "thickness", "temp_min", "temp_max", "collar",
            "sleeve", "length_band", "fit", "material", "pattern"}
    for slot, cats in catalog.CATEGORIES.items():
        for c in cats:
            d = catalog.defaults_for(slot, c)
            miss = need - set(d)
            if miss:
                bad += 1
                say(f"  FAIL {slot}/{c} 缺字段 {miss}")
            d2 = catalog.defaults_for(slot, c)
            if not (1 <= d2["formality"] <= 5 and 1 <= d2["thickness"] <= 5):
                bad += 1
                say(f"  FAIL {slot}/{c} 正式度/厚度超出 1-5")
            if d2["temp_min"] >= d2["temp_max"]:
                bad += 1
                say(f"  FAIL {slot}/{c} 温度区间反了")
    say(f"  {'ok' if bad == 0 else 'FAIL'}  {sum(len(v) for v in catalog.CATEGORIES.values())} 个品类默认值齐全")

    # 自由品类名 → 枚举
    say("  自由品类名映射：")
    for hint, want in [("长袖衬衫", "衬衫"), ("米白色针织衫", "薄针织"), ("深色牛仔裤", "牛仔裤"),
                       ("短裙", "短裙"), ("半身裙", "半身裙"), ("休闲裤", "直筒裤"),
                       ("高跟鞋", "皮鞋"), ("连帽卫衣", "卫衣"), ("羽绒服", "羽绒服"),
                       ("某个看不懂的东西", None)]:
        got = catalog.nearest_category(hint)
        ok = got == want
        if not ok:
            bad += 1
        say(f"    {'ok  ' if ok else 'FAIL'} {hint!r} → {got!r}（期望 {want!r}）")
    return bad


# ══════════════════════════════════════════════
# 3. 模型输出校验（重点）
# ══════════════════════════════════════════════

MESSY = [
    ("正常输出", {"slot": "L1", "category": "衬衫", "name": "白色棉质衬衫", "formality": 3,
                  "thickness": 2, "temp_min": 10, "temp_max": 30, "collar": "翻领",
                  "sleeve": "长袖", "length_band": "常规", "fit": "合身",
                  "material": "棉", "pattern": "纯色", "confidence": 0.92}),
    ("槽位用中文", {"slot": "上衣", "category": "长袖衬衫", "name": "蓝色衬衫",
                    "formality": 3, "thickness": 2, "temp_min": 8, "temp_max": 28}),
    ("槽位非法且品类也认不出", {"slot": "不知道", "category": "某个东西", "name": "X"}),
    ("数值越界", {"slot": "L3", "category": "大衣", "name": "驼色大衣", "formality": 9,
                  "thickness": 0, "temp_min": -80, "temp_max": 90, "confidence": 5}),
    ("温度区间反了", {"slot": "L3", "category": "夹克", "name": "夹克",
                      "temp_min": 25, "temp_max": 5}),
    ("温度区间过窄", {"slot": "L1", "category": "T恤", "name": "T恤",
                      "temp_min": 20, "temp_max": 22}),
    ("裤子给了领型", {"slot": "L4", "category": "牛仔裤", "name": "牛仔裤",
                      "collar": "翻领", "sleeve": "长袖", "length_band": "长"}),
    ("全是垃圾值", {"slot": "L2", "category": "毛衣", "name": "毛衣",
                    "collar": "不知道", "material": "塑料", "pattern": "豹纹", "fit": "紧身"}),
    ("空对象", {}),
]


def test_validate() -> int:
    say()
    say("【模型输出校验】（用伪造输出测，不需要 API Key）")
    bad = 0
    for title, raw in MESSY:
        out = vision.validate(raw)
        # 硬性检查：产出必须能直接入库
        problems = []
        if out["slot"] not in catalog.SLOT_NAMES:
            problems.append("槽位非法")
        if out["category"] not in catalog.CATEGORIES[out["slot"]]:
            problems.append("品类不属于该槽位")
        if not (1 <= out["formality"] <= 5):
            problems.append("正式度越界")
        if not (1 <= out["thickness"] <= 5):
            problems.append("厚度越界")
        if not (-30 <= out["temp_min"] < out["temp_max"] <= 45):
            problems.append("温度区间非法")
        if out["temp_max"] - out["temp_min"] < 8:
            problems.append("温度区间过窄")
        if out["collar"] and out["collar"] not in catalog.COLLARS:
            problems.append("领型非法")
        if out["material"] and out["material"] not in catalog.MATERIALS:
            problems.append("材质非法")
        if out["slot"] in ("L4", "L5", "ACC") and out["collar"]:
            problems.append("裤子/鞋不该有领型")
        if out["slot"] in ("L5", "ACC") and out["length_band"]:
            problems.append("鞋/配饰不该有衣长")
        if problems:
            bad += 1

        tag = "ok  " if not problems else "FAIL"
        say(f"  {tag}{title}")
        say(f"        → slot={out['slot']} category={out['category']} name={out['name']!r} "
            f"正式{out['formality']} 厚{out['thickness']} {out['temp_min']}~{out['temp_max']}℃ "
            f"领{out['collar'] or '-'} 材{out['material'] or '-'} conf={out['_confidence']}")
        if out["_dropped"]:
            say(f"        丢掉字段：{out['_dropped']}")
        if problems:
            say(f"        !! {problems}")
    return bad


# ══════════════════════════════════════════════
# 4. 接线检查
# ══════════════════════════════════════════════

def test_wiring() -> int:
    say()
    say("【接线检查】")
    bad = 0
    try:
        from app import main  # noqa: F401
        say("  ok   app.main 可以导入（路由接线正常）")
    except Exception as exc:
        bad += 1
        say(f"  FAIL 导入 app.main 失败：{type(exc).__name__}: {exc}")
        return bad

    cfg = vision.public_config()
    say(f"  ok   视觉识别配置：configured={cfg['configured']} enabled={cfg['enabled']} "
        f"provider={cfg['provider'] or '(未选)'} model={cfg['model'] or '(未填)'}")
    say(f"  ok   内置预设 {len(cfg['presets'])} 个：" +
        "、".join(p["label"] for p in cfg["presets"]))

    enabled, reason = vision.available()
    say(f"  ok   当前可用性：{enabled}（{reason or '已配置'}）")
    if not enabled:
        say("       —— 未配置模型时，录入退回「品类默认值」模式，仍然可用")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把报告写到文件（PowerShell 管道会毁掉中文编码）")
    args = ap.parse_args()

    bad = 0
    bad += test_color()
    bad += test_catalog()
    bad += test_validate()
    bad += test_wiring()

    say()
    say("=" * 58)
    say(f"失败项：{bad}" + ("  全部通过" if bad == 0 else "  ← 需要修"))

    text = "\r\n".join(LINES)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}  ({bad} failures)")
    else:
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode("utf-8", "replace").decode("utf-8", "replace"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

