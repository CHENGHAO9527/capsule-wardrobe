"""穿完之后的体感反馈：偏冷 / 偏热。

════════ 为什么调的是「舒适线」，不是某件衣服的保暖值 ════════

引擎的保暖模型只有一句话：`气温 + Σ上装保暖 ≥ 舒适线`（见 prefs.py）。
"这套太冷" 可能来自两个地方，**而单凭一次反馈分不开它们**：

  ① **舒适线太低**（你整体比模型假设的更怕冷）→ 全局问题
  ② **某几件的保暖值估低了**（模型看图猜的）→ 局部问题

从"这一套冷"这一个观测点上，这两者是不可分辨的 —— 数学上叫不可辨识。
硬要一次就归因到某件衣服，等于用一次数据改一个永久参数，错一次就一直错。

所以本文件的做法是：

  · **主：动舒适线**（每人一格 ±0.5℃）。"整体偏冷/偏热"这句话本身就是在说
    这个刻度不对 —— 它是低维量的、一步就能被听懂、而且用户随时能在
    `/settings` 里看到同一个数、手动改回来。**同一个旋钮，不另开一个隐藏的偏移量。**
  · **副：只记录、不自动改单件**。哪几件反复出现在"偏冷"的套里，
    在体检页列出来当**线索**（"它的保暖值可能估低了"），由人决定改不改。
    证据攒够了再说话，比一次就写死安全得多。

⚠️ 舒适线是**引擎里唯一那个数**（`prefs.comfort()`）。改了它，
   所有温度口径的判定跟着变 —— 这是对的，别在别处再开第二个偏移量。
   `title` 里那句"改一处须同改五处"说的是**判定口径**，不是数值来源；
   数值来源只有一个：`prefs.comfort()`。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from . import prefs

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "feel.json"

# 每次反馈动多少度。0.5 是**故意小**的：
# 一次体感说明不了太多，改大了会来回打摆子；连点两次才 1℃，用户能感知到。
STEP = 0.5
# 留最近多少条 —— 只需要"最近这段时间"的线索，不需要全史（文件要能人眼看）
KEEP = 40
# 一件衣服至少出现在几次同类反馈里，才值得当线索提出来
FLAG_AT = 2


def _read() -> dict:
    """任何失败都退回空 —— 这个值挂在"打开首页"这条路径上。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def events() -> list[dict]:
    ev = _read().get("events")
    return ev if isinstance(ev, list) else []


def record(conn, verdict: str, outfit_id: int, temp: float,
           day: Optional[str] = None) -> dict:
    """记一次体感，并**把舒适线挪 0.5℃**。

    `verdict` 只接受 `cold` / `hot`。返回改动前后的舒适线，前端据此说
    "已把偏好改到更怕冷一点（22.0 → 22.5℃）" —— 不告诉用户改了什么，
    下次他还是不知道为什么搭配变了。
    """
    if verdict not in ("cold", "hot"):
        raise ValueError("verdict 只能是 cold 或 hot")
    ids = [int(r["item_id"]) for r in conn.execute(
        "SELECT item_id FROM outfit_items WHERE outfit_id = ?",
        (int(outfit_id),)).fetchall()]

    before = prefs.comfort()
    # 冷 → 需要更多保暖 → 舒适线抬高；热 → 反过来
    after = prefs.save({"comfort": before + (STEP if verdict == "cold" else -STEP)})["comfort"]

    data = _read()
    ev = list(events())
    ev.append({
        "date": day or date.today().isoformat(),
        "temp": round(float(temp), 1) if temp is not None else None,
        "ids": ids,
        "verdict": verdict,
        "comfort_before": before,
        "comfort_after": after,
    })
    data["events"] = ev[-KEEP:]
    _write(data)
    return {"verdict": verdict, "comfort_before": before, "comfort_after": after,
            "moved": round(after - before, 2), "ids": ids}


def reset_comfort(to: Optional[float] = None) -> float:
    """把舒适线放回默认值（或指定值）。学歪了要能一键回去。"""
    to = prefs.DEFAULT_PREFS["comfort"] if to is None else to
    return prefs.save({"comfort": to})["comfort"]


def garment_flags() -> dict[int, dict]:
    """哪几件反复出现在"偏冷"/"偏热"的搭配里。

    ⚠️ 这只是**线索**，不是判决 —— 所以调用方只许展示，不许自动改保暖值。
    """
    tally: dict[int, dict[str, int]] = {}
    for e in events():
        v = e.get("verdict")
        if v not in ("cold", "hot"):
            continue
        for i in (e.get("ids") or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            t = tally.setdefault(i, {"cold": 0, "hot": 0})
            t[v] += 1
    return {i: t for i, t in tally.items()
            if t["cold"] >= FLAG_AT or t["hot"] >= FLAG_AT}
