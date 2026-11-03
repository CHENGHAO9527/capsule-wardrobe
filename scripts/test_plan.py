# -*- coding: utf-8 -*-
"""天气 + 多天排布自检。**全程离线，不需要网络也不需要 API Key。**

    python scripts/test_plan.py --out test_plan.out.txt

四块：
  ① 天气模块的纯函数与降级路径（打桩，不联网）
  ② 排布引擎跑在**真实衣橱**上：合法性、跨天不重复、旅行少带件、打包清单完整性
  ③ 旅行的分段目的地（两个旅游地）：合并那一层，全程打桩
  ④ 交叉检查：新接口引用的符号是否真的存在、模块是否已 import
     （这个项目栽过好几次"改名没改调用方"，异常被吞掉就不报错）
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, plan, weather  # noqa: E402
from app.engine import graph, rules  # noqa: E402

L: list[str] = []
FAILS: list[str] = []


def say(s: str = "") -> None:
    print(s)
    L.append(s)


def ok(cond: bool, label: str) -> bool:
    if cond:
        say("  ✓ " + label)
    else:
        say("  ✗ " + label)
        FAILS.append(label)
    return bool(cond)


def head(s: str) -> None:
    say()
    say("=" * 64)
    say(s)
    say("=" * 64)


# ══════════════════════════════════════════════
# ① 天气：纯函数 + 降级
# ══════════════════════════════════════════════

def test_weather() -> None:
    head("① 天气模块（离线，打桩）")

    say("· 温度折算：预报的最高/最低 → 出门穿的那个温度")
    cases = [
        ((10, 20), "avg", 15), ((10, 20), "am", 10), ((10, 20), "pm", 20),
        ((-2, 6), "avg", 2), ((-2, 6), "am", -2), ((-2, 6), "pm", 6),
    ]
    for (lo, hi), bias, want in cases:
        got = weather.dress_temp(lo, hi, bias)
        ok(got == want, f"{lo}~{hi}℃ 按 {bias} 算 = {got}（期望 {want}）")
    ok(weather.dress_temp(None, 20, "avg") == 20, "只有最高温时不崩，直接用最高")
    ok(weather.dress_temp(None, None, "avg") is None, "两个都没有时返回 None，不编一个 0")

    say("· 天气码 → 中文")
    ok(weather.weather_text(0) == "晴", "0 → 晴")
    ok(weather.weather_text(61) == "小雨", "61 → 小雨")
    ok(weather.weather_text(9999) == "", "没见过的码返回空串，不瞎猜")
    ok(weather.is_wet(61) and weather.is_wet(80) and weather.is_wet(95), "雨/阵雨/雷阵雨都算湿")
    ok(not weather.is_wet(0) and not weather.is_wet(3), "晴和多云不算湿")

    say("· 解析 open-meteo 的返回（缺数据的行必须跳过，不能编）")
    fake = {"daily": {
        "time": ["2026-09-21", "2026-09-22", "2026-09-23"],
        "temperature_2m_max": [24, None, 26],
        "temperature_2m_min": [14, None, 16],
        "weather_code": [3, None, 61],
        "precipitation_probability_max": [10, None, 80],
        "wind_speed_10m_max": [12.5, None, 9.0],
    }}
    days = weather.parse_daily(fake, "avg")
    ok(len(days) == 2, f"3 天里只有 2 天有数据 → 解析出 {len(days)} 天")
    ok(days[0]["temp"] == 19 and days[0]["tmin"] == 14 and days[0]["tmax"] == 24,
       "第一天 14~24℃ → 按平均算 19℃")
    ok(days[1]["wet"] is True and days[1]["text"] == "小雨", "第三天的小雨被认出来了")
    ok(weather.parse_daily({}, "avg") == [], "空返回解析成空列表，不抛异常")

    say("· 降级路径：没城市 / 没缓存 / 有旧缓存")
    tmp = Path(tempfile.mkdtemp()) / "weather.json"
    orig_path, orig_fetch = weather.CONFIG_PATH, weather._fetch_json
    try:
        weather.CONFIG_PATH = tmp

        r = weather.forecast(days=7)
        ok(r["ok"] is False and r.get("need_city") and r.get("days") == [],
           "没设置城市 → ok=False + need_city（今日页据此安静地退回手动滑杆）")
        ok("day" in r, "没城市这一支也带 day 键（形状统一，前端少一类分支）")

        def boom(*a, **k):
            raise OSError("模拟断网")

        weather._fetch_json = boom
        r = weather.forecast(days=7)
        ok(r["ok"] is False and r.get("reason"),
           f"设置过城市但断网、且没缓存 → ok=False + 有话说：{r.get('reason', '')[:40]}")
        ok("day" in r and r["day"] is None, "断网这一支 day=None，但键还在")

        day = {"date": weather._local_today(None), "tmin": 14, "tmax": 24, "temp": 19,
               "temp_am": 14, "temp_pm": 24, "code": 3, "text": "阴",
               "wet": False, "precip": 10, "wind": 12.0}
        weather._write({"config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
                        "cache": {"lat": 1.0, "lon": 2.0, "at": time.time(),
                                  "bias": "avg", "days": [day]}})
        r = weather.forecast(days=1)
        ok(r["ok"] and r["cached"] and not r["stale"],
           "半小时内的缓存直接命中（不打网络）")
        ok(r["days"][0]["temp"] == 19, "命中缓存的那天温度是对的")
        # 今日页读的是 day，不是 days[0] —— 少了它，天气那块会无声地不显示
        ok(r.get("day") == r["days"][0], "返回里带 day（今日页读的就是它）")
        ok(r["day"]["temp"] == 19, "day 里就是今天那天的温度")

        weather._write({"config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
                        "cache": {"lat": 1.0, "lon": 2.0, "at": time.time() - 99999,
                                  "bias": "avg", "days": [day]}})
        r = weather.forecast(days=1)
        ok(r["ok"] and r["stale"] and r["days"],
           "缓存过期 + 断网 → 回退旧缓存并标 stale（比空白屏有用）")

        ok(r.get("reason"), f"stale 时必须说清原因：{r.get('reason', '')[:48]}")

        say("· 换档（早/平均/午后）不用重新联网")
        cfg = weather.save_config({"bias": "pm"})
        got = json.loads(tmp.read_text(encoding="utf-8"))
        ok(cfg["bias"] == "pm", "档位存下来了")
        ok(got["cache"]["days"][0]["temp"] == 24,
           f"缓存里的温度就地重算成午后档 {got['cache']['days'][0]['temp']}℃"
           f"（那天 14~24℃）")

        say("· search_city 遇到网络错误返回空列表，不抛异常")
        ok(weather.search_city("上海") == [], "断网时返回 []（由调用方决定怎么提示）")
        ok(weather.search_city("") == [], "空关键词直接返回 []，不发请求")

        say("· forecast_for：查别人的城市，不许动配置")
        before = weather.load_config().copy()

        def fake_fetch(url, params, timeout=8):
            return {"daily": {"time": ["2026-10-01"], "temperature_2m_max": [30],
                              "temperature_2m_min": [22], "weather_code": [0],
                              "precipitation_probability_max": [0],
                              "wind_speed_10m_max": [5]}}

        weather._fetch_json = fake_fetch
        fc = weather.forecast_for(35.68, 139.76, days=3, label="东京 · 日本")
        ok(fc["ok"] and fc["days"][0]["temp"] == 26 and fc["label"] == "东京 · 日本",
           "目的地预报取到了（26℃）")
        ok(weather.load_config() == before, "查目的地之后，常住城市配置一个字都没变")
    finally:
        weather.CONFIG_PATH, weather._fetch_json = orig_path, orig_fetch


# ══════════════════════════════════════════════
# ①b 缓存跨过零点（2026-09-28 00:16 实测撞到，不是推测）
# ══════════════════════════════════════════════

def check_midnight() -> None:
    """⚠️ 这一节的每一个数字都是**相对"今天"算出来的**。

    为什么必须单独一节：那天 23:47 跑全套自检**全绿**，00:16 再跑就红了 ——
    同一个二进制、同一个数据库、同一份代码，只因为**跨过了零点**。
    根因是天气缓存 TTL 30 分钟：23:59:15 取的那一份，`days[0]` 就是 09-27；
    过了零点它还在有效期内，于是

      · `/api/timeline` 第一行印的是**昨天**（周日），"今天"掉到第二行；
      · 7 天窗口被顶掉一天 —— 10-04 直接消失；
      · **最要命的**：`_present` 把 `days[0]` 当 `day`，
        今日页于是**拿昨天的气温去挑今天的衣服**，而且全程不报错。

    白天写测试永远碰不到它（同一天）。所以这里**不许写死日期** ——
    写死就变成了下一颗同类型的雷。"""
    head("①b 天气缓存跨过零点（昨天的预报不许当成今天）")
    today = date.fromisoformat(weather._local_today(None))
    iso = today.isoformat()
    yday = (today - timedelta(days=1)).isoformat()
    tmr = (today + timedelta(days=1)).isoformat()

    say("· drop_past_days 是个纯函数，先把它的边界钉住")
    def d(x):
        return {"date": x, "tmin": 10, "tmax": 20, "temp": 15,
                "temp_am": 10, "temp_pm": 20, "code": 3, "text": "阴",
                "wet": False, "precip": 0, "wind": 5.0}
    kept = weather.drop_past_days([d(yday), d(iso), d(tmr)], iso)
    ok(len(kept) == 2 and kept[0]["date"] == iso,
       f"丢掉昨天，留下今天和明天（{len(kept)} 天，头一天 {kept[0]['date']}）")
    ok(weather.drop_past_days([], iso) == [], "空列表原样返回，不抛异常")
    only_past = weather.drop_past_days([d(yday)], iso)
    ok(len(only_past) == 1,
       "整份都在过去时原样返回 —— 宁可由 stale 标记说清，也不要返回空数组让整页白屏")

    say("· 零点前写的缓存，零点后不许再当「新鲜的」")
    tmp = Path(tempfile.mkdtemp()) / "weather.json"
    orig_path, orig_fetch = weather.CONFIG_PATH, weather._fetch_json
    calls = []

    def fetched(url, params, timeout=8):
        calls.append(params)
        return {"utc_offset_seconds": 28800,
                "daily": {"time": [iso, tmr],
                          "temperature_2m_max": [20, 21],
                          "temperature_2m_min": [10, 11],
                          "weather_code": [0, 0],
                          "precipitation_probability_max": [0, 0],
                          "wind_speed_10m_max": [5, 5]}}

    try:
        weather.CONFIG_PATH = tmp
        weather._write({
            "config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
            # at 是"刚刚" —— 半小时内，TTL 完全没到。坏的只是**日期轴**。
            "cache": {"lat": 1.0, "lon": 2.0, "at": time.time(), "bias": "avg",
                      "days": [d(yday), d(iso), d(tmr)],
                      "today": yday, "utc_offset_seconds": 28800},
        })
        weather._fetch_json = fetched
        r = weather.forecast(days=2)
        ok(calls, "TTL 没过、但日期轴是昨天的缓存**被判定失效并重新拉了**（不是闷头发出去）")
        ok(r["days"][0]["date"] == iso,
           f"重新拉完之后第一天是今天（{r['days'][0]['date']}）")
        ok(r["day"]["date"] == iso,
           "返回的 day 也是今天 —— 今日页就是靠它挑衣服的")

        say("· 断网兜底那条路也要剔掉昨天（否则 stale 会把昨天的温度当今天）")
        calls.clear()
        weather._write({
            "config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
            "cache": {"lat": 1.0, "lon": 2.0, "at": time.time(), "bias": "avg",
                      "days": [d(yday), d(iso), d(tmr)],
                      "today": yday, "utc_offset_seconds": 28800},
        })

        def boom(*a, **k):
            raise OSError("模拟断网")

        weather._fetch_json = boom
        r = weather.forecast(days=2)
        ok(r["ok"] and r["stale"], "断网 → 回退旧缓存并标 stale")
        ok(r["days"][0]["date"] == iso,
           f"回退的那份里第一天仍是今天（{r['days'][0]['date']}），不是昨天")

        say("· 老格式缓存（没有 today 字段）也不能逃过这一关")
        weather._write({
            "config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
            "cache": {"lat": 1.0, "lon": 2.0, "at": time.time(), "bias": "avg",
                      "days": [d(yday), d(iso), d(tmr)]},   # ← 无 today / 无 offset
        })
        calls.clear()
        weather._fetch_json = fetched
        r = weather.forecast(days=2)
        ok(calls, "锚点退回「第一天」也能识破（老缓存不用等升级后手动清）")
        ok(r["days"][0]["date"] == iso, f"第一天仍是今天（{r['days'][0]['date']}）")

        say("· 反向对照：日期轴正确时，缓存必须照常命中（别把缓存改废了）")
        weather._write({
            "config": {"city": "测试市", "lat": 1.0, "lon": 2.0, "bias": "avg"},
            "cache": {"lat": 1.0, "lon": 2.0, "at": time.time(), "bias": "avg",
                      "days": [d(iso), d(tmr)],
                      "today": iso, "utc_offset_seconds": 28800},
        })
        calls.clear()
        r = weather.forecast(days=2)
        ok(not calls and r["cached"],
           "同一天的缓存仍然直接命中 —— 这条防的是「为了修 bug 把缓存整个关掉」")
        ok((r["days"][0]["date"] if isinstance(r["days"][0], dict) else r["days"][0]) == iso,
           "命中缓存时第一天还是今天")
    finally:
        weather.CONFIG_PATH, weather._fetch_json = orig_path, orig_fetch


# ══════════════════════════════════════════════
# ② 排布引擎（真实衣橱）
# ══════════════════════════════════════════════

def _mk(iid: int, slot: str) -> dict:
    """造一件假单品。

    **字段不能瞎造**：试删现在是直接问引擎枚举（`Closet.count_paths` → `rules`），
    引擎真的会读这件单品。`is_neutral` 绕过 R4 撞色，剩下的规则自然全过 ——
    这样测的是**试删机制**本身，不掺衣橱数据的随机性。

    ⚠️ **2026-09-23「26℃穿衣法则」之后，"温度给满"这条老规矩不成立了。**
    以前"够不够暖"由全局温度带决定，给满区间（-10~40）正好隔离出机制；
    现在**每件自己的保暖值直接参与判定**，再给满就等于造了一批
    "40℃ 还能穿夹克"的假衣服 —— 试删的可行性判定会算出一堆假结论。
    所以按 `catalog` 的品类默认值给：内搭 1℃ / 中间层 4℃ / 外套 3℃。
    """
    shape = {
        "L1": ((-10, 38), 2, 1),
        "L2": ((5, 20), 3, 4),
        "L3": ((0, 22), 3, 3),
        "L4I": ((-10, 14), 4, 0),
        "L4": ((5, 28), 3, 0),
        "L5": ((5, 30), 3, 0),
    }
    (lo, hi), thick, warm = shape.get(slot, ((-10, 40), 3, 0))
    return {"id": iid, "slot": slot, "name": f"{slot}-{iid}",
            "temp_min": lo, "temp_max": hi, "formality": 2,
            "thickness": thick, "warmth": warm, "is_neutral": True}


def _wear_of(plan_days: list[dict]) -> tuple[dict[int, list[int]], list[tuple]]:
    """排出来的计划里：(每件贴身层出现在第几天, 每天的上衣栈)。

    贴身层的两条规矩（不连穿、单件不超上限）和"换上衣才算换穿搭"都靠它来查。
    """
    wear: dict[int, list[int]] = {}
    looks: list[tuple] = []
    for i, d in enumerate(plan_days):
        if not d.get("outfit"):
            continue
        looks.append(plan._look(d["outfit"]))
        for it in d["outfit"]["items"]:
            if it["slot"] in plan.NO_ADJACENT:
                wear.setdefault(int(it["id"]), []).append(i)
    return wear, looks


def test_shrink() -> None:
    """旅行"少带一件"的机制 —— 直接对着机制测，不靠端到端。

    ⚠️ 试删**必须直接问引擎**，不能问候选池。候选池是按"看起来不一样"挑出来的，
    用它判断"少带一件还行不行"会系统性低估 —— 实测真实衣橱跑出"试删 0 件"。

    ⚠️ 光判"每天还有得穿"也不够。那会一路省到"一件 T 恤 + 一条裤子"：
    内搭的穿着规矩（一件最多穿 2 天、不连穿）和常识下限（4 天以上两条裤子）
    必须一起算，否则旅行清单会荒唐到没人敢用。
    """
    head("② 少带一件（shrink）的机制")

    # ⚠️ 试删夹具的温度取 **22℃**：换到「26℃穿衣法则」之后，
    #    "一套只穿内搭"合不合法由保暖值说了算（T恤 1℃ → 22+1 ≥ 舒适线 22），
    #    16℃ 只穿内搭是**不够暖**的，那样这批"只有 L1+L4+L5"的夹具
    #    会一路走到"一天都排不出来"的分支，容量闸压根测不到。
    #    这一节测的是**容量/轮换机制**，不是保暖，所以要挑一个够暖的温度。
    T = 22.0

    def by_id_map(ts, ps, ss):
        return {i: _mk(i, s) for i, s in
                [(i, "L1") for i in ts] + [(i, "L4") for i in ps]
                + [(i, "L5") for i in ss]}

    # 4 件内搭 + 2 条裤 + 2 双鞋，排 7 天。
    # 内搭一件最多穿 2 天 → 4 件刚好够 7 天（3 件只顶得住 6 天）
    # 裤子 4 天以上留 2 条、鞋至少 1 双 → 只有多出来的那双鞋能省
    by = by_id_map([1, 2, 3, 4], [11, 12], [21, 22])
    seven = [T] * 7
    items = set(by)
    kept, dropped = plan.shrink(by, seven, items)
    say(f"     4 内搭 + 2 裤 + 2 鞋排 7 天 → 从 {len(items)} 件试删到 {len(kept)} 件，"
        f"删掉 {sorted(dropped)}")
    ok(len(dropped) == 1, f"只省得下 1 件（多出来的那双鞋），实际 {len(dropped)} 件")
    ok(sum(1 for i in kept if by[i]["slot"] == "L1") == 4,
       "4 件内搭全留着 —— 7 天天天换上衣就靠它们")
    ok(sum(1 for i in kept if by[i]["slot"] == "L4") == 2,
       "两条裤子都留着（一条身上一条换洗）")
    ok(plan._can_serve(by, seven, kept), "省完之后依然每天一套、内搭轮得过来")

    # 最值钱的一条：**3 件内搭撑不起 7 天**（一件最多穿 2 天）→ 判为不可行，
    # 试删不敢再砍。没有它，"少带件"会砍成"7 天一件 T 恤"。
    by3 = by_id_map([1, 2, 3], [11, 12], [21, 22])
    ok(not plan._can_serve(by3, [T] * 7, set(by3)),
       "3 件内搭排 7 天 → 不可行（一件最多穿 2 天，轮不过来）")
    kept3, _d3 = plan.shrink(by3, [T] * 7, set(by3))
    ok(sum(1 for i in kept3 if by3[i]["slot"] == "L1") == 3,
       f"内搭本来就不够时，试删一件都不砍（还剩 {len(kept3)} 件）")

    # 天数短，内搭要求跟着降
    ok(plan._can_serve(by3, [T] * 6, set(by3)), "同样 3 件内搭，排 6 天可行（正好 3×2）")

    # 容量够还得排得开：2 件内搭排 4 天行（1,2,1,2），排 5 天不行
    by2 = by_id_map([1, 2], [11], [21])
    ok(plan._can_serve(by2, [T] * 4, set(by2)),
       "2 件内搭排 4 天可行（隔天换着穿，每件各 2 天）")
    ok(not plan._can_serve(by2, [T] * 5, set(by2)), "2 件内搭排 5 天不行")

    # 某一天这些衣服一件都穿不了（比如气温超出所有单品的上限）
    # —— 那一天**不算"因为带少了"**，否则试删会被它整体卡死。
    # 实测：真实衣橱有一天气温 31℃（T 恤上限 30℃）排不出来，
    # 那一趟旅行就一件都省不下来（dropped_count=0）。
    ok(plan._can_serve(by2, [T] * 4 + [45.0], set(by2)),
       "4 个能排的天 + 1 个本来就排不出来的天 → 放行（不把缺层位单品算成带少了）")
    ok(not plan._can_serve(by2, [T] * 5 + [45.0], set(by2)),
       "能排的变成 5 天 → 2 件内搭又不够了，照样拦住（关卡没有整个拆掉）")
    ok(plan._can_serve(by2, [45.0] * 3, set(by2)),
       "一天都排不出来 → 放行（那是缺层位单品的事，与带哪几件无关）")

    # ── 判可行性必须用**当天最冷那会儿**（warms），不是出门温度 ──
    # 踩过（2026-09-22）：口径不一致 → 15℃ 的下午被当成"不用中间层"，
    # 试删把两件卫衣全砍了，而那天早上 12℃（<14℃ 档必需 L2+L3）根本穿不出门；
    # 紧接着重排填不满 → 按"宁可多带一件"整体回退 → **一件都没省下来**。
    # 用户看到的就是"我要带 7 条裤子"，而"少带件"静默失效。
    def mk2(iid: int, slot: str, lo: float, hi: float) -> dict:
        d = _mk(iid, slot)
        d["temp_min"], d["temp_max"] = lo, hi
        return d

    warm_by = {i: _mk(i, "L1") for i in (1, 2, 3, 4)}
    warm_by |= {11: _mk(11, "L4"), 12: _mk(12, "L4"), 21: _mk(21, "L5"),
                31: mk2(31, "L3", -10, 20)}          # 外套
    all_w = set(warm_by)
    ok(plan._can_serve(warm_by, [T] * 7, all_w),
       f"{T:g}℃：只靠内搭就够暖，没外套也排得出来")
    # 14℃ 需要 8℃ 保暖值（舒适线 22 − 14），而"内搭 1 + 外套 3 = 4"远远不够
    ok(not plan._l1_domain(list(warm_by.values()), 14.0, "通勤"),
       "14℃：光靠「内搭 + 外套」不够暖（26℃法则：1 + 3 < 8）")
    warm_by[41] = mk2(41, "L2", -10, 25)             # 补一件中间层 → 1 + 4 + 3 = 8
    all_w = set(warm_by)
    ok(plan._can_serve(warm_by, [14.0] * 7, all_w), "补上中间层（1+4+3=8），14℃ 就排得出来")
    ok(not plan._can_serve(warm_by, [14.0] * 7, all_w - {41}, base_ok=[True] * 7),
       "原始清单穿得了这天、砍完穿不了 → 拦住（不许把「砍多了」当成「缺层位单品」）")
    ok(plan._can_serve(warm_by, [14.0] * 7, all_w - {41}, base_ok=[False] * 7),
       "原始清单本来也穿不了这天 → 放行（缺层位单品与带哪几件无关）")

    # 原始清单**本身**就轮不过来（内搭件数不够）时，容量不许把试删焊死。
    # 真实数据上就是这个案子：他 7 件 T 恤里有 4 件标了"只能单穿"，
    # 12℃ 只剩 3 件能当内搭 → 7 天必然要重复穿，试删却因此 12 次全被判 False、
    # 一件都省不下来（"少带件"再次静默失效）。
    few = {i: _mk(i, "L1") for i in (1, 2, 3)}
    few |= {11: _mk(11, "L4"), 21: _mk(21, "L5")}
    ok(not plan._can_serve(few, [T] * 7, set(few)),
       "3 件内搭排 7 天：容量不够 → 拦住（7 天至少要 4 件）")
    ok(plan._can_serve(few, [T] * 7, set(few), base_ok=[True] * 7, cap_gate=False),
       "但原始清单本身就轮不过来时，容量不再当闸（否则裤子鞋子也一件都省不下）")

    # 回归：带一天排不出来的日子，试删不能整体失效
    by6 = by_id_map([1, 2, 3, 4], [11, 12], [21, 22])
    mixed = [T] * 6 + [45.0]
    keptm, droppedm = plan.shrink(by6, mixed, set(by6))
    say(f"     6 个能排的天 + 1 个排不出来的天 → 从 {len(by6)} 件省到 "
        f"{len(keptm)} 件（删 {sorted(droppedm)}）")
    ok(len(droppedm) >= 1,
       f"照样省得下件（省了 {len(droppedm)} 件）—— 以前这里会被卡成 0 件")

    ok(plan.shrink({}, [], set()) == (set(), []), "空清单不崩")


def test_plan_engine() -> None:
    head("② 排布引擎（跑真实衣橱）")

    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
        n_before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()

    if len(items) < 6:
        say(f"在库单品只有 {len(items)} 件，跳过排布测试（先录几件）")
        return

    closet = graph.Closet(items)
    by_id = {int(it["id"]): it for it in items}
    weights = {"item": {}, "pair": {}}
    say(f"在库 {len(items)} 件")

    week_temps = [22, 21, 19, 16, 15, 18, 23]
    days = [{"date": f"2026-10-{10 + i:02d}", "temp": t}
            for i, t in enumerate(week_temps)]

    say()
    say("· 一周：7 天，温度 15~23℃")
    wk = plan.build_plan(closet, days, occasion="通勤", weights=weights,
                         mode="week", seed=3, use_llm=False)
    ok(wk["ok"] and wk["n_days"] == 7, f"排出 {wk.get('n_days')} 天")
    ok(wk["n_planned"] >= 5, f"实排 {wk['n_planned']} 天（有温度缺口可以少，但不该是 0）")

    for d in wk["days"]:
        o = d["outfit"]
        if not o:
            say(f"     {d['date']} [{d['temp']}℃] 排不出来 —— 已给 warning")
            continue
        ids = [int(x["id"]) for x in o["items"]]
        names = " ＋ ".join(x["name"] for x in o["items"])
        say(f"     {d['weekday']} [{d['temp']}℃] {names}")
        err = rules.outfit_ok(o["items"], d["temp"], "通勤")
        ok(err is None, f"{d['weekday']} 这套是合法的（{err or '通过硬规则'}）")
        ok(len(set(ids)) == len(ids), f"{d['weekday']} 没有重复单品")
        ok(all(i in by_id for i in ids), f"{d['weekday']} 每件都真的在衣橱里")

    cores = [graph.core_of(d["outfit"]["items"]) for d in wk["days"] if d["outfit"]]
    wear, looks = _wear_of(wk["days"])
    say(f"     排出现 {len(looks)} 套：上衣不重样 {len(set(looks))} 天 / "
        f"不同核心 {len(set(cores))} 个")
    ok(len(set(looks)) == len(looks),
       "7 天里没有两天的上衣栈是同一套（换上衣才算换了穿搭）")

    say("· 贴身层的穿着规矩（用户原话：短袖基本不会连续穿两天）")
    cap = plan.WEAR_CAP["L1"]
    over = {i: ds for i, ds in wear.items() if len(ds) > cap}
    ok(not over, f"没有一件内搭超过 {cap} 天（超出：{over or '无'}）")
    adj = {i: ds for i, ds in wear.items()
           if any(b - a == 1 for a, b in zip(ds, ds[1:]))}
    ok(not adj, f"没有一件内搭连穿两天（连穿：{adj or '无'}）")
    say(f"     这 7 天用了 {len(wear)} 件内搭（{wk['n_planned']} 天至少要 "
        f"{plan._l1_need(wk['n_planned'])} 件）")

    say()
    say("· 同一批日子换成旅行模式：应该更省件数")
    tr = plan.build_plan(closet, days, occasion="通勤", weights=weights,
                         mode="trip", seed=3, use_llm=False)
    say(f"     一周模式用到 {wk['n_items']} 件；旅行模式用到 {tr['n_items']} 件"
        f"（试删掉 {tr['dropped_count']} 件）")
    ok(tr["n_items"] <= wk["n_items"],
       f"旅行的打包清单不该比在家的更多（{tr['n_items']} ≤ {wk['n_items']}）")
    ok(tr["n_planned"] == wk["n_planned"] or tr["n_planned"] >= wk["n_planned"] - 1,
       "精简之后每天依然有得穿")

    say("· 旅行每天穿什么")
    for d in tr["days"]:
        o = d["outfit"]
        say(f"       {d['weekday']} [{d['temp']}℃] " + (" ＋ ".join(
            x["name"] for x in o["items"]) if o else "排不出来"))

    say("· 旅行不许再出现「7 天只穿一件 T 恤」")
    tr_l1 = [r for r in tr["packing"] if r["slot"] == "L1"]
    tr_wear, tr_looks = _wear_of(tr["days"])
    need = plan._l1_need(tr["n_planned"])
    say("     清单：" + " / ".join(
        f"{g['name']} {len(g['items'])} 件" for g in tr["packing_groups"]))
    say(f"     内搭 {len(tr_l1)} 件（至少需要 {need} 件）；"
        f"{len(tr_looks)} 天里上衣不重样 {len(set(tr_looks))} 天")
    ok(len(tr_l1) >= need,
       f"内搭够轮换（{len(tr_l1)} ≥ {need}）—— 一件最多穿 {plan.WEAR_CAP['L1']} 天")
    ok(all(len(ds) <= plan.WEAR_CAP["L1"] for ds in tr_wear.values()),
       f"旅行清单里的内搭也守规矩（最多 {plan.WEAR_CAP['L1']} 天一件）")
    ok(len(set(tr_looks)) >= min(4, len(tr_looks)),
       f"旅行也尽量换上衣（{len(set(tr_looks))} 种 / {len(tr_looks)} 天）")

    # ⚠️ 这条钉的是"少带件静默失效"（2026-09-22）：
    # 试删的可行性判定拿了一份**自身就违规**的清单（贪心排出一件 T 恤穿 3 天），
    # 容量判定必然为 False → 试删一件都砍不掉 → 箱子比一周模式还大。
    # 用户看到的是"旅行要我带 7 条裤子 / 一天一条不重样"。
    ok(tr["dropped_count"] > 0,
       f"旅行必须真的砍得下件（砍掉 {tr['dropped_count']} 件）—— "
       f"一件都砍不掉就是「少带件」坏了，不是「这趟没得省」")

    say()
    say("· 早晚温差大的行程：可行性按**当天最冷**判，别把早上冻着")
    # 出门 20℃、早上 12℃：12℃ 落在 <14℃ 档，L2+L3 都是必需层。
    # 之前按出门温度判 → 试删会把中间层砍光，重排只能穿短袖出门。
    # ⚠️ 早晚改成 **14℃**：12℃ 已经低于这套衣橱的保暖上限
    #    （T恤1 + 卫衣4 + 皮衣4 = 9 → 只能穿到 22 − 9 = 13℃），
    #    拿 12℃ 当早上，整趟行程会一天都排不出来，测不到排布本身。
    swing = [{"date": f"2026-10-{10 + i:02d}", "temp": 20, "tmin": 14}
             for i in range(7)]
    tr2 = plan.build_plan(closet, swing, occasion="通勤", weights=weights,
                          mode="trip", seed=3, use_llm=False)
    slots2: dict[str, int] = {}
    for r in tr2["packing"]:
        slots2[r["slot"]] = slots2.get(r["slot"], 0) + 1
    say(f"     排 {tr2['n_planned']} 天，箱子 {tr2['n_items']} 件（试删 "
        f"{tr2['dropped_count']} 件）：" + " / ".join(
            f"{plan.SLOT_LABEL.get(s, s)} {n} 件" for s, n in sorted(slots2.items())))
    ok(tr2["n_planned"] == 7, f"20℃/早晚 14℃ 的 7 天全部排得出来（实际 {tr2['n_planned']} 天）")
    for s in ("L2", "L3"):
        ok(slots2.get(s, 0) >= 1,
           f"箱子里必须有 {plan.SLOT_LABEL.get(s, s)}（早上 14℃ 没这个出不了门）")
    ok(tr2["dropped_count"] > 0, f"这种行程也要能省件（省了 {tr2['dropped_count']} 件）")

    say()
    say("· 温度低时不许只排一件上装（用户原话：18 度怎么可能只穿短袖）")
    say("  ⚠️ 换到「26℃穿衣法则」之后，这条**不再是排布层的罚分，而是引擎的硬约束**：")
    say("     引擎在叶子上用 `stack_warm_ok` 就把「不够暖」的组合否掉了，")
    say("     排布根本拿不到那种候选。所以这里改成直接断言**引擎那条性质**。")
    comfort = rules.comfort_of()
    for t in (14.0, 16.0, 18.0):
        blk = plan.build_plan(closet, [{"date": f"2026-05-{i + 1:02d}", "temp": t}
                                       for i in range(7)],
                              weights=weights, seed=0, use_llm=False)
        got = [d for d in blk["days"] if d["outfit"]]
        n_tops = [sum(1 for x in d["outfit"]["items"] if x["slot"] in plan.TOP_LAYERS)
                  for d in got]

        def _short(d):
            tops = [x for x in d["outfit"]["items"] if x["slot"] in plan.TOP_LAYERS]
            return rules.stack_warm_ok(tops, t, comfort)

        cold = [d for d in got if not _short(d)]
        say(f"     {t:g}℃：排 {blk['n_planned']} 天，上装件数 {sorted(set(n_tops))}，"
            f"不够暖 {len(cold)} 天（舒适线 {comfort:g}）")
        if not got:
            # 排不出来是**允许**的（衣橱确实不够暖），但必须说得出为什么
            d = closet.diagnose(t)
            say(f"     （{t:g}℃ 排不出来 —— {d['kind']}：{d['hint']}）")
            ok(d["kind"] in ("missing_layer", "too_cold"),
               f"{t:g}℃ 空态给出了可解释的原因（不是「算错了」）")
            continue
        ok(not cold,
           f"{t:g}℃ 每天的上装都够暖（{t:g}℃ + 保暖 ≥ {comfort:g}℃）")

    say()
    say("· 提示里不许提清单里没有的外套（用户反馈：说要带外套，穿搭里一件都没有）")
    stale = ("内搭多带一件换洗比多带一件外套有用",
             "外套要选能随时脱、随手搭在胳膊上的那种",
             "带一件可叠穿的外套就够应付")
    for label, res in (("一周", wk), ("旅行", tr)):
        toppers = [r for r in res["packing"] if r["slot"] in ("L2", "L3")]
        hit = [t for t in res["tips"] for s in stale if s in t]
        ok(not hit, f"{label}的提示里没有那几句与清单脱节的旧文案（{hit or '无'}）")
        say(f"     {label}清单里的中间层/外层：{len(toppers)} 件；"
            f"提示里提到外套的句子："
            f"{[t[:24] + '…' for t in res['tips'] if '外套' in t] or '无'}")

    say()
    say("· 打包清单必须自洽")
    covered = {int(r["id"]) for r in tr["packing"]}
    # 「备着」的必带件是刻意的例外：勾了必带、但那个温度穿不上 → 只进箱子不穿上
    backup = {int(i) for i in (tr.get("must_backup") or [])}
    used: set[int] = set()
    for d in tr["days"]:
        if d["outfit"]:
            used |= {int(x["id"]) for x in d["outfit"]["items"]}
    ok(used == covered - backup,
       f"清单里除「备着」的之外，和实际穿的完全对上"
       f"（清单 {len(covered)} 件 / 清单里穿过的 {len(covered) - len(backup)} 件 / "
       f"实际用到 {len(used)} 件）")
    zero = [r for r in tr["packing"] if r["wear_count"] == 0]
    ok(all(int(r["id"]) in backup for r in zero),
       f"清单里「0 天」的只可能是必带的备着件（实际 {[r['name'] for r in zero]}）")
    bad = [r for r in tr["packing"]
           if r["wear_count"] != len(r["days"]) or r["wear_count"] < 0]
    ok(not bad, f"每件的 wear_count 和日期列表一致（异常 {len(bad)} 件）")
    ok(all(r["slot"] in plan.SLOT_LABEL for r in tr["packing"]), "每件都有合法槽位")

    # ⚠️ 中间层**不许带超额**。原来 `_l2_need` 的 cap 是 3 天，10 天就要 4 件，
    #    用户反馈"矫枉过正、带得太多了"（原话：10 天最多带两件外套、3 条裤子）。
    #    2026-09-28 把 cap 放到 4 天 → 10 天 3 件。这条钉住它别长回去。
    n_l2 = len([r for r in tr["packing"] if r["slot"] == "L2"])
    cap_l2 = plan._l2_need(tr["n_days"])
    ok(n_l2 <= cap_l2,
       f"中间层不带超额：{n_l2} 件 ≤ {cap_l2} 件（{tr['n_days']} 天，一件最多穿 "
       f"{plan.L2_WEAR_CAP} 天）")
    flat = [r["id"] for g in tr["packing_groups"] for r in g["items"]]
    ok(sorted(flat) == sorted(r["id"] for r in tr["packing"]),
       "分组视图和平铺清单是同一批件，一件不多一件不少")

    say("· 试删掉的件，是真的能不带的（复验一遍）")
    if tr["dropped_count"] and tr["n_planned"] == len(tr["days"]):
        # 判可行性用**当天最冷那会儿**，和试删内部一个口径（`shrink` 里那句
        # `judge = warms or temps`）。用出门温度复验会放水。
        judge = [plan._warm_of(d) for d in tr["days"]]
        ok(plan._can_serve(by_id, judge, covered, "通勤"),
           f"只带清单里这 {len(covered)} 件，{len(judge)} 天依然每天有得穿、"
           f"内搭轮得过来")
    elif tr["dropped_count"]:
        say(f"     （有 {len(tr['days']) - tr['n_planned']} 天本来就排不出来，跳过整段复验）")
    else:
        say("     （这一趟没有可省的件，跳过）")

    say()
    say("· 极端情况不能崩")
    cold = plan.build_plan(closet, [{"date": "2026-01-05", "temp": -8}],
                           weights=weights, seed=1, use_llm=False)
    ok(cold["ok"], "-8℃ 单天：返回结构正常（哪怕排不出来）")
    if cold["n_planned"] == 0:
        ok(bool(cold["warnings"]), f"排不出来时给出了原因：{cold['warnings'][:1]}")

    same = plan.build_plan(closet, [{"date": f"2026-05-{i + 1:02d}", "temp": 20}
                                    for i in range(7)],
                           weights=weights, seed=5, use_llm=False)
    s_wear, s_looks = _wear_of(same["days"])
    ok(same["n_planned"] >= 5, f"连续 7 天都是 20℃：排出 {same['n_planned']} 天")
    ok(len(set(s_looks)) >= min(5, len(s_looks)),
       f"全同温度下仍然换上衣（{len(set(s_looks))} 种上衣 / {len(s_looks)} 天）")
    # ⚠️ 20℃ 这一周的天花板不能写死 2 天。换到「26℃穿衣法则」之后，
    #    20℃ **需要叠一层**（T恤 1℃：20+1 < 舒适线 22），于是能当内搭的
    #    只剩"可以被盖住"的那几件 —— 真实衣橱里 7 件 T 恤有 4 件标了
    #    「只能单穿」，能当内搭的只有 3 件 → 7 天必然有人要穿 3 次。
    #    这是**真实约束**（用户自己标的标签），不是排布的错。
    #    所以天花板按"可当内搭的件数"算：⌈7 / n⌉。
    n_inner = sum(1 for it in closet.items
                  if it["slot"] == "L1" and rules.TAG_SOLO not in rules.item_tags(it))
    cap_week = max(plan.WEAR_CAP["L1"], -(-7 // max(1, n_inner)))
    ok(all(len(ds) <= cap_week for ds in s_wear.values()),
       f"20℃ 这一周里内搭最多穿 {cap_week} 天"
       f"（能当内搭的只有 {n_inner} 件，7÷{n_inner} 取整）"
       f"—— 实际 {sorted(len(ds) for ds in s_wear.values())}")

    ok(plan.build_plan(closet, [], weights=weights)["ok"] is False, "空日期返回 ok=False")

    say()
    say("· 内搭不够换时，要说清是「拿不出来」而不是「带得不够」（2026-09-28）")
    # 造一个极端但很常见的场景：内搭件数够多，但绝大多数标了「只能单穿」——
    # 14~18℃ 要叠层时它们一律穿不上，真正能当内搭的只剩 2 件。
    # 旧文案只会说"10 天得带够 5 件内搭"，等于让用户去找一件**他拿不出来**的衣服。
    import copy as _copy
    l1_n = 0
    synth: list[dict] = []
    for it in closet.items:
        d = _copy.deepcopy(it)
        if d.get("slot") == "L1":
            l1_n += 1
            d["tags"] = json.dumps([rules.TAG_SOLO]) if l1_n > 2 else None
        synth.append(d)
    ok(l1_n >= 3, f"真实衣橱里有 {l1_n} 件内搭，够造这个场景")
    sc = graph.Closet(synth)
    d10 = [{"date": f"2026-10-{i + 1:02d}", "temp": 18, "tmin": 14, "tmax": 22}
           for i in range(10)]
    r10 = plan.build_plan(sc, d10, weights=weights, mode=plan.MODE_TRIP,
                          seed=3, use_llm=False)
    ok(r10["n_planned"] >= 8, f"这个合成衣橱排出了 {r10['n_planned']}/10 天")
    said = [w for w in r10["warnings"] if "能当内搭的只有" in w]
    ok(bool(said),
       f"说的是「能当内搭的只有 N 件」而不是「你得带够 5 件」："
       f"{(said or r10['warnings'])[:1]}")

    # ⚠️⚠️ **防漂移 + 防漏报**。`REPEAT_WARN_MARK` 这一类是**给代码用的标记**，
    #  不是给人看的文案 —— "修好之后把旧警告撤掉""已有旧警告就别再补一条"
    #  都靠它去认自己那句话。文案一改而常量没同步，这些逻辑就**静默失效**：
    #  2026-09-28 实测常量写的是"得重复穿某件内搭"，而正文里从来没有这个串 ——
    #  于是同一件事在页面上说了两遍，还互相矛盾。
    #
    #  ⚠️ 2026-09-29 起，「重复穿」提示**按层拆成两条**（内搭一句、中间层一句），
    #  所以断言不再是"总数为 1"，而是三件事：
    #    ① 真重复穿了就必须给出原因（一条都没有 = 标记和正文脱节）
    #    ② **每层最多一句**（两句 = 没合并 / 兜底没被抑制）
    #    ③ **每句都归得到层** —— 有一条既不是内搭也不是中间层，就说明标记漂了
    #  第 ③ 条是这次新加的：合成一条时，`_repair_l1` 修好内搭会顺手把
    #  中间层那句一起吞掉 —— 那是一种**漏报**，用户永远看不到中间层要带几件。
    n_l1 = sum(1 for w in r10["warnings"] if plan._is_l1_repeat(w))
    n_l2 = sum(1 for w in r10["warnings"] if plan.L2_REPEAT_MARK in w)
    n_all = sum(1 for w in r10["warnings"] if plan.REPEAT_WARN_MARK in w)

    ok(n_all >= 1, "真的重复穿了就必须给出原因（一条都没有 = 标记和正文脱节了）")
    ok(n_l1 <= 1, f"「内搭轮不过来」最多说一句（实际 {n_l1} 句）")
    ok(n_l2 <= 1, f"「中间层得带够」最多说一句（实际 {n_l2} 句）")
    ok(n_all == n_l1 + n_l2,
       f"每句都归得到层（{n_all} = 内搭 {n_l1} + 中间层 {n_l2}）—— "
       f"有句子两层都不属于，说明标记和文案脱节了")

    say()
    say("· 不该写库")
    conn = db.get_conn()
    try:
        n_after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    ok(n_after == n_before, f"排计划不动 items 表（{n_before} → {n_after}）")


# ══════════════════════════════════════════════
# ③ 必带（must_ids）
# ══════════════════════════════════════════════

def test_must() -> None:
    """旅行的「必带」—— 用户勾了就一定带上。三条都要成立：

      ① 温度合适 → 真被排进穿搭（不是只躺在清单里）
      ② **试删一件都不许砍**（这类件常常正好"只出场一次"，按出场次数排在最前面）
      ③ 温度穿不上 → 照样进箱子，标「备着」

    第 ③ 条是这次最容易漏的：清单原来只收"排布里穿过的件"，
    于是"勾了但没排上"的那件会**无声消失** —— 用户明明勾了，清单里却没有。
    """
    head("③ 必带（must_ids）")
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()
    if len(items) < 6:
        say(f"在库只有 {len(items)} 件，跳过")
        return

    closet = graph.Closet(items)
    weights = {"item": {}, "pair": {}}
    cool = [{"date": f"2026-04-{10 + i:02d}", "temp": t, "tmin": t}
            for i, t in enumerate([15, 14, 16, 15, 17])]

    def run_it(must, days=None):
        return plan.build_plan(closet, days or cool, weights=weights, worn={},
                               mode="trip", seed=0, use_llm=False, must_ids=must)

    def tags_of(it) -> set:
        try:
            return set(json.loads(it.get("tags") or "[]"))
        except Exception:
            return set()

    # 选一件**这个温度下真能被排进去**的上装 —— 排除标了 `solo_only` 的，
    # 那种"只能单穿"的件在需要叠穿的天气里本来就排不进去（会进「备着」，
    # 那是正确行为，不是 bug；见下面 ③ 的说明）。
    top = None
    for it in items:
        if it["slot"] != "L1" or "solo_only" in tags_of(it):
            continue
        if closet.count_paths(14.0, None, must={"L1": it}, limit=1) > 0:
            top = it
            break
    if top:
        r = run_it([int(top["id"])])
        pack = {int(x["id"]) for x in r["packing"]}
        ok(int(top["id"]) in pack, f"必带的「{top['name']}」进了清单")
        ok(int(top["id"]) in set(r["must_used"]),
           f"而且排进了某天的穿搭（{r['must_used_names']}）")
        ok(int(top["id"]) not in set(r["must_backup"]), "不该出现在「备着」里")
    else:
        say("     （衣橱里没有能叠穿的 L1，跳过这一条）")

    # ② 试删对必带免疫：拿一件"勾了必须留、不勾可能被砍"的鞋来验
    shoes = [it for it in items if it["slot"] == "L5"]
    if len(shoes) >= 2:
        target = int(shoes[-1]["id"])
        free = run_it([])
        forced = run_it([target])
        ok(target in {int(x["id"]) for x in forced["packing"]},
           f"勾了必带的「{shoes[-1]['name']}」一定在清单里"
           f"（不勾时它在不在：{'在' if target in {int(x['id']) for x in free['packing']} else '不在'}）")
        ok(forced["n_planned"] >= free["n_planned"] - 1,
           f"多带这一件之后每天照样有得穿（{forced['n_planned']} vs {free['n_planned']}）")

    # ③ 温度穿不上的（羽绒服 -10~10 扔进 20℃ 的行程）→ 只进箱子，标「备着」
    cold = next((it for it in items
                 if it["slot"] == "L3" and (it.get("temp_max") or 99) <= 12), None)
    if cold:
        hot = [{"date": f"2026-06-{10 + i:02d}", "temp": 20, "tmin": 20}
               for i in range(4)]
        r2 = run_it([int(cold["id"])], hot)
        pack2 = {int(x["id"]) for x in r2["packing"]}
        ok(int(cold["id"]) in pack2,
           f"穿不上的必带件「{cold['name']}」照样进清单（不能无声消失）")
        ok(int(cold["id"]) in set(r2["must_backup"]),
           f"并且被标成「备着」（{r2['must_backup_names']}）")
        row = next((x for x in r2["packing"] if int(x["id"]) == int(cold["id"])), None)
        ok(row is not None and row.get("wear_count") == 0 and row.get("must_only"),
           "它在清单里是 wear_count=0 + must_only")
        ok(any("备着" in t or "照样放进箱子" in t for t in r2["tips"]),
           f"提示里说清了这件事：{[t for t in r2['tips'] if '必带' in t]}")

    # ④ 必带 ≠ 每天必穿（用户原话："这怎么选了一条裤子必选结果 7 天只穿一条裤子"）
    #    踩过的坑有两层，都得钉住：
    #    · 候选池里"命中必带"原来被当成排序第一关键字**无条件插队** —— 而一条裤子
    #      能跟每个上衣栈都搭，于是 24 个名额全被含它的套占满（实测 72/72 套），
    #      天天只能穿它，"必带"被理解成了"每天必须穿"。
    #    · 就算池子修好了，裤子自己也没有轮换动力：`_cost` 的下装惩罚是按
    #      "裤子+鞋"的**组合**算的，每天换双鞋就归零 —— 排出来是"上衣天天换、
    #      裤子一周不换"。现在按件平方罚，鞋同理（否则不换的东西只是从裤子挪到鞋）。
    pants = [it for it in items if it["slot"] == "L4"]
    if len(pants) >= 4:
        week = [{"date": f"2026-04-{10 + i:02d}", "temp": t, "tmin": t}
                for i, t in enumerate([22, 21, 19, 16, 15, 18, 23])]
        target = int(pants[0]["id"])
        rw = plan.build_plan(closet, week, weights=weights, worn={}, mode="week",
                             seed=0, use_llm=False, must_ids=[target])
        seq = [int(x["id"]) for d in rw["days"] if d["outfit"]
               for x in d["outfit"]["items"] if x["slot"] == "L4"]
        ok(seq.count(target) <= 3,
           f"必带的那条裤子只穿了 {seq.count(target)} 天（不许霸占整周）")
        ok(target in set(rw["must_used"]),
           f"但必带件确实排进穿搭了（{rw['must_used_names']}）")
        ok(len(set(seq)) >= 3,
           f"一周里换了 {len(set(seq))} 种裤子（只换上衣、裤子不动是不行的）")
        shoe_seq = [int(x["id"]) for d in rw["days"] if d["outfit"]
                    for x in d["outfit"]["items"] if x["slot"] == "L5"]
        n_shoes = len({int(it["id"]) for it in items if it["slot"] == "L5"})
        ok(len(set(shoe_seq)) >= min(2, n_shoes),
           f"一周里换了 {len(set(shoe_seq))} 双鞋（衣橱共 {n_shoes} 双）")
        # 不勾必带时也该轮换 —— 这条防的是"修必带时顺手把裤子轮换关掉"
        free = plan.build_plan(closet, week, weights=weights, worn={}, mode="week",
                              seed=0, use_llm=False, must_ids=[])
        seq2 = [int(x["id"]) for d in free["days"] if d["outfit"]
                for x in d["outfit"]["items"] if x["slot"] == "L4"]
        ok(len(set(seq2)) >= 3,
           f"不勾必带时也换了 {len(set(seq2))} 种裤子")
    else:
        say(f"     （衣橱里只有 {len(pants)} 条下装，跳过必带轮换这一条）")

    # ⑥ 标了「只能单穿」的必带件：需要叠穿的天气里它本来就排不进去
    #    （`rules.tag_conflict` 拦的，不是 bug）→ 进「备着」。
    #    **但绝不许无声消失** —— 这是第一版测试栽过的地方：
    #    我以为"没排进穿搭"坏了，其实是设计如此，只是清单里得有它。
    solo = next((it for it in items
                 if it["slot"] in ("L1", "L2") and "solo_only" in tags_of(it)), None)
    if solo:
        r4 = run_it([int(solo["id"])])
        pack4 = {int(x["id"]) for x in r4["packing"]}
        ok(int(solo["id"]) in pack4,
           f"标了只能单穿的「{solo['name']}」勾成必带后照样进清单"
           f"（能不能穿上是另一回事）")

    # ⑦ 前端传了不存在的 id（单品已删）→ 不当回事，也不能崩
    r3 = run_it([999999])
    ok(r3["ok"] and not r3["must_ids"], "不存在的 id 被过滤掉，排布照常")
    ok(run_it([])["ok"], "不勾任何必带时一切照旧")

    # ⑤ 一周模式也认这个参数（后端通用，前端只在旅行页显示）
    wk = plan.build_plan(closet, cool, weights=weights, worn={}, mode="week",
                         seed=0, use_llm=False,
                         must_ids=[int(top["id"])] if top else None)
    if top:
        ok(int(top["id"]) in {int(x["id"]) for x in wk["packing"]},
           "一周模式同样认「必带」")


# ══════════════════════════════════════════════
# ④ 旅行的分段目的地（两个旅游地）
# ══════════════════════════════════════════════

def test_segments() -> None:
    """「京都 3 天 + 大阪 3 天」= 一个箱子、两段天气。

    用户原话：「旅游那边如果我在两个不同的旅游地呢？」

    这里测的是**合并那一层**（`main._plan_weather_segments`）—— 全程打桩，不联网。

    ⚠️ **排布引擎一行都没改，这是刻意的。** `plan.build_plan` 只吃一条按日期排好的
    温度序列，"少带一件"本来就是按"**整趟行程共用一份清单**"算的（试删问的是
    "不带这件，这几天还排得出吗"）。所以两段天然合出一个箱子 —— 是必然，不是碰巧。
    城市只在"温度从哪来"这一步存在。

    要钉住的几件事：
      ① 两段各用各的天气，按日期首尾相接（不是一段铺满）
      ② 日期轴**显式给出去** —— 两段之间可能隔着几十天，调用方不能按"连续 N 天"推
      ③ 跨预报边界：一段在预报内、一段在预报外 → 报缺，但整段不失败
      ④ 同坐标只打一次网络；重叠的段不产生重复日期
    """
    head("④ 旅行分段目的地（离线，打桩）")

    from datetime import date, timedelta
    from app import main as app_main

    orig_search, orig_for = weather.search_city, weather.forecast_for
    calls: list = []

    KIX = {"city": "京都", "admin": "京都府", "country": "日本",
           "lat": 35.0, "lon": 135.75, "label": "京都 · 京都府 · 日本"}
    OSA = {"city": "大阪市", "admin": "大阪府", "country": "日本",
           "lat": 34.69, "lon": 135.5, "label": "大阪市 · 大阪府 · 日本"}

    def fake_search(q, count=8):
        calls.append(("search", q))
        if "京都" in q:
            return [KIX]
        if "大阪" in q:
            return [OSA]
        return []

    def mk_days(base: int, n: int = 16) -> list[dict]:
        """从 2026-10-01 起 n 天，温度按 base 递增 —— 方便一眼看出是哪一段的天气。"""
        out = []
        for i in range(n):
            d = (date(2026, 10, 1) + timedelta(days=i)).isoformat()
            out.append({"date": d, "tmin": base + i - 4, "tmax": base + i + 4,
                        "temp": base + i, "temp_am": base + i - 4,
                        "temp_pm": base + i + 4, "code": 0, "text": "晴",
                        "wet": False, "precip": 0, "wind": 3.0})
        return out

    def fake_for(lat, lon, days=16, timezone="auto", bias="avg", label=""):
        calls.append(("forecast_for", round(float(lat), 2), round(float(lon), 2)))
        # 纬度低的（大阪）给热一点 —— 好断言"两段各用各的天气"
        base = 26 if round(float(lat), 1) < 35.0 else 20
        return {"ok": True, "city": label, "label": label, "lat": lat, "lon": lon,
                "bias": bias, "cached": False, "stale": False, "reason": "",
                "days": mk_days(base), "day": None}

    weather.search_city, weather.forecast_for = fake_search, fake_for
    try:
        seg = app_main.PlanSegment

        say("· 两段拼成一条行程：前三天京都、后三天大阪")
        r = app_main._plan_weather_segments([
            seg(label=KIX["label"], lat=35.0, lon=135.75, start="2026-10-01", days=3),
            seg(label=OSA["label"], lat=34.69, lon=135.5, start="2026-10-04", days=3),
        ])
        ok(r["ok"] and not r["missing"], "两段天气都取到了 → 不报缺")
        ok(len(r["days"]) == 6, f"合出 6 天（实际 {len(r['days'])}）")
        ok(r["day_axis"] == [d["date"] for d in r["days"]],
           "日期轴和每天的顺序逐字一致（前端就靠它画手填温度那一屏）")
        ok([s["label"][:2] for s in r["segments"]] == ["京都", "大阪"],
           f"两段的标签按顺序保留（{[s['label'][:3] for s in r['segments']]}）")
        ok(all(d.get("seg") for d in r["days"]), "每一天都标了自己属于哪一段")
        ok(r["days"][0]["seg"] != r["days"][-1]["seg"],
           "前三天在京都、后三天在大阪（不是一段铺满整趟）")
        t = [d["temp"] for d in r["days"]]
        say(f"     京都那三天 {t[:3]}℃ · 大阪那三天 {t[3:]}℃")
        ok(t[3] > t[2], "两段各用各的天气（大阪那段比京都热，正是打的那份桩）")

        say("· 同坐标只打一次网络")
        calls.clear()
        app_main._plan_weather_segments([
            seg(lat=35.0, lon=135.75, start="2026-10-01", days=2),
            seg(lat=35.0, lon=135.75, start="2026-10-03", days=2),
        ])
        n_for = sum(1 for c in calls if c[0] == "forecast_for")
        ok(n_for == 1, f"同一座城市被拆成两段时只查一次（实际 {n_for} 次）")
        calls.clear()

        say("· 只给城市名、不给坐标（接口层兜底；前端总是给坐标）")
        r3 = app_main._plan_weather_segments([
            seg(city="京都", start="2026-10-01", days=2),
            seg(city="大阪市", start="2026-10-03", days=2),
        ])
        ok(r3["ok"] and len(r3["days"]) == 4, f"只给城市名也能拼出 4 天（{len(r3['days'])}）")
        ok(r3["segments"][1]["label"].startswith("大阪"),
           f"搜出来的标签用完整地名（{r3['segments'][1]['label']}）")

        say("· 第二段没给开始日 → 自动接在第一段之后")
        r4 = app_main._plan_weather_segments([
            seg(lat=35.0, lon=135.75, start="2026-10-01", days=3),
            seg(lat=34.69, lon=135.5, days=2),
        ])
        ok(r4["day_axis"][3] == "2026-10-04",
           f"接得上（第 4 天 = {r4['day_axis'][3]}，期望 2026-10-04）")

        say("· ⚠️ 跨预报边界：一段在预报内、一段在预报外")
        say("   （这是分段最容易出错的地方 —— 两段之间隔着几十天，")
        say("     调用方按「从某天起连续 N 天」推出来的日期是错的）")
        r5 = app_main._plan_weather_segments([
            seg(label=KIX["label"], lat=35.0, lon=135.75, start="2026-10-01", days=3),
            seg(label=OSA["label"], lat=34.69, lon=135.5, start="2026-11-20", days=3),
        ])
        ok(r5["ok"] and r5["missing"], "第二段在预报之外 → 报缺，但整段不失败")
        ok(len(r5["day_axis"]) == 6,
           f"日期轴仍然给出全部 6 个日子（实际 {len(r5['day_axis'])}）")
        ok(r5["day_axis"][3] == "2026-11-20",
           f"日期轴里第二段是 11-20（实际 {r5['day_axis'][3]}）—— 不许推成 10-04")
        ok(len(r5["days"]) == 3, f"只把查得到的 3 天给出去当预填（{len(r5['days'])}）")
        ok("大阪" in r5["reason"], f"说清是哪一段没预报：{r5['reason'][:44]}")
        ok(r5["segments"][1]["got"] == 0 and r5["segments"][0]["got"] == 3,
           "每段自己报出「查到几天 / 要几天」（前端靠它说「有几天先替你填好了」）")
        say(f"     day_axis = {r5['day_axis']}")

        say("· 连 label 都不给时用「第 N 段」兜底（不编一个假地名）")
        r5b = app_main._plan_weather_segments([
            seg(lat=35.0, lon=135.75, start="2026-10-01", days=1)])
        ok(r5b["segments"][0]["label"] == "第 1 段",
           f"没 label 就用序号兜底（实际「{r5b['segments'][0]['label']}」）")

        say("· 重叠的段不产生重复日期（重复日期会让排布排出重复的一天）")
        r6 = app_main._plan_weather_segments([
            seg(lat=35.0, lon=135.75, start="2026-10-01", days=3),
            seg(lat=34.69, lon=135.5, start="2026-10-02", days=3),
        ])
        ok(len(r6["day_axis"]) == len(set(r6["day_axis"])),
           f"去重了（{len(r6['day_axis'])} 天，全不重复）")

        say("· 搜不到城市 / 一段都没有")
        r7 = app_main._plan_weather_segments([seg(city="不存在的地方xyz", days=2)])
        ok(r7["ok"] is False and "没搜到" in (r7["reason"] or ""),
           f"搜不到 → ok=False + 说清楚：{r7['reason']}")
        ok(app_main._plan_weather_segments([])["ok"] is True, "一段都没有时不崩")

        say("· 排布引擎只**透传**段落标签，不参与任何判定")
        conn = db.get_conn()
        try:
            items = db.load_items(conn, "owned")
        finally:
            conn.close()
        if len(items) >= 6:
            closet = graph.Closet(items)
            days = [{"date": f"2026-10-0{i + 1}", "temp": tp, "seg": sg}
                    for i, (tp, sg) in enumerate(
                        [(20, "京都"), (20, "京都"), (26, "大阪"), (26, "大阪")])]
            bp = plan.build_plan(closet, days, weights={"item": {}, "pair": {}},
                                 mode="trip", seed=0, use_llm=False)
            ok([d.get("seg") for d in bp["days"]] == ["京都", "京都", "大阪", "大阪"],
               "排出来的每一天都带着自己那一段的名字")
            ok(any("箱子只有一份" in x for x in bp["tips"]),
               f"提示里说清了「两段共用一个箱子」："
               f"{[x[:30] for x in bp['tips'] if '箱子' in x]}")
        else:
            say(f"     （在库只有 {len(items)} 件，跳过排布透传这一条）")

        say("· PlanIn 认这个字段（字段名/类型对上，不是只写在文档里）")
        pi = app_main.PlanIn(mode="trip", segments=[{"label": "京都", "days": 3}])
        ok(pi.segments and pi.segments[0].days == 3, "PlanIn.segments 接住了")
        ok(app_main.PlanSegment().days == 3, "不写天数时默认 3 天")
        src = __import__("inspect").getsource(app_main.api_plan)
        ok("_plan_weather_segments" in src, "api_plan 真的调了分段合并（不是定义了没人用）")
        ok("day_axis" in src, "缺预报那一支把 day_axis 给出去了（不然后端白算）")
    finally:
        weather.search_city, weather.forecast_for = orig_search, orig_for


# ══════════════════════════════════════════════
# ⑤ 交叉检查：新代码里引用的符号真的存在吗
# ══════════════════════════════════════════════

def test_symbols() -> None:
    head("⑤ 交叉检查（这个项目最爱栽的地方）")

    say("· main.py 引用的每个 `模块.符号` 都存在，且模块已 import")
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src, "main.py")

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            for a in node.names:
                imported.add(a.asname or a.name)
    mods = {"catalog": "app.catalog", "colorlab": "app.colorlab", "db": "app.db",
            "dupes": "app.dupes", "imgs": "app.imgs", "outfits": "app.outfits",
            "plan": "app.plan", "vision": "app.vision", "weather": "app.weather"}
    for name in ("plan", "weather"):
        ok(name in imported, f"main.py 已 import {name}"
                             f"（漏了它 → /api/{name} 一调用就 500）")

    import importlib
    missing = []
    for name, modpath in mods.items():
        mod = importlib.import_module(modpath)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == name):
                if not hasattr(mod, node.attr):
                    missing.append(f"{name}.{node.attr}")
    ok(not missing, f"main.py 里被引用的符号全部存在（缺：{missing or '无'}）")

    say("· 前端页面引的接口，后端真的有")
    routes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and isinstance(d.func.value, ast.Name)
                        and d.func.value.id == "app"):
                    if d.args and isinstance(d.args[0], ast.Constant):
                        routes.add(str(d.args[0].value))
    for r in ("/api/weather", "/api/weather/search", "/api/weather/config", "/api/plan"):
        ok(r in routes, f"后端有 {r}")

    say("· plan 模块内部的函数调用就位（改名没改调用方就靠这条抓）")
    import inspect
    psrc = (ROOT / "app" / "plan.py").read_text(encoding="utf-8")
    ptree = ast.parse(psrc, "plan.py")
    defined = {n.name for n in ast.walk(ptree) if isinstance(n, ast.FunctionDef)}
    called = {n.func.id for n in ast.walk(ptree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id.startswith("_")}
    ghost = sorted(called - defined)
    ok(not ghost, f"plan.py 里没有「调用了但不存在」的内部函数（幽灵：{ghost or '无'}）")

    say("· 参数名对上：build_plan 的签名和 main.py 的调用一致")
    sig = set(inspect.signature(plan.build_plan).parameters)
    for k in ("closet", "days", "occasion", "weights", "worn", "mode", "seed",
              "use_llm", "must_ids"):
        ok(k in sig, f"build_plan 接受 {k}")

    say("· 环境页不会因为天气挂了而打不开")
    wsrc = (ROOT / "app" / "weather.py").read_text(encoding="utf-8")
    ok("except Exception as exc" in wsrc and "return {\"ok\": False" in wsrc,
       "weather.forecast 把网络异常转成 ok=False，不外抛")
    wtree = ast.parse(wsrc, "weather.py")
    for n in ast.walk(wtree):
        if isinstance(n, ast.FunctionDef) and n.name == "forecast":
            raises = [x for x in ast.walk(n) if isinstance(x, ast.Raise)]
            ok(not raises, "forecast 里一条 raise 都没有（首页不能因为天气炸掉）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()

    say("胶囊衣橱 · 天气 + 多天排布自检")
    say("（全程离线：网络调用全部打桩，不需要 API Key）")

    test_weather()
    check_midnight()
    test_shrink()
    test_plan_engine()
    test_must()
    test_segments()
    test_symbols()

    head("结论")
    if FAILS:
        say(f"✗ {len(FAILS)} 项没过：")
        for f in FAILS:
            say("   - " + f)
    else:
        say("✓ 全部通过")

    text = "\n".join(L)
    if args.out:
        (ROOT / args.out).write_text(text, encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
