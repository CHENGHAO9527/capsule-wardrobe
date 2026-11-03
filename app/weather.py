# -*- coding: utf-8 -*-
"""天气：让「今日」自己知道外面多少度。

**为什么不问大模型。** 大模型的 API 不联网 —— 你问它"上海今天几度"，
它会给你一个语法通顺、数字随手编的答案，而且每次问都不一样。
查天气要直连天气服务，跟有没有接模型是两件不相干的事。

走 open-meteo：免费、不用注册、不用 Key、认识中文城市名。
顺带一个好处：它回的是 WMO 标准天气码（0/1/2/3/61…），
能稳定映射成中文，不像抓网页那样隔一阵子改版就崩。

三条设计约束（都是这个项目踩出来的）：
  1. **失败必须看得见。** 拉不到就说拉不到，返回 reason，绝不编一个温度顶上。
     （本项目已栽过 6 次"看起来生效了、其实是某个默认值"，不能再添第 7 次。）
  2. **缓存优先。** 预报半小时内不会变，缓存在 data/weather.json。
     断网时用旧缓存并在界面上标出来 —— 比一个空白屏有用得多。
  3. **预报温度要折算。** 预报给的是当天最高/最低，而穿衣要的是"我出门那会儿多少度"。
     默认取日平均，允许在 早 / 平均 / 午后 三档之间切。

为什么没有"远期天气估算"：
  预报只到 16 天（`FORECAST_DAYS`）。旅行常常排在一个月后，那会儿谁也查不到天气。
  本想用 open-meteo 的历史归档（取去年同一天）来估，但实测
  `archive-api.open-meteo.com` 在这台机器上回的是一个中间设备签的无效证书
  （DER 只有 914 字节、subject 为空，SSL 验证直接失败）—— 这条路不能依赖。
  所以远期一律**如实说"还没有预报"，把温度交给用户填**（见 main.api_plan 的
  need_temps 分支）。让人给一个大概温度，比让程序编一个准确得多。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "weather.json"

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 超时要短。这个调用挂在"打开首页"这条路径上，宁可拿不到也不能让首页转圈 15 秒。
TIMEOUT = 8
CACHE_TTL = 30 * 60          # 秒
# 一次取满这么多天。今日页只要 1 天，但不能因此只取 1 天 ——
# 计划和今日共用同一份缓存，取少了另一边就得重新联网。
FORECAST_DAYS = 16

DEFAULT_CONFIG = {
    "city": "",
    "admin": "",
    "country": "",
    "lat": None,
    "lon": None,
    "timezone": "auto",
    "bias": "avg",           # am | avg | pm
}

# WMO 天气码 → 中文。open-meteo 用的是这套，比自由文本稳定。
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "冻雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}

# 会下东西的码（用于「带把伞」这类提示）
WET_CODES = set(range(51, 68)) | set(range(71, 78)) | set(range(80, 87)) | {95, 96, 99}


def weather_text(code) -> str:
    try:
        return WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def is_wet(code) -> bool:
    try:
        return int(code) in WET_CODES
    except (TypeError, ValueError):
        return False


def dress_temp(tmin, tmax, bias: str = "avg"):
    """预报的日最高/最低 → 实际用来选衣服的那个温度。

    为什么不能固定用最高或最低：
      · 只用最高 → 早上 8℃ 出门穿成 22℃ 的样子，冻一天
      · 只用最低 → 中午 22℃ 穿着羽绒服
    默认取日平均。想更细就切档，三档写成最简单的三个数，
    **不搞"四分之一位"那种解释不清的取值** —— 说不清楚的东西没法让人信：

      · am  早（当天最低温那个时段）
      · avg 平均（默认）
      · pm  午后（当天最高温那个时段）
    """
    if tmin is None and tmax is None:
        return None
    if tmin is None:
        return int(round(float(tmax)))
    if tmax is None:
        return int(round(float(tmin)))
    lo, hi = float(tmin), float(tmax)
    if bias == "am":
        return int(round(lo))
    if bias == "pm":
        return int(round(hi))
    return int(round((lo + hi) / 2))


# ══════════════════════════════════════════════
# 存取
# ══════════════════════════════════════════════

def _read() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read().get("config") or {})
    return cfg


def save_config(patch: dict) -> dict:
    """存城市。只给了城市名没给坐标时，自动补查一次 —— 让调用方少一步。

    改了取温档（早/平均/午后）时顺手把缓存里那几天重算一遍 ——
    折算值是从 tmin/tmax 推出来的，没必要为了换个档再联网一次。
    """
    data = _read()
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data.get("config") or {})

    for k in ("city", "admin", "country", "lat", "lon", "timezone", "bias"):
        if k in patch and patch[k] is not None:
            cfg[k] = patch[k]

    if patch.get("city") and patch.get("lat") is None and patch.get("lon") is None:
        hits = search_city(str(patch["city"]))
        if hits:
            h = hits[0]
            cfg.update({k: h[k] for k in ("city", "admin", "country", "lat",
                                          "lon", "timezone")})

    data["config"] = cfg
    cache = data.get("cache") or {}
    if cache.get("days"):
        cache["days"] = apply_bias(cache["days"], cfg.get("bias") or "avg")
        cache["bias"] = cfg.get("bias") or "avg"
        data["cache"] = cache
    _write(data)
    return cfg


def city_label(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    bits = [cfg.get("city") or "", cfg.get("admin") or "", cfg.get("country") or ""]
    out, seen = [], set()
    for b in bits:
        b = (b or "").strip()
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return " · ".join(out)


# ══════════════════════════════════════════════
# 网络
# ══════════════════════════════════════════════

def _fetch_json(url: str, params: dict, timeout: int = TIMEOUT) -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{q}", headers={"User-Agent": "capsule-wardrobe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search_city(q: str, count: int = 8) -> list[dict]:
    """按名字搜城市。失败返回空列表，由调用方决定怎么提示 —— 不抛异常。"""
    q = (q or "").strip()
    if not q:
        return []
    try:
        data = _fetch_json(GEO_URL, {"name": q, "count": count,
                                     "language": "zh", "format": "json"})
    except Exception:
        return []

    out, seen = [], set()
    for r in (data.get("results") or []):
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is None or lon is None:
            continue
        key = (r.get("name"), r.get("admin1"), round(float(lat), 3))
        if key in seen:
            continue
        seen.add(key)
        name, admin, country = r.get("name") or "", r.get("admin1") or "", r.get("country") or ""
        out.append({
            "city": name, "admin": admin, "country": country,
            "lat": lat, "lon": lon,
            "timezone": r.get("timezone") or "auto",
            "label": " · ".join(x for x in (name, admin, country) if x),
        })
    return out


# ══════════════════════════════════════════════
# 预报
# ══════════════════════════════════════════════

def _num(seq, i):
    try:
        v = seq[i]
    except (IndexError, TypeError):
        return None
    return None if v is None else float(v)


def _int(seq, i):
    n = _num(seq, i)
    return None if n is None else int(round(n))


def _local_today(offset_seconds) -> str:
    """**目的地**那边现在是几号（返回 'YYYY-MM-DD'）。

    预报里的日期是按目的地时区排的（请求里 `timezone=auto`／配置里的
    Asia/Shanghai）。要判断"哪几天已经过去了"，就得拿**同一个时区**的"今天"，
    否则跨过零点会差一天。

    用 open-meteo 回的 `utc_offset_seconds` 反推，不依赖跑服务的这台机器的
    时区，也不用 tzdata（Windows 上不一定装）。
    """
    if offset_seconds is None:
        return date.today().isoformat()
    try:
        off = int(offset_seconds)
    except (TypeError, ValueError):
        return date.today().isoformat()
    return (datetime.now(timezone.utc) + timedelta(seconds=off)).date().isoformat()


def drop_past_days(days: list[dict], today_iso: str) -> list[dict]:
    """丢掉"今天之前"的那几天。

    ⚠️ 为什么必须有这一步（2026-09-28 00:16 实测撞到，不是推测）：
    缓存 TTL 是 30 分钟。23:59:15 取的那一份，`days[0]` 就是 **09-27**；
    过了零点它还在缓存有效期内，于是：
      · `/api/timeline` 第一行印的是**昨天**（周日），"今天"掉到第二行；
      · 7 天窗口被顶掉一天 —— 10-04 直接消失（本该是 09-28 ~ 10-04）；
      · **更严重的**：`_present` 把 `days[0]` 当作 `day`，
        于是**今日页拿昨天的气温去挑今天的衣服**。
    全程不报错、退出码 0、界面上一切正常。

    这是"缓存跨过零点"这一类 bug，**每晚 0:00 到缓存过期之间必然复发**，
    白天跑测试永远碰不到（写测试的那会儿是 23:47，同一天，所以全绿）。

    返回空的时候**把原样还回去**：TTL 只有 30 分钟，真出现"整份都是过去的"
    只可能是断网很久 + 旧缓存兜底那条路。那种情况下宁可给一份带 `stale=True`
    的旧数据（界面会明说），也不要返回空数组让整页白屏。
    """
    if not days:
        return days
    keep = [d for d in days if str(d.get("date") or "") >= today_iso]
    return keep or days


def parse_daily(raw: dict, bias: str = "avg") -> list[dict]:
    """open-meteo 的 daily 块 → 本项目的日期列表。纯函数，方便离线测。"""
    d = raw.get("daily") or {}
    dates = d.get("time") or []
    tmax = d.get("temperature_2m_max") or []
    tmin = d.get("temperature_2m_min") or []
    code = d.get("weather_code") or []
    prec = d.get("precipitation_probability_max") or []
    wind = d.get("wind_speed_10m_max") or []

    out = []
    for i, date in enumerate(dates):
        lo, hi = _num(tmin, i), _num(tmax, i)
        if lo is None and hi is None:
            continue          # 这一天的预报缺了，跳过比编一个 0℃ 强
        c = _int(code, i)
        out.append({
            "date": date,
            "tmin": None if lo is None else int(round(lo)),
            "tmax": None if hi is None else int(round(hi)),
            "temp": dress_temp(lo, hi, bias),
            "temp_am": dress_temp(lo, hi, "am"),
            "temp_pm": dress_temp(lo, hi, "pm"),
            "code": c,
            "text": weather_text(c),
            "wet": is_wet(c),
            "precip": _int(prec, i),
            "wind": _num(wind, i),
        })
    return out


def _present(cfg: dict, days: list[dict], cached: bool) -> dict:
    return {
        "ok": True,
        "city": cfg.get("city") or "",
        "admin": cfg.get("admin") or "",
        "country": cfg.get("country") or "",
        "label": city_label(cfg),
        "lat": cfg.get("lat"), "lon": cfg.get("lon"),
        "bias": cfg.get("bias") or "avg",
        "cached": cached,        # 命中缓存（正常）
        "stale": False,          # 拉新的失败了，回退到旧缓存（要提示）
        "reason": "",
        "days": days,
        # 「今日」永远单独给一份。前端读 wx.day 就够了，不用再关心 days 是不是刚好一天 ——
        # 少了它，今日页会拿到一个 ok=True 却没有 day 的返回，然后整块天气无声地不显示。
        "day": days[0] if days else None,
    }


def apply_bias(days: list[dict], bias: str) -> list[dict]:
    """按 早/平均/午后 重算穿衣温度。

    折算值是从 tmin/tmax 推出来的，所以换档不用重新联网。
    """
    out = []
    for d in days:
        d = dict(d)
        d["temp"] = dress_temp(d.get("tmin"), d.get("tmax"), bias)
        d["temp_am"] = dress_temp(d.get("tmin"), d.get("tmax"), "am")
        d["temp_pm"] = dress_temp(d.get("tmin"), d.get("tmax"), "pm")
        out.append(d)
    return out


def forecast_for(lat: float, lon: float, days: int = 16,
                 timezone: str = "auto", bias: str = "avg",
                 label: str = "") -> dict:
    """查**任意一点**的预报，不动配置、不写缓存。

    旅行要的是一个不是你家的地方 —— 不能为了看一眼目的地天气就把居住城市改掉。
    """
    try:
        days = max(1, min(16, int(days)))
    except (TypeError, ValueError):
        days = 16
    if lat is None or lon is None:
        return {"ok": False, "days": [], "day": None, "reason": "缺少目的地坐标。"}
    try:
        raw = _fetch_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,wind_speed_10m_max",
            "timezone": timezone or "auto",
            "forecast_days": days,
        })
    except Exception as exc:
        return {"ok": False, "days": [], "day": None,
                "reason": f"取不到目的地的天气：{exc}"}

    parsed = parse_daily(raw, bias)
    if not parsed:
        return {"ok": False, "days": [], "day": None, "reason": "目的地那几天还没有预报。"}
    parsed = drop_past_days(parsed, _local_today(raw.get("utc_offset_seconds")))
    return {"ok": True, "city": label, "admin": "", "country": "",
            "label": label, "lat": lat, "lon": lon, "bias": bias,
            "cached": False, "stale": False, "reason": "",
            "days": parsed, "day": parsed[0] if parsed else None}


def forecast(days: int = 14, force: bool = False) -> dict:
    """取预报。永远不抛异常。

    返回里一定要能分辨这几种情况：
      · ok=True, stale=False          → 正常（可能是缓存）
      · ok=True, stale=True           → 网断了，给的是旧缓存，界面要标出来
      · ok=False, need_city=True      → 还没设置城市
      · ok=False, 其他 reason         → 真拿不到

    **一次取满 `FORECAST_DAYS` 天共用缓存。** 今日页要 1 天、计划页要 16 天，
    按请求天数去取的话，来回切页面就会打两次网络（还会互相把缓存顶掉）。
    """
    try:
        days = max(1, min(FORECAST_DAYS, int(days)))
    except (TypeError, ValueError):
        days = 14

    cfg = load_config()
    if not cfg.get("city"):
        return {"ok": False, "need_city": True, "days": [], "day": None,
                "reason": "还没设置城市 —— 设一次之后，「今日」就自己知道今天多少度了。"}
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if lat is None or lon is None:
        return {"ok": False, "need_city": True, "days": [], "day": None,
                "reason": "这个城市没有坐标，重新选一次。"}

    cache = _read().get("cache") or {}
    same_place = (cache.get("lat") == lat and cache.get("lon") == lon)
    cached_days = list(cache.get("days") or [])
    # ⚠️ `cache.get("today") == 当前目的地日期` 这一条不能省：
    # 30 分钟的 TTL 足以跨过零点。不加它，23:59 那份缓存到 00:29 都算"新鲜"，
    # 于是 `days[0]` 是昨天、`day` 也是昨天（见 drop_past_days 的说明）。
    # 日期一对不上就当缓存失效 → 重新拉一次，而不是悄悄发一份错的。
    cache_today = _local_today(cache.get("utc_offset_seconds"))
    # 这份缓存的日期轴是**从哪天起**排的。新写的缓存直接带 `today`；
    # 老格式（改动之前写的）没有这个字段，就退而用它的第一天当锚点 ——
    # 缓存永远是"写入那天"从当天起排的，所以这个推断是成立的。
    cache_anchor = cache.get("today") or (cached_days[0].get("date") if cached_days else None)
    fresh = (same_place and cache.get("bias") == (cfg.get("bias") or "avg")
             and len(cached_days) >= days
             and cache_anchor == cache_today
             and (time.time() - float(cache.get("at") or 0)) < CACHE_TTL)
    if fresh and not force:
        return _present(cfg, cached_days[:days], cached=True)

    try:
        raw = _fetch_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,wind_speed_10m_max",
            "timezone": cfg.get("timezone") or "auto",
            "forecast_days": FORECAST_DAYS,
        })
    except Exception as exc:
        if same_place and cached_days:
            # 断网兜底：旧缓存也**先把过去的那几天剔掉**，别把昨天当今天。
            res = _present(cfg, drop_past_days(cached_days, cache_today)[:days], cached=True)
            res["stale"] = True
            res["reason"] = f"取最新预报失败，用的是上次的数据：{exc}"
            return res
        return {"ok": False, "days": [], "day": None,
                "reason": f"取不到天气：{exc}（检查一下网络，或换个城市）"}

    parsed = parse_daily(raw, cfg.get("bias") or "avg")
    if not parsed:
        return {"ok": False, "days": [], "day": None,
                "reason": "天气服务返回了空预报，稍后再试。"}

    api_today = _local_today(raw.get("utc_offset_seconds"))
    parsed = drop_past_days(parsed, api_today)
    data = _read()
    data["cache"] = {"lat": lat, "lon": lon, "at": time.time(),
                     "bias": cfg.get("bias") or "avg", "days": parsed,
                     # 把"这份缓存是哪个日期的"一起存下来 ——
                     # 这是上面那条 fresh 判断的依据，也是换成另一个时区的城市时
                     # 唯一能分辨"日期轴该不该整体平移"的线索。
                     "today": api_today,
                     "utc_offset_seconds": raw.get("utc_offset_seconds")}
    data["config"] = cfg
    _write(data)
    return _present(cfg, parsed[:days], cached=False)
