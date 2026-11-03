"""洗衣篮：今天洗了的衣服，暂时不进搭配。

**为什么是"临时集合"而不是 items 上的一个字段**：洗了是**状态**，不是属性。
晾干就回来，明天它还是那件卫衣 —— 写进 items 会变成一笔"要记得改回来"的数据债，
而且会跟着单品进导出、进体检、进缓存指纹（`_FP_FIELDS`）。
所以单独放一个集合里，语义就是"这几件今天不参与"。

**为什么用 JSON 不用 db 表**：它不是关系数据、没有查询需求，
"恢复"就是清空整个集合。跟 `prefs.json` / `weather.json` 一个路子，
`weather.py` 的读写就是这里的模板。

**它只管今日推荐，不碰计划页。** 计划页（一周 / 旅行）要按"你衣柜里有什么"排 ——
它回答的是"出门那几天这件在不在手边"，而不是"今天这件能不能穿"。
所以剔除只发生在 `/api/today` 这一条路径上。

⚠️ 里面存的是 **id**，单品删掉之后 id 会残留在 json 里。
消费方必须拿它跟当前衣橱取**交集**（`laundry.ids() & {it["id"] ...}`），
**不要**拿它直接去查库 —— 一个已删的 id 足够让整屏炸掉（踩过：979 行孤儿引用）。
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "laundry.json"


def ids() -> set[int]:
    """当前在洗衣篮里的单品 id。

    **任何失败都当作空集** —— 这个值挂在"打开首页"这条路径上，
    一个被改坏的 json 不该让首页打不开（同 `prefs.load`）。
    """
    out: set[int] = set()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for v in raw.get("ids") or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue          # 单个坏值跳过就行，不要整个文件作废
    return out


def wash(item_id: int) -> set[int]:
    """把这件丢进洗衣篮。重复洗同一件是幂等的。"""
    s = ids()
    s.add(int(item_id))
    return _write(s)


def restore(item_id: int) -> set[int]:
    """把这一件放回。放回一件不在篮子里的，也是幂等的。"""
    s = ids()
    s.discard(int(item_id))
    return _write(s)


def clear() -> set[int]:
    """全部放回。界面上那个「恢复」就是它。"""
    return _write(set())


def _write(s: set[int]) -> set[int]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 排序后再写：这个文件人眼要能直接看懂，也便于出问题时 diff。
    CONFIG_PATH.write_text(
        json.dumps({"ids": sorted(int(x) for x in s)}, ensure_ascii=False),
        encoding="utf-8")
    return s
