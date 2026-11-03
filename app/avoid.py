"""今天不想看到的件：「不喜欢」→ 立刻不出现。

════════ 为什么和 laundry 分开 ════════
`洗了` 是**物理状态**：晾干就回来，所以它挂在洗衣篮那张卡上、有「全部恢复」。
`不喜欢` 是**评价**：今天别给我看，但衣服本身没问题。

两者都会缩小候选（都是"减号"），但**理由不同、生命周期不同**：
  · 洗完衣服点「全部恢复」不该把"不喜欢"也一起清掉
  · 反过来，明天打开不该还记着昨天那句"不喜欢"
所以各存各的，消费时在 `/api/today` 里取交集。

⚠️ **按天自动作废**：文件里记 `day`，读的时候不是今天就当空集。
   不加这一条的话，一次"不喜欢"会永久把某件从今日推荐里抹掉，
   而界面上只表现为"这件怎么再也不出现了" —— 典型静默。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "avoid.json"


def _today() -> str:
    return date.today().isoformat()


def _read() -> dict:
    """任何失败都退回空 —— 这个值挂在"打开首页"这条路径上。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(ids: list[int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"day": _today(), "ids": sorted(set(ids))},
                   ensure_ascii=False, indent=1), encoding="utf-8")


def ids() -> set[int]:
    """今天"不喜欢"的件。**跨天自动作废**（见文件头）。"""
    raw = _read()
    if raw.get("day") != _today():
        return set()
    out: set[int] = set()
    for v in (raw.get("ids") or []):
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def add(item_id: int) -> set[int]:
    cur = ids()
    cur.add(int(item_id))
    _write(list(cur))
    return cur


def remove(item_id: int) -> set[int]:
    cur = ids()
    cur.discard(int(item_id))
    _write(list(cur))
    return cur


def clear() -> None:
    _write([])
