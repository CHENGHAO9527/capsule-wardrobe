"""随身清单（旅行模式）：出门那几天，只从"带出去的这几件"里挑穿搭。

**它解决的是一个具体的尴尬**：10 天旅行只带了一个箱子，可「今日」页认的是
**完整衣橱** —— 于是那几天打开首页，它推的全是留在家里的大衣。用户只能一件件
点「洗了」把没带的收起来，纯手工，而且每天都要重来一遍。

**为什么跟 `laundry` 分开、不直接复用它**：
  · 「洗了」是**今天**的状态，晾干就回来；「随身清单」是**这几天**的状态。
  · 两者的生命周期不同（一个是几小时，一个是整趟行程），塞进一个文件之后，
    用户洗完衣服点「全部恢复」会把旅行模式一起清掉。
  所以各存各的，消费时在 `/api/today` 里取交集 —— 都是"缩小候选"，方向一致。

**为什么存 json 不建表**：同 `laundry`，它不是关系数据、没有查询需求，
"退出旅行模式"就是清空。`weather.py` / `laundry.py` 的读写就是这里的模板。

⚠️ 里面存的是 **id**。单品删掉之后 id 会残留在 json 里 —— 消费方必须拿它跟
当前衣橱取**交集**（`kit.ids() & {it["id"] ...}`），**不要**直接拿去查库。
（跟 `laundry.py` 同一个坑：一个已删的 id 足够让整屏炸掉。）
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "kit.json"


def load() -> Optional[dict]:
    """当前随身清单。没开旅行模式时返回 None。

    **任何失败都当作没开** —— 这个值挂在"打开首页"这条路径上，
    一个被改坏的 json 不该让首页打不开（同 `prefs.load` / `laundry.ids`）。
    """
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("active"):
        return None
    ids: list[int] = []
    for v in raw.get("item_ids") or []:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue          # 单个坏值跳过就行，不要整个文件作废
    return {
        "active": True,
        "label": str(raw.get("label") or "旅行"),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "item_ids": ids,
        # 逐天温度（date → temp）。**只在今天落在行程范围内时**才有意义，
        # 用来把"出门那会儿"的温度从常住城市改成目的地 —— 否则人在外面、
        # 首页还在按老家的天气挑衣服，清单对了温度还是错的。
        "temps": raw.get("temps") or {},
    }


def ids() -> set[int]:
    k = load()
    return {int(x) for x in k["item_ids"]} if k else set()


def temp_for(day: Optional[str] = None) -> Optional[float]:
    """行程里那一天的温度。不在范围内 → None（调用方照常走天气）。

    ⚠️ 必须**显式**给日期，不要在里面偷偷读 `date.today()` —— 那样测试
    就依赖"跑测那天恰好在行程里"，是最典型的假绿。
    """
    k = load()
    if not k or not day:
        return None
    v = (k.get("temps") or {}).get(str(day))
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def set_kit(label: str, start: str, n_days: int,
            item_ids: list[int], temps: Optional[list[dict]] = None) -> dict:
    """开一趟旅行模式。`item_ids` 是这趟的打包清单。"""
    ids = []
    for v in item_ids or []:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    tmap: dict[str, float] = {}
    for row in (temps or []):
        if not isinstance(row, dict):
            continue
        d, t = row.get("date"), row.get("temp")
        if d is None or t is None:
            continue
        try:
            tmap[str(d)] = round(float(t), 1)
        except (TypeError, ValueError):
            continue
    end = start
    try:
        y, m, dd = (int(x) for x in str(start).split("-"))
        end = (date(y, m, dd) + timedelta(days=max(0, int(n_days) - 1))).isoformat()
    except (TypeError, ValueError):
        pass

    payload = {
        "active": True,
        "label": label or "旅行",
        "start": start,
        "end": end,
        "item_ids": sorted(set(ids)),
        "temps": tmap,
        "created_at": date.today().isoformat(),
    }
    _write(payload)
    return payload


def remove_item(item_id: int) -> Optional[dict]:
    """把一件从随身清单里拿掉（「不喜欢」时顺手做的）。

    返回 `{"label", "n_items"}`；没有清单 / 它本来就不在清单里 → None。
    **拿空了就整个关掉** —— 留一个 0 件的清单等于让首页卡在空态里出不来。
    """
    k = load()
    if not k:
        return None
    left = [int(i) for i in k["item_ids"] if int(i) != int(item_id)]
    if len(left) == len(k["item_ids"]):
        return None
    if not left:
        clear()
        return {"label": k["label"], "n_items": 0, "emptied": True}
    _write({"active": True, "label": k["label"], "start": k["start"], "end": k["end"],
            "item_ids": sorted(left), "temps": k["temps"],
            "created_at": date.today().isoformat()})
    return {"label": k["label"], "n_items": len(left), "emptied": False}


def clear() -> None:
    """退出旅行模式。"""
    _write({"active": False})


def _write(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 排序后再写：这个文件人眼要能直接看懂，也便于出问题时 diff。
    CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
