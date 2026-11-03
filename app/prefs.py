"""用户偏好：体感舒适温度（「26℃穿衣法则」里那个 26 的位置）。

为什么单独一个文件、落在 data/ 下：它**不是"某件衣服的属性"**，而是
"这个人怕不怕冷" —— 跟天气的城市、模型的服务商一样，是整库级别的设置，
所以和 `weather.json` / `vision.json` 一个路子，不进 items 表。

**为什么默认 22 而不是法则原文的 26**：26 是标给儿童的（怕冷、代谢不同）。
拿本机真实的 27 件衣橱从 30℃ 扫到 0℃，26 会让"16℃ 穿 T恤+卫衣+夹克"被判成
不够暖（那套的保暖值只有 9，26-16=10 差 1），而 21~22 才对得上体感：
  · 舒适线 26 → 这套衣橱只覆盖到 17℃
  · 舒适线 22 → 覆盖到 13℃
  · 舒适线 21 → 覆盖到 12℃
所以**做成可调的**，不写死。改它在 `/settings`。
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "prefs.json"

# 舒适线允许的范围。上限给到 28 是留出"很怕冷"的人；下限 18 是"很抗冻"。
COMFORT_MIN, COMFORT_MAX = 18.0, 28.0
# 法则原文的 26（儿童标准），界面上作为一档参考值列出来
COMFORT_FOLK = 26.0

DEFAULT_PREFS = {
    "comfort": 22.0,
}


def _read() -> dict:
    """读配置。**任何失败都退回默认值** —— 这个值挂在"打开首页"这条路径上，
    一个坏掉的 json 不该让首页打不开。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    prefs = dict(DEFAULT_PREFS)
    prefs.update(_read())
    prefs["comfort"] = _clamp(prefs.get("comfort"))
    return prefs


def save(patch: dict) -> dict:
    prefs = load()
    if "comfort" in patch and patch["comfort"] is not None:
        prefs["comfort"] = _clamp(patch["comfort"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    return prefs


def comfort() -> float:
    """当前的体感舒适温度。引擎每一处都走这里，不要在别处写死。"""
    return load()["comfort"]


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULT_PREFS["comfort"]
    return max(COMFORT_MIN, min(COMFORT_MAX, round(f, 1)))
