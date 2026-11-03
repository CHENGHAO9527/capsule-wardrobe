"""胶囊衣橱 · FastAPI 入口。

启动：python -m uvicorn app.main:app --host 0.0.0.0 --port 8077
手机与电脑同一 WiFi 时，用电脑局域网 IP + :8077 访问。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import (avoid, catalog, colorlab, db, dupes, feel, gate, imgs, kit, laundry,
               outfits, plan, prefs, sets, vision, weather)
from .catalog import (
    CATEGORIES, COLORS, COLLARS, FITS, LENGTHS, MATERIALS, PATTERNS,
    SLEEVES, SLOT_NAMES, STATUSES, STATUS_NAMES,
)
from .engine import graph, prebuy, rules

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

OCCASIONS = list(rules.OCCASION_FORMALITY.keys())


# ══════════════════════════════════════════════
# 模型
# ══════════════════════════════════════════════

class ItemIn(BaseModel):
    name: str
    slot: str
    category: str
    stage: str = "owned"
    color_main: Optional[str] = None
    color_hex: Optional[str] = None
    is_neutral: int = 0
    color_second: Optional[str] = None
    pattern: Optional[str] = "纯色"
    formality: Optional[int] = 3
    temp_min: Optional[int] = 5
    temp_max: Optional[int] = 30
    # 保暖值（℃）—— 「26℃穿衣法则」里的那个数。只上装参与加总。
    # ⚠️ 必须在这里显式声明：`_insert()` 按 ITEM_FIELDS 过滤，
    # 没声明的字段会被静默丢掉（img_* 就是这么丢了整整一版）。
    warmth: Optional[int] = None
    thickness: Optional[int] = 3
    collar: Optional[str] = None
    length_band: Optional[str] = "常规"
    sleeve: Optional[str] = "长袖"
    fit: Optional[str] = "合身"
    material: Optional[str] = None
    status: str = "in_wardrobe"
    location: Optional[str] = None
    notes: Optional[str] = None
    intent: Optional[str] = None
    source_url: Optional[str] = None
    eval_batch_id: Optional[int] = None
    purchase_price: Optional[float] = None
    purchase_date: Optional[str] = None
    is_signature: int = 0
    # 穿搭标签（JSON 数组字符串）。**必须在模型里显式声明** —— _insert() 按
    # ITEM_FIELDS 过滤，没声明的字段会被静默丢掉（img_* 就是这么丢了整整一版）。
    tags: Optional[str] = None
    # 图片三件套。**必须在模型里显式声明** —— _insert() 按 ITEM_FIELDS 过滤，
    # 没声明的字段会被静默丢掉：之前上传的衣服全都没有图，就是因为漏了这三行。
    img_raw: Optional[str] = None
    img_display: Optional[str] = None
    img_llm: Optional[str] = None
    img_mask: Optional[str] = None
    img_worn: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _norm_tags(cls, v):
        """tags 允许数组、JSON 字符串或手写逗号分隔，统一成入库用的 JSON 字符串。

        **必须在模型层归一化**：`_clean_payload` 是在 Pydantic 校验之后才跑的，
        数组传进来会在 422 就被挡下，根本轮不到它（实测过 —— apitest 直接报 422）。
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, (list, tuple, set)):
            return rules.dump_tags(v) or None
        return rules.dump_tags(rules.item_tags({"tags": v})) or None


class PrebuyIn(BaseModel):
    temp: Optional[int] = None
    occasion: Optional[str] = None
    save: bool = True


ITEM_FIELDS = list(ItemIn.model_fields.keys())


# ══════════════════════════════════════════════
# 应用
# ══════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="胶囊衣橱", lifespan=lifespan)

# 口令门。**没配 data/gate.json 时整段穿透**，本机行为与加它之前一字不差；
# 配了（= 挂到公网上）才要求先过门。见 app/gate.py 顶部为什么不用"来自本机"当判据。
gate.install(app)


def _clean_payload(data: dict) -> dict:
    """过滤到已声明字段 + 归一化 tags。

    tags 允许三种写法（前端传 JSON 字符串 / 数组 / 手写逗号分隔），入库统一成
    JSON 字符串 —— 存法不统一的话 rules 那边解析不出来，就会静默当成"没有标签"。
    """
    data = {k: v for k, v in data.items() if k in ITEM_FIELDS}
    if "tags" in data:
        v = data["tags"]
        if v is None or (isinstance(v, str) and not v.strip()):
            data["tags"] = None
        elif isinstance(v, (list, tuple, set)):
            data["tags"] = rules.dump_tags(v) or None
        else:
            data["tags"] = rules.dump_tags(rules.item_tags({"tags": v})) or None
    return data


def _insert(conn, data: dict) -> int:
    data = _clean_payload(data)
    cols = ", ".join(data)
    ph = ", ".join("?" * len(data))
    cur = conn.execute(f"INSERT INTO items ({cols}) VALUES ({ph})", list(data.values()))
    return cur.lastrowid


def _update(conn, item_id: int, data: dict) -> None:
    data = _clean_payload(data)
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(
        f"UPDATE items SET {sets}, updated_at = datetime('now','localtime') WHERE id = ?",
        list(data.values()) + [item_id],
    )


# ══════════════════════════════════════════════
# 元数据
# ══════════════════════════════════════════════

APP_TAG = "capsule-wardrobe"


@app.get("/api/ping")
def api_ping():
    """给启动器探活用：判断某个端口上跑的是不是本应用。

    只能回一个固定标识，不要返回业务数据 —— 它被调用时可能连库都还没初始化。
    """
    return {"app": APP_TAG}


@app.get("/api/meta")
def api_meta():
    return {
        "categories": CATEGORIES,
        "colors": [{"name": c, "hex": h, "neutral": n} for c, h, n in COLORS],
        "collars": COLLARS,
        "lengths": LENGTHS,
        "sleeves": SLEEVES,
        "fits": FITS,
        "materials": MATERIALS,
        "patterns": PATTERNS,
        "statuses": STATUSES,
        "status_names": STATUS_NAMES,
        "occasions": OCCASIONS,
        "slots": SLOT_NAMES,
        # 穿搭标签词表。**只下发引擎真的会读的标签**，前端按 slots 决定在哪层显示，
        # 免得出现"点了没反应"的控件。
        "tags": rules.TAGS,
        # 品类默认值下发到前端：选完槽位/品类就能就地填满，不用来回请求
        "category_defaults": catalog.CATEGORY_DEFAULTS,
        # 颜色词表也下发：图片测色不可信时（截图），前端要从模型给的命名里取颜色
        "color_words": catalog.COLOR_WORDS,
        # 体感舒适温度（「26℃穿衣法则」里 26 的那个位置）。前端要显示、要能改。
        "comfort": prefs.comfort(),
        "comfort_range": [prefs.COMFORT_MIN, prefs.COMFORT_MAX],
        "comfort_folk": prefs.COMFORT_FOLK,
    }


@app.get("/api/prefs")
def api_prefs():
    return {"ok": True, "prefs": prefs.load(),
            "range": [prefs.COMFORT_MIN, prefs.COMFORT_MAX],
            "default": prefs.DEFAULT_PREFS["comfort"],
            "folk": prefs.COMFORT_FOLK}


class PrefsIn(BaseModel):
    comfort: Optional[float] = None


@app.post("/api/prefs")
def api_prefs_save(payload: PrefsIn):
    """改「体感舒适温度」。

    它直接决定"多少度以上能只穿一件、多少度以下必须有第二层" ——
    所以改完必须让引擎的缓存失效。`_CORE_CACHE` 的键含 comfort，天然会失效；
    这里再清一次是防止将来有人把 comfort 从键里拿掉（那个坑吃过）。
    """
    saved = prefs.save(payload.model_dump(exclude_none=True))
    graph._CORE_CACHE.clear()
    return {"ok": True, "prefs": saved}


# ══════════════════════════════════════════════
# 单品
# ══════════════════════════════════════════════

@app.get("/api/items")
def api_items(stage: Optional[str] = None, slot: Optional[str] = None):
    sql = "SELECT * FROM items WHERE 1=1"
    params: list = []
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    if slot:
        sql += " AND slot = ?"
        params.append(slot)
    sql += " ORDER BY slot, id"
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@app.post("/api/items")
def api_create_item(payload: ItemIn):
    conn = db.get_conn()
    try:
        new_id = _insert(conn, payload.model_dump())
        conn.commit()
        row = conn.execute("SELECT * FROM items WHERE id = ?", (new_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.post("/api/items/bulk")
def api_create_items_bulk(payload: list[ItemIn] = Body(...)):
    """批量入库 —— 录入页的「全部入库」走这里。

    逐条插入，单条出错不影响其余：返回 {created: [...], failed: [{index, error}]}。
    不做整体事务回滚，因为用户此时已经排好队，回滚掉全部比丢一条更糟。
    """
    created, failed = [], []
    conn = db.get_conn()
    try:
        for i, item in enumerate(payload):
            try:
                with conn:
                    new_id = _insert(conn, item.model_dump())
                created.append(new_id)
            except Exception as exc:
                failed.append({"index": i, "name": item.name, "error": str(exc)})
    finally:
        conn.close()
    return {"created": created, "failed": failed, "count": len(created)}


@app.patch("/api/items/{item_id}")
def api_patch_item(item_id: int, payload: dict = Body(...)):
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(404, "单品不存在")
        _update(conn, item_id, payload)
        conn.commit()
        return dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/items/{item_id}")
def api_delete_item(item_id: int):
    """删单品。

    **不要退回到裸的 `DELETE FROM items`** —— outfit_items.item_id 在老库上没有
    ON DELETE 子句，配上 PRAGMA foreign_keys=ON 会撞外键，于是前端"点了没反应"。
    真正的清理逻辑在 db.delete_item()，测试脚本调的是同一个函数。
    """
    conn = db.get_conn()
    try:
        r = db.delete_item(conn, item_id)
    finally:
        conn.close()
    if not r.get("ok"):
        raise HTTPException(404, "单品不存在")
    return r


@app.post("/api/items/{item_id}/promote")
def api_promote(item_id: int):
    """候选件 → 在库件。买下之后调这个。"""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "单品不存在")
        conn.execute(
            "UPDATE items SET stage='owned', decision='bought', "
            "decided_at=?, status='in_wardrobe', "
            "updated_at=datetime('now','localtime') WHERE id = ?",
            (date.today().isoformat(), item_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/items/{item_id}/reject")
def api_reject(item_id: int):
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE items SET decision='rejected', decided_at=?, "
            "updated_at=datetime('now','localtime') WHERE id = ?",
            (date.today().isoformat(), item_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/items/{item_id}/wear")
def api_wear(item_id: int, day: Optional[str] = None):
    day = day or date.today().isoformat()
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO wear_log (item_id, date) VALUES (?, ?)", (item_id, day))
        conn.execute(
            "UPDATE items SET wear_count = (SELECT COUNT(*) FROM wear_log WHERE item_id = ?) "
            "WHERE id = ?", (item_id, item_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/duplicates")
def api_duplicates():
    """疑似重复录入的件。

    只读，**不自动删** —— 删衣服不可逆，同款不同色也很常见，得人来拍板。
    判据在 app/dupes.py，命令行脚本用的是同一个函数。
    """
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()
    return {"pairs": dupes.find(items)}


# ══════════════════════════════════════════════
# 衣橱体检
# ══════════════════════════════════════════════

@app.get("/api/stats")
def api_stats(temp: int = 20, occasion: Optional[str] = None, detail: bool = False):
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
        # 「不喜欢」进了算法之后要落地成一个**看得见的证据**：这件衣服被否过几次。
        # 该记录在 `w_item.dislike_n`（**按件累计**，不是按套）—— 见 outfits.dislike_item。
        rejected_counts = outfits.dislike_counts(conn)
    finally:
        conn.close()

    if not items:
        return {"empty": True, "temp": temp, "n_items": 0, "n_cores": 0, "n_variants": 0,
                "n_outfits": 0, "efficiency": 0, "density": 0,
                "islands": [], "inactive": [], "hinges": []}

    closet = graph.Closet(items, comfort=prefs.comfort())
    st = graph.wardrobe_stats(closet, temp, occasion)

    def brief(it: dict) -> dict:
        return {"id": it["id"], "name": it["name"], "slot": it["slot"],
                "category": it.get("category"), "color_main": it.get("color_main"),
                "color_hex": it.get("color_hex"), "thickness": it.get("thickness"),
                "temp_min": it.get("temp_min"), "temp_max": it.get("temp_max")}

    result = {
        "empty": False,
        "temp": temp,
        "occasion": occasion,
        **{k: v for k, v in st.items() if k not in ("islands", "inactive")},
        "islands": [brief(it) for it in st["islands"]],
        "inactive": [brief(it) for it in st["inactive"]],
    }

    if detail:
        result["hinges"] = graph.hinge_ranking(closet, temp, occasion)
        cw = []
        for it in items:
            wc = it.get("wear_count") or 0
            price = it.get("purchase_price")
            cw.append({
                "id": it["id"], "name": it["name"], "wear_count": wc,
                "price": price,
                "cpw": round(price / wc, 1) if (price and wc) else None,
                "participations": 0,
            })
        counts = graph.participating_counts(closet, temp, occasion)
        for row in cw:
            row["participations"] = counts.get(row["id"], 0)
        cw.sort(key=lambda r: (r["cpw"] is None, r["cpw"] or 0))
        result["cpw_ranking"] = cw

        # 待淘汰观察：被拒次数 ↓，同一件再附上"删了会损失多少组合"（ΔN）。
        # 两个数要一起看才做得了决定 —— 被拒很多但 ΔN 很高，说明它是**刚需但不讨喜**
        # （该换的是"怎么搭"，不是"把它扔了"）；被拒多且 ΔN≈0，才是真的该清出去。
        base_n = closet.count_cores(temp, occasion)
        disliked = []
        for it in items:
            n = rejected_counts.get(int(it["id"]), 0)
            if n <= 0:
                continue
            disliked.append({
                "id": int(it["id"]), "name": it["name"], "slot": it["slot"],
                "category": it.get("category"), "reject_count": n,
                "delta_n": graph.delta_n(closet, it, temp, occasion, base_n),
            })
        disliked.sort(key=lambda r: (-r["reject_count"], r["delta_n"]))
        result["disliked"] = disliked
        result["dislike_threshold"] = outfits.DISLIKE_ELIMINATE_HINT

        # 体感线索：哪几件反复出现在"偏冷/偏热"的搭配里。**只展示，绝不自动改保暖值。**
        flags = feel.garment_flags()
        by_id = {int(it["id"]): it for it in items}
        result["feel_flags"] = [
            {"id": i, "name": by_id[i]["name"], "slot": by_id[i].get("slot"),
             "warmth": by_id[i].get("warmth"),
             "cold": t["cold"], "hot": t["hot"]}
            for i, t in sorted(flags.items(), key=lambda kv: -(kv[1]["cold"] + kv[1]["hot"]))
            if i in by_id
        ]
        result["comfort"] = prefs.comfort()
    return result


@app.get("/api/efficiency-curve")
def api_curve(occasion: Optional[str] = None):
    """各温度带的核心组合数与效率，用于看季节差异。"""
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()
    if not items:
        return []
    closet = graph.Closet(items, comfort=prefs.comfort())
    n = max(1, len(items))
    out = []
    for t in graph.TEMP_BANDS:
        cores, variants = closet.core_and_variant(t, occasion)
        cap = closet.core_capacity(t)
        out.append({"temp": t, "n_outfits": cores, "n_cores": cores,
                    "n_variants": variants, "core_capacity": cap,
                    "efficiency": round(cores / n, 2),
                    "density": round(cores / cap, 3) if cap else 0.0,
                    "layers": len(graph.layers_for_temp(t))})
    return out


# ══════════════════════════════════════════════
# 买前模拟
# ══════════════════════════════════════════════

@app.post("/api/prebuy/{item_id}")
def api_prebuy(item_id: int, payload: PrebuyIn = PrebuyIn()):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "候选件不存在")
        candidate = dict(row)
        if candidate["stage"] != "candidate":
            raise HTTPException(400, "该单品不是候选件（stage 必须为 candidate）")
        owned = db.load_items(conn, "owned")

        if not owned:
            return {"error": "衣橱还是空的，先把常穿的衣服录进来（至少 10 件）才能算模拟。"}

        result = prebuy.evaluate(candidate, owned,
                                 temp=payload.temp,
                                 occasion=payload.occasion)
        result["owned_count"] = len(owned)

        if payload.save and "error" not in result:
            conn.execute(
                "INSERT INTO prebuy_evals (candidate_id, batch_id, base_n, with_n, delta_n,"
                " revived_count, revival_ids, partner_ids, conflict_ids, seasons,"
                " replace_rate, replace_ids, hole_flag, verdict, verdict_text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, candidate.get("eval_batch_id"), result["base_n"], result["with_n"],
                 result["delta_n"], result["revived_count"],
                 json.dumps(result["revival_ids"]),
                 json.dumps([p["id"] for p in result["partners"]]),
                 json.dumps([c["id"] for c in result["conflicts"]]),
                 json.dumps(result["seasons"], ensure_ascii=False),
                 result["replace_rate"], json.dumps(result["replace_ids"]),
                 1 if result["hole_flag"] else 0,
                 result["verdict"], result["verdict_text"]),
            )
            conn.commit()
        return result
    finally:
        conn.close()


@app.get("/api/prebuy/batch/{batch_id}")
def api_prebuy_batch(batch_id: int):
    """同批候选横向对比。"""
    conn = db.get_conn()
    try:
        cands = [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE eval_batch_id = ? AND stage = 'candidate'",
            (batch_id,)).fetchall()]
        owned = db.load_items(conn, "owned")
        if not cands or not owned:
            return {"batch_id": batch_id, "candidates": []}
        rows = []
        for c in cands:
            r = prebuy.evaluate(c, owned)
            rows.append({
                "id": c["id"], "name": c["name"],
                "color_main": c.get("color_main"), "color_hex": c.get("color_hex"),
                "price": c.get("purchase_price"),
                "delta_n": r.get("delta_n"), "revived_count": r.get("revived_count"),
                "replace_rate": r.get("replace_rate"), "season_count": r.get("season_count"),
                "seasons": r.get("seasons"), "hole_flag": r.get("hole_flag"),
                "verdict": r.get("verdict"), "verdict_text": r.get("verdict_text"),
            })
        rows.sort(key=lambda r: (-(r["delta_n"] or 0), r["replace_rate"] or 0))
        return {"batch_id": batch_id, "candidates": rows, "best": rows[0]["id"] if rows else None}
    finally:
        conn.close()


# ══════════════════════════════════════════════
# 今日推荐 + 反馈学习
# ══════════════════════════════════════════════

class FeedbackIn(BaseModel):
    outfit_id: int
    gesture: str          # accepted | skipped | rejected | auto_worn


def _nearest_workable_temp(closet, temp: float, occasion: Optional[str]) -> Optional[int]:
    """当前温度出不了搭配时，找一个最近能出的温度。

    用户看到空屏最想知道的是"那我调多少度有东西可穿"，
    总比让他自己一格一格拖滑杆试强。
    """
    for d in range(1, 41):
        for cand in (temp - d, temp + d):
            if not (-10 <= cand <= 40):
                continue
            if closet.count_paths(cand, occasion, limit=1) > 0:
                return int(cand)
    return None


def _anchor_hint(closet, item_id: int, temp: float,
                 occasion: Optional[str]) -> dict:
    """「围绕这件搭」搭不出来时，说清是**哪一头卡住**的。

    两种原因、两句话：这件**这个温度穿不上**（该去调温度），
    还是这件能穿但**配不出完整一套**（该补件 / 去掉场合限制）。
    含糊地说一句"搭不出来"，用户不知道该改什么。
    """
    it = next((x for x in closet.items if int(x["id"]) == int(item_id)), None)
    if not it:
        return {"kind": "missing"}
    cover = True
    try:
        tmin, tmax = it.get("temp_min"), it.get("temp_max")
        if tmin is not None and tmax is not None:
            cover = float(tmin) <= float(temp) <= float(tmax)
    except (TypeError, ValueError):
        cover = True
    if not cover:
        return {"kind": "temp", "temp_min": it.get("temp_min"),
                "temp_max": it.get("temp_max")}
    return {"kind": "pair", "slot": it.get("slot")}


@app.get("/api/today")
def api_today(temp: float = 20, occasion: Optional[str] = None, n: int = 3,
              seed: Optional[int] = None, exclude: str = "",
              exclude_sets: str = "",
              anchor: Optional[int] = None,
              save: bool = True, use_llm: bool = True,
              include_washed: bool = False):
    """今天的几套。

    流程就是文档里定的那条：硬规则枚举 → 本地打分排序（含 Beta 权重）
    → 可选交给模型做审美排序 → 落库拿 outfit_id（反馈要用）。

    `include_washed=True`：**不排除洗衣篮**，给「今日」页点到未来某天时用 ——
    洗了的衣服到那天早干了，那时还把它排除掉，数出来的搭配数会偏少，
    跟时间轴上写的数对不上（实测 25℃：252 vs 224，用户会以为是坏的）。
    """
    conn = db.get_conn()
    try:
        all_items = db.load_items(conn, "owned")

        # 洗衣篮：今天洗了的件**真的从候选里剔掉**，不是降权。
        # `avoid_ids` 只是打分罚分，罚完照样可能被选中；而"洗了"是物理上穿不了。
        # **只在这一条路径上剔**：计划页（一周 / 旅行）要按"你衣柜里有什么"排，
        # 洗了不等于那几天不在手边，所以 /api/plan 完全不受影响。
        # ⚠️ 必须取交集 —— 单品删掉之后 id 会残留在 laundry.json 里。
        washed_ids = laundry.ids() & {int(it["id"]) for it in all_items}
        washed = [{"id": int(it["id"]), "name": it["name"], "slot": it["slot"]}
                  for it in all_items if int(it["id"]) in washed_ids]
        # `washed` 一直照实返回（洗衣篮那张卡永远显示今天真实洗了什么），
        # 但**排除动作**只在"就是今天"时才做。
        eff_washed = set() if include_washed else washed_ids
        items = [it for it in all_items if int(it["id"]) not in eff_washed]

        # 「不喜欢」＝今天别给我看这件。**硬排除**（不是降权）——
        # 降权在池子小的时候等于没做（旅行模式尤其），用户会以为按钮没反应。
        # 和洗衣篮一样**只在"就是今天"那一屏生效**：看未来某天时那是另一天的事。
        have_ids = {int(it["id"]) for it in all_items}
        eff_avoid = set() if include_washed else (avoid.ids() & have_ids)
        before_avoid = list(items)
        if eff_avoid:
            items = [it for it in items if int(it["id"]) not in eff_avoid]
        avoided = [{"id": int(it["id"]), "name": it["name"], "slot": it["slot"]}
                   for it in all_items if int(it["id"]) in eff_avoid]

        if not all_items:
            return {"empty": True, "reason": "no_items", "n_items": 0,
                    "temp": temp, "occasion": occasion, "outfits": [],
                    "washed": [], "n_washed": 0}

        # 旅行模式（随身清单）：出门那几天只从"带出去的那几件"里挑。
        # 不加这一段的话，人在外面打开首页，推的全是**留在家里的**大衣 ——
        # 用户只能一件件点「洗了」手工排掉（这正是他原来的做法）。
        # ⚠️ 必须取交集：清单是排行程那一刻写下的 id，之后衣服可能被删。
        # ⚠️ 只在"就是今天"那一屏生效（`include_washed=False`）。时间轴上看别的日子时
        #    不限制 —— 那天未必还在旅行，而且横幅也只在这一屏出现，状态和所见一致。
        kitk = kit.load()
        kit_info = None
        before_kit = list(items)
        if kitk and not include_washed:
            kit_ids = {int(x) for x in kitk["item_ids"]} & {int(it["id"]) for it in all_items}
            items = [it for it in items if int(it["id"]) in kit_ids]
            kit_info = {"label": kitk["label"], "start": kitk["start"], "end": kitk["end"],
                        "n_items": len(kit_ids), "total": len(kitk["item_ids"])}

        if not items:
            # 空态有三种，说的话完全不同：**"衣橱是空的" ≠ "今天没衣服穿" ≠ "全被你不喜欢掉了"**。
            # 而且这一屏必须带上出口，否则用户会被困死。
            if eff_avoid and before_avoid:
                return {"empty": True, "reason": "all_disliked", "n_items": len(all_items),
                        "temp": temp, "occasion": occasion, "outfits": [],
                        "washed": washed, "n_washed": len(washed),
                        "avoided": avoided, "kit": kit_info}
            if kit_info and before_kit:
                # 洗的没几件，却被旅行清单筛空了 —— 多半是清单里的衣服被删过。
                return {"empty": True, "reason": "kit_empty", "n_items": len(all_items),
                        "temp": temp, "occasion": occasion, "outfits": [],
                        "washed": washed, "n_washed": len(washed), "kit": kit_info}
            return {"empty": True, "reason": "all_washed", "n_items": len(all_items),
                    "temp": temp, "occasion": occasion, "outfits": [],
                    "washed": washed, "n_washed": len(washed)}

        closet = graph.Closet(items, comfort=prefs.comfort())
        weights = outfits.load_weights(conn)
        worn = outfits.recent_wear_map(conn)
        # ⚠️ 别叫 `avoid`：会和上面那个 `avoid` 模块撞名，Python 会把整个函数里的
        #    `avoid` 都当成局部变量 → UnboundLocalError（真踩了）。
        avoid_learned = outfits.recent_reject_ids(conn)
        for x in (exclude or "").split(","):
            x = x.strip()
            if x.isdigit():
                avoid_learned.add(int(x))

        # 「换一批」传的是**已经看过的整组搭配**，不是散件。
        # 格式：组内用 `-` 连、组间用 `|` 隔（例：`1-2-3|4-5-6`）。
        # ⚠️ 别用逗号：会和上面 `exclude`（散件）撞。
        exc_sets: list[set[int]] = []
        for grp in (exclude_sets or "").split("|"):
            got = {int(x) for x in grp.split("-") if x.strip().isdigit()}
            if got:
                exc_sets.append(got)

        res = outfits.recommend(closet, temp, occasion, weights, worn,
                                avoid_ids=avoid_learned, n=max(1, min(6, n)), seed=seed,
                                exclude_sets=exc_sets, anchor_id=anchor)

        if res.get("anchor_empty"):
            # 「围绕这件搭」在这两个条件下确实搭不出来 —— 这不是故障，是事实。
            # 说清楚是哪个条件卡住（温度 / 场合），别让人以为是程序坏了。
            nm = next((str(it["name"]) for it in all_items
                       if int(it["id"]) == int(anchor)), "这件")
            return {"empty": True, "reason": "anchor_empty", "n_items": len(all_items),
                    "temp": temp, "occasion": occasion, "outfits": [],
                    "washed": washed, "n_washed": len(washed),
                    "anchor": {"id": int(anchor), "name": nm},
                    "anchor_hint": _anchor_hint(closet, int(anchor), temp, occasion)}

        if not res["outfits"]:
            # 空态必须能自证：说清卡在哪一层，而不是让用户自己猜
            diag = closet.diagnose(temp, occasion)
            diag["nearest_temp"] = _nearest_workable_temp(closet, temp, occasion)
            if kit_info:
                # ⚠️ 旅行模式下 `diagnose` 是**在清单筛过的池子上**算的，于是它会说
                #    "你还没有「鞋」这一类" —— 可用户明明有鞋，只是没装进这趟箱子。
                #    这句错话会被当成 bug（真被报过）。改成说清"是清单里没有"。
                miss = [l["name"] for l in (diag.get("layers") or [])
                        if l.get("required") and not l.get("eligible")]
                diag["hint"] = (
                    f"旅行模式：只从这 {kit_info['n_items']} 件里挑 —— "
                    + (f"清单里没有「{'」「'.join(miss)}」，" if miss else "")
                    + "这个温度下排不出完整的一套。退出旅行模式就能用整个衣橱。")
                diag["kit_missing"] = miss
            return {"empty": True, "reason": "no_legal_outfit", "n_items": len(all_items),
                    "temp": temp, "occasion": occasion,
                    "candidates": res["candidates"], "outfits": [],
                    "washed": washed, "n_washed": len(washed),
                    "kit": kit_info,
                    "diag": diag}

        llm_used, llm_error = False, ""
        if use_llm:
            r = vision.rank_outfits(res["outfits"], temp, occasion)
            llm_used, llm_error = bool(r.get("used")), str(r.get("error") or "")

        if save:
            ids = outfits.save_outfits(conn, res["outfits"], temp, occasion)
            conn.commit()
            for o, oid in zip(res["outfits"], ids):
                o["outfit_id"] = oid

        return {
            "empty": False,
            "temp": temp,
            "occasion": occasion,
            # 衣橱有多少件（含洗着的那几件）。副标题读它 ——
            # 用剔除后的数会让人以为"我的衣服丢了"。
            "n_items": len(all_items),
            "washed": washed,
            "n_washed": len(washed),
            # 今天被「不喜欢」掉的件（可以逐件恢复）
            "avoided": avoided,
            # 旅行模式开着才非 null —— 首页据此显示那条横幅（和"一键退出"）。
            "kit": kit_info,
            "cores": res["cores"],
            "variants": res["variants"],
            # 「换一批」之后真的一套新的都排不出来了（池子太小，例如旅行模式只带了几件）。
            # 前端据此明说"能排的就这些了"，而不是让用户以为按钮坏了。
            "exhausted": bool(res.get("exhausted")),
            # 围绕某件搭时，页头要显示"围绕「X」"＋一键清除
            "anchor": ({"id": int(anchor),
                        "name": next((str(it["name"]) for it in all_items
                                      if int(it["id"]) == int(anchor)), "")}
                       if anchor is not None else None),
            "candidates": res["candidates"],
            "truncated": res.get("truncated", False),
            "llm_used": llm_used,
            # 配了模型却没排上序，要说出来 —— 否则用户只会觉得"审美没跟上"，
            # 而实际上是请求静默失败了（踩过：没关思考模式，正文返回空串）。
            "llm_error": llm_error,
            # 我自己搭的套装。这个温度下能穿的全部端出来，不占那 3 个推荐名额 ——
            # 它是自己写的答案，不该跟"它推荐的"抢位置。
            "sets": [sets.to_outfit(s, temp, occasion)
                     for s in sets.usable_at(conn, temp, occasion)],
            "outfits": res["outfits"],
            # 这个温度穿不上的件 —— 不说清楚的话，用户会以为"新加的衣服没进搭配"
            "inactive": [{"id": it["id"], "name": it["name"], "slot": it["slot"],
                          "category": it.get("category"),
                          "temp_min": it.get("temp_min"), "temp_max": it.get("temp_max")}
                         for it in graph.inactive_now(closet, temp, occasion)],
        }
    finally:
        conn.close()


@app.post("/api/today/feedback")
def api_today_feedback(payload: FeedbackIn):
    """滑动反馈。这是学习闭环唯一的写入口。"""
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM outfits WHERE id = ?",
                            (payload.outfit_id,)).fetchone():
            raise HTTPException(404, "搭配不存在")
        try:
            return outfits.apply_feedback(conn, payload.outfit_id, payload.gesture)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


class FeelIn(BaseModel):
    outfit_id: int
    verdict: str          # cold / hot
    temp: float = 20


@app.post("/api/feel")
def api_feel(payload: FeelIn):
    """穿完之后的体感：偏冷 / 偏热。

    **它动的是舒适线（`prefs.comfort`），不是某件衣服的保暖值。**
    理由写在 `app/feel.py` 顶部：单次"这一套冷"分不清是"你整体更怕冷"
    还是"某件保暖值估低了"，硬归因到单件等于用一次数据改一个永久参数。
    """
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM outfits WHERE id = ?",
                            (payload.outfit_id,)).fetchone():
            raise HTTPException(404, "搭配不存在")
        try:
            out = feel.record(conn, payload.verdict, payload.outfit_id, payload.temp)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, **out}
    finally:
        conn.close()


@app.post("/api/feel/reset")
def api_feel_reset():
    """把舒适线放回默认（学歪了要能一键回去）。"""
    return {"ok": True, "comfort": feel.reset_comfort()}


@app.post("/api/items/{item_id}/dislike")
def api_item_dislike(item_id: int):
    """「不喜欢」**这一件衣服** → **立刻从今天的搭配里消失**。

    ⚠️ 两层含义，缺一不可：
      ① **今天排除**（`avoid`，硬过滤）—— 用户原话：「不喜欢的话，我们的穿搭或者说
         带的衣服里面就不能包含这一件」。**只降权不够**：池子小的时候降权 ≈ 没做，
         按钮看起来像没反应（这一点已经被报过两次）。
      ② **累计证据**（`w_item.dislike_n`）—— 反复不喜欢才是"该淘汰了"的线索。
    出门在外（旅行模式）时顺手把它**从这趟清单里拿掉** —— 箱子里的东西也要对上。
    """
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "这件衣服不存在")
        out = outfits.dislike_item(conn, item_id)
        avoid.add(item_id)
        kk = kit.remove_item(item_id)
        return {"ok": True, "id": int(item_id), "name": row["name"],
                "dislike_n": out["dislike_n"],
                "hint_at": outfits.DISLIKE_ELIMINATE_HINT,
                "kit": kk}
    finally:
        conn.close()


@app.get("/api/avoid")
def api_avoid_get():
    """今天"不喜欢"掉的件（可以逐件恢复）。"""
    conn = db.get_conn()
    try:
        ids = avoid.ids()
        rows = [{"id": int(r["id"]), "name": r["name"], "slot": r["slot"]}
                for r in conn.execute(
                    "SELECT id, name, slot FROM items WHERE id IN (%s)"
                    % ",".join("?" * len(ids)), tuple(ids)).fetchall()] if ids else []
        return {"ids": sorted(ids), "items": rows}
    finally:
        conn.close()


@app.delete("/api/avoid/{item_id}")
def api_avoid_remove(item_id: int):
    """把一件放回今日候选（"不喜欢"是可以反悔的）。"""
    return {"ok": True, "ids": sorted(avoid.remove(item_id))}


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@app.get("/api/timeline")
def api_timeline(days: int = 7, occasion: Optional[str] = None):
    """从现在开始的时间轴：今天 + 未来几天，每天能不能穿出搭配。

    这是「今日」页那一屏从"一天"变成"一条时间轴"的数据源。
    **只回答一个问题：那天按预报的温度，衣橱撑不撑得住。**
    具体穿哪套不在这里算 —— 那是点进那一天之后 /api/today 的事，
    否则这里要为 7 天各排一套搭配，代价高十几倍，而用户一眼只需要看
    "哪天能穿、哪天穿不出"。

    ⚠️ 不排除洗衣篮：洗了不等于那几天还没干。跟 /api/plan 同一口径
    （"按你衣柜里有什么排"），洗衣篮只管**今天**那一屏。
    """
    days = max(1, min(14, int(days or 7)))
    fc = weather.forecast(days=days)
    if not fc.get("ok"):
        # 天气拿不到也**不能编温度** —— 编一个假数字比空着更糟，
        # 用户会拿它去挑衣服。把原因原样带上去，前端显示空态。
        return {"ok": False, "need_city": bool(fc.get("need_city")),
                "stale": bool(fc.get("stale")), "reason": fc.get("reason") or "",
                "city": fc.get("city") or "", "days": []}

    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
    finally:
        conn.close()

    if not items:
        return {"ok": True, "empty": True, "city": fc.get("city") or "",
                "label": fc.get("label") or "", "stale": bool(fc.get("stale")),
                "days": []}

    closet = graph.Closet(items, comfort=prefs.comfort())
    today_iso = date.today().isoformat()
    tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()

    rows = []
    for d in fc.get("days") or []:
        iso = d.get("date")
        temp = d.get("temp")
        if not iso or temp is None:
            continue
        try:
            wd = _WEEKDAY_CN[date.fromisoformat(iso).weekday()]
        except ValueError:
            wd = ""
        # 核心组合数**已经包含"够不够暖"**：不够暖时引擎直接返回 0，
        # 不用再单独判一次温度，否则又是一处"两边各写一套"的口径。
        n = closet.count_cores(temp, occasion)
        rows.append({
            "date": iso, "weekday": wd, "temp": temp,
            "tmin": d.get("tmin"), "tmax": d.get("tmax"),
            "text": d.get("text") or "", "wet": bool(d.get("wet")),
            "n_cores": n, "wearable": n > 0,
            "is_today": iso == today_iso, "is_tomorrow": iso == tomorrow_iso,
        })
        if len(rows) >= days:
            break

    return {"ok": True, "empty": False, "stale": bool(fc.get("stale")),
            "city": fc.get("city") or "", "label": fc.get("label") or "",
            "today": today_iso, "occasion": occasion, "days": rows}


@app.get("/api/today/history")
def api_today_history(days: int = 14):
    """最近穿过什么 —— 首页用它提示"这套上周三穿过"。"""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT w.date, i.id, i.name, i.slot, i.img_display "
            "FROM wear_log w JOIN items i ON i.id = w.item_id "
            "WHERE w.date >= date('now','localtime', ?) "
            "ORDER BY w.date DESC, i.slot",
            (f"-{int(days)} day",),
        ).fetchall()
        by_date: dict[str, list] = {}
        for r in rows:
            by_date.setdefault(r["date"], []).append(
                {"id": r["id"], "name": r["name"], "slot": r["slot"],
                 "img_display": r["img_display"]})
        return {"days": days, "count": len(rows),
                "by_date": [{"date": d, "items": v} for d, v in by_date.items()]}
    finally:
        conn.close()


@app.get("/api/today/stats")
def api_today_stats():
    """学习进度：收了哪些反馈、哪些件在降权。给「它在慢慢懂我」一个可见的证据。"""
    conn = db.get_conn()
    try:
        gestures = {r["gesture"]: r["n"] for r in conn.execute(
            "SELECT gesture, COUNT(*) n FROM feedback GROUP BY gesture").fetchall()}
        down = [{"id": r["item_id"], "name": r["name"], "posterior": round(r["p"], 2)}
                for r in conn.execute(
                    "SELECT w.item_id, i.name, w.alpha/(w.alpha+w.beta) AS p "
                    "FROM w_item w JOIN items i ON i.id = w.item_id "
                    "WHERE (w.alpha + w.beta) > 2 AND w.alpha/(w.alpha+w.beta) < 0.45 "
                    "ORDER BY p").fetchall()]
        up = [{"id": r["item_id"], "name": r["name"], "posterior": round(r["p"], 2)}
              for r in conn.execute(
                  "SELECT w.item_id, i.name, w.alpha/(w.alpha+w.beta) AS p "
                  "FROM w_item w JOIN items i ON i.id = w.item_id "
                  "WHERE (w.alpha + w.beta) > 2 AND w.alpha/(w.alpha+w.beta) > 0.6 "
                  "ORDER BY p DESC").fetchall()]
        return {"gestures": gestures, "total": sum(gestures.values()),
                "down": down[:8], "up": up[:8],
                "conflicts": [{"a": r["a"], "b": r["b"]} for r in conn.execute(
                    "SELECT ia.name a, ib.name b FROM w_pair p "
                    "JOIN items ia ON ia.id = p.item_a_id "
                    "JOIN items ib ON ib.id = p.item_b_id "
                    "WHERE (p.alpha + p.beta) > 2 AND p.alpha/(p.alpha+p.beta) < 0.4 "
                    "ORDER BY p.alpha/(p.alpha+p.beta)").fetchall()][:6]}
    finally:
        conn.close()


# ══════════════════════════════════════════════
# 随身清单 / 旅行模式：出门那几天只从"带出去的这几件"里挑
#
# 和洗衣篮是**两个独立的"缩小"**，方向一致（都在 `api_today` 里做剔除），
# 但生命周期不同 —— 见 `app/kit.py` 开头为什么不合成一个文件。
# 同样**只在「今日」这条路径上生效**：计划页照旧按完整衣橱排。
# ══════════════════════════════════════════════

class KitIn(BaseModel):
    label: str = "旅行"
    start: str
    n_days: int = 1
    item_ids: list[int] = []
    temps: Optional[list[dict]] = None


@app.get("/api/kit")
def api_kit_get():
    """当前随身清单。没开旅行模式时 active=False。"""
    k = kit.load()
    if not k:
        return {"active": False}
    conn = db.get_conn()
    try:
        have = {int(r["id"]) for r in conn.execute(
            "SELECT id FROM items WHERE stage='owned'").fetchall()}
    finally:
        conn.close()
    live = [i for i in k["item_ids"] if int(i) in have]
    # `missing` 是给人看的：清单里有几件衣服后来被删了（清单会自动缩小，
    # 但不该悄悄缩 —— 说一声，用户才知道为什么少了）。
    return {"active": True, "label": k["label"], "start": k["start"], "end": k["end"],
            "item_ids": live, "n_items": len(live),
            "missing": len(k["item_ids"]) - len(live)}


@app.post("/api/kit")
def api_kit_set(payload: KitIn):
    """开一趟旅行模式。清单会被裁到"当前衣橱里真实存在的那几件"。"""
    conn = db.get_conn()
    try:
        have = {int(r["id"]) for r in conn.execute(
            "SELECT id FROM items WHERE stage='owned'").fetchall()}
    finally:
        conn.close()
    ids = [int(i) for i in payload.item_ids if int(i) in have]
    if not ids:
        raise HTTPException(400, "这份清单里的衣服在当前衣橱里一件都找不到")
    k = kit.set_kit(payload.label, payload.start, payload.n_days, ids, payload.temps)
    return {"ok": True, "label": k["label"], "start": k["start"], "end": k["end"],
            "n_items": len(ids)}


@app.delete("/api/kit")
def api_kit_clear():
    """退出旅行模式（回到完整衣橱）。"""
    kit.clear()
    return {"ok": True, "active": False}


# ══════════════════════════════════════════════
# 洗衣篮：今天洗了的件，暂时不进搭配
#
# **只在「今日」这条路径上生效**（api_today 里那个剔除）。
# 计划页（一周 / 旅行）不读它 —— 旅行清单回答的是"出门那几天这件在不在手边"，
# "今天这件能不能穿"是另一个问题。两个问题的答案不该互相污染。
# ══════════════════════════════════════════════

class LaundryIn(BaseModel):
    item_id: int
    # true = 洗了，false = 放回。默认"洗"：这个接口的主要用法就是把它丢进篮子。
    washed: bool = True


@app.post("/api/laundry")
def api_laundry(payload: LaundryIn):
    """洗了 / 放回一件衣服。

    **这里不校验这件存不存在**：用户可能同时开着两个页面，一边把某件删掉了。
    只管记账；跟当前衣橱取交集那一步在 `api_today` 里做
    （为什么必须取交集，见 `laundry.ids()` 的 docstring）。
    """
    ids = (laundry.wash(payload.item_id) if payload.washed
           else laundry.restore(payload.item_id))
    return {"ok": True, "washed_ids": sorted(ids)}


@app.post("/api/laundry/reset")
def api_laundry_reset():
    """全部放回。界面上那个「恢复」按钮。"""
    return {"ok": True, "washed_ids": sorted(laundry.clear())}


# ══════════════════════════════════════════════
# 天气：让「今日」自己知道外面多少度
# ══════════════════════════════════════════════

class WeatherCfgIn(BaseModel):
    city: Optional[str] = None
    admin: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    timezone: Optional[str] = None
    bias: Optional[str] = None


@app.get("/api/weather")
def api_weather(days: int = 14, force: bool = False):
    """当日 / 未来预报。

    **这个接口不会因为网络问题而返回 5xx。** 拿不到就 ok=False + reason，
    今日页安静地退回手动滑杆 —— 天气是个增强，不该让首页打不开。
    天气预报是直连 open-meteo 的，跟接没接大模型无关（模型 API 不联网）。
    """
    return weather.forecast(days=days, force=force)


@app.get("/api/weather/search")
def api_weather_search(q: str = ""):
    hits = weather.search_city(q)
    return {"ok": bool(hits), "q": q, "hits": hits,
            "reason": "" if hits else "没搜到这个城市 —— 换个写法试试，比如只写「杭州」。"}


@app.get("/api/weather/config")
def api_weather_config():
    cfg = weather.load_config()
    return {"config": cfg, "label": weather.city_label(cfg),
            "has_city": bool(cfg.get("city") and cfg.get("lat") is not None)}


@app.post("/api/weather/config")
def api_weather_config_save(payload: WeatherCfgIn):
    cfg = weather.save_config(payload.model_dump(exclude_none=True))
    return {"ok": True, "config": cfg, "label": weather.city_label(cfg)}


# ══════════════════════════════════════════════
# 多天计划：一周穿搭 / 旅行穿搭
# ══════════════════════════════════════════════

class PlanSegment(BaseModel):
    """旅行的一"段"：一段日子待在一个地方。

    一趟行程常常不止一个地方（京都 3 天 + 大阪 3 天）。**排布引擎不需要知道
    这件事** —— 它只要一条按日期排好的温度序列（`plan.build_plan` 的 `days`），
    而"少带件"本来就是在"整趟行程共用一份清单"的前提下算的，两段天然合出一个箱子。
    所以分段只在这一层做：**把各段各自取到的天气拼成一条时间轴**。
    """
    label: Optional[str] = None        # 展示名（京都 · 日本）
    city: Optional[str] = None         # 给 label 兜底，顺带支持只给城市名
    lat: Optional[float] = None
    lon: Optional[float] = None
    start: Optional[str] = None        # YYYY-MM-DD，这一段从哪天开始
    days: int = 3


class PlanIn(BaseModel):
    mode: str = "week"                 # week（在家，不重样）| trip（出门，少带件）
    start: Optional[str] = None        # YYYY-MM-DD，从这里开始排
    n_days: int = 7
    city: Optional[str] = None         # 目的地（旅行）。不给就用已设置的城市
    lat: Optional[float] = None
    lon: Optional[float] = None
    # 分段目的地（旅行）。给了它就以它为准 —— 每段各取各的天气，
    # 按日期首尾相接拼成一条行程。见 `_plan_weather_segments`。
    segments: Optional[list[PlanSegment]] = None
    occasion: Optional[str] = None
    temps: Optional[list[float]] = None   # 直接给温度序列，不查天气
    days: Optional[list[dict]] = None     # 直接给完整 days（date + temp），不带天气
    seed: Optional[int] = 0
    use_llm: bool = True
    # 「必带」：用户手动指定的单品 id。排布会优先把它们穿进去；
    # 试删一件都不许砍；实在穿不上也照样进箱子（标「备着」）。见 plan.build_plan。
    must_ids: Optional[list[int]] = None


def _plan_weather(payload: PlanIn) -> dict:
    """决定"排哪儿的天气"。目的地不是你设置的城市时，**绝不能改配置** ——
    看一眼东京的天气不该把你的常住城市从上海改成东京。"""
    cfg = weather.load_config()
    bias = cfg.get("bias") or "avg"

    if payload.lat is not None and payload.lon is not None:
        same = (cfg.get("lat") is not None and cfg.get("lon") is not None
                and abs(float(cfg["lat"]) - payload.lat) < 0.02
                and abs(float(cfg["lon"]) - payload.lon) < 0.02)
        if not same:
            return weather.forecast_for(payload.lat, payload.lon, days=16,
                                        bias=bias, label=payload.city or "")
    elif payload.city and payload.city != cfg.get("city"):
        hits = weather.search_city(payload.city)
        if not hits:
            return {"ok": False, "days": [],
                    "reason": f"没搜到「{payload.city}」这个城市。"}
        h = hits[0]
        return weather.forecast_for(h["lat"], h["lon"], days=16,
                                    timezone=h["timezone"], bias=bias,
                                    label=h["label"])
    return weather.forecast(days=16)


def _day_axis(start: Optional[str], n: int) -> list[str]:
    """从 `start` 起的连续 n 个日期。算不出来就返回空列表 —— 不编日期。"""
    try:
        d0 = date.fromisoformat(str(start)[:10])
    except (TypeError, ValueError):
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range(max(0, int(n)))]


def _plan_weather_segments(segs: list[PlanSegment],
                           default_start: Optional[str] = None) -> dict:
    """分段目的地：**每段各取各的天气**，再按日期拼成一条时间轴。

    一趟行程常常不止一个地方（京都 3 天 + 大阪 3 天）。这里只做一件事：
    把"哪几天在哪儿"变成一条按日期排好的温度序列。

    ⚠️ **排布引擎完全不知道"城市"这回事** —— `plan.build_plan` 要的只是一个
    `days` 列表。这不是缺陷，是设计前提：**"少带一件"本来就是按"整趟行程共用
    一份清单"算的**（试删会问"不带这件，这几天还排得出吗"，问的是整趟），
    所以两段天然合出一个箱子 —— 是必然，不是碰巧。

    三种返回：

      · `ok=True,  missing=[]`     → 天气齐了，`days` 就是整趟行程
      · `ok=True,  missing=[名字]` → 有段落落在预报之外（远期行程）。**不猜温度**：
                                     `days` 只放拿得到的那几天，同时给出显式日期轴
      · `ok=False, need_city=…`    → 城市搜不到 / 一点天气都取不到

    ⚠️ **日期轴必须显式给出来**（`day_axis`），不能让调用方"从 start 起连续 N 天"
    去推：两段之间可能隔着几十天（10/01 京都、11/20 大阪），推出来的日期是错的，
    而用户填完温度提交时用的正是这些日期。
    """
    cfg = weather.load_config()
    bias = cfg.get("bias") or "avg"
    out: list[dict] = []
    metas: list[dict] = []
    axis: list[str] = []
    missing: list[str] = []
    fetched: dict[tuple, dict] = {}       # 同坐标只取一次
    cursor: Optional[str] = default_start

    for idx, s in enumerate(segs or []):
        name = str(s.label or s.city or "").strip()
        lat, lon = s.lat, s.lon
        if (lat is None or lon is None) and name:
            hits = weather.search_city(name)
            if not hits:
                return {"ok": False, "days": [], "segments": [], "day_axis": [],
                        "reason": f"没搜到「{name}」这个地方。"}
            lat, lon = hits[0]["lat"], hits[0]["lon"]
            # 显示名用搜到的完整地名（"京都" → "京都 · 京都府 · 日本"）
            name = str(hits[0].get("label") or name)

        key = ((None, None) if lat is None or lon is None
               else (round(float(lat), 3), round(float(lon), 3)))
        fc = fetched.get(key)
        if fc is None:
            fc = (weather.forecast(days=16) if key == (None, None)
                  else weather.forecast_for(lat, lon, days=16,
                                            bias=bias, label=name))
            fetched[key] = fc
        if not fc.get("ok"):
            return {"ok": False, "days": [], "segments": [], "day_axis": [],
                    "need_city": bool(fc.get("need_city")),
                    "reason": fc.get("reason") or "取不到天气。"}

        want = max(1, int(s.days or 1))
        start = s.start or cursor
        got = plan.days_from_weather(fc, start=start, count=want)
        label = str(fc.get("label") or name or f"第 {idx + 1} 段")
        for d in got:
            d["seg"] = label
        out.extend(got)

        seg_axis = _day_axis(start, want)
        axis.extend(seg_axis)
        if seg_axis:
            cursor = (date.fromisoformat(seg_axis[-1])
                      + timedelta(days=1)).isoformat()
        metas.append({
            "label": label, "days": want, "got": len(got),
            "from": seg_axis[0] if seg_axis else None,
            "to": seg_axis[-1] if seg_axis else None,
        })
        if len(got) < want:
            missing.append(label)

    # 段与段万一重叠，日期去重（先出现的留着）—— 排布拿到重复日期会排出重复的一天
    uniq: list[str] = []
    seen: set[str] = set()
    for d in axis:
        if d not in seen:
            seen.add(d)
            uniq.append(d)

    reason = ""
    if missing:
        last = max((str(d.get("date") or "") for f in fetched.values()
                    for d in (f.get("days") or [])), default="")
        reason = (f"{'、'.join(missing)} 落在预报之外 —— 天气预报最多只到 16 天内，"
                  f"那几天谁也查不到。")
        if last:
            reason += f"目前最远能查到 {last}。"

    return {
        "ok": True,
        "days": out,
        "day_axis": uniq,
        "segments": metas,
        "missing": missing,
        "reason": reason,
        "weather_meta": {
            "segments": metas,
            "label": " + ".join(m["label"] for m in metas),
            "bias": bias,
            "cached": any(bool(f.get("cached")) for f in fetched.values()),
            "stale": any(bool(f.get("stale")) for f in fetched.values()),
            "reason": "；".join(sorted({str(f.get("reason") or "")
                                        for f in fetched.values()} - {""})),
        },
    }


@app.post("/api/plan")
def api_plan(payload: PlanIn):
    """排出多天的穿搭。

    和今日页一样，**所有组合都来自硬规则枚举**，模型只在最后写一句总结，
    绝不参与生成（让它生成，它就会推荐你根本没有的衣服）。
    """
    conn = db.get_conn()
    try:
        items = db.load_items(conn, "owned")
        weights = outfits.load_weights(conn)
        worn = outfits.recent_wear_map(conn)
    finally:
        conn.close()

    empty = {"days": [], "packing": [], "packing_groups": [], "tips": [], "warnings": []}
    if not items:
        return {"ok": False, "reason": "衣橱里还没有单品，先去录几件。", **empty}

    wmeta = None
    if payload.days:
        days = [dict(d) for d in payload.days]
    elif payload.temps:
        days = [{"date": None, "temp": float(t)} for t in payload.temps]
    elif payload.segments:
        # 分段目的地（旅行）：每段各取各的天气，按日期拼成一条行程。
        merged = _plan_weather_segments(payload.segments, payload.start)
        if not merged.get("ok"):
            return {"ok": False, "reason": merged.get("reason") or "取不到天气。",
                    "need_city": bool(merged.get("need_city")), **empty}
        days = merged["days"]
        if merged.get("missing"):
            # 有段落落在预报之外 —— **不猜温度**。把拿得到的那几天给出去当预填，
            # 并且把整趟的日期轴显式给出来：段与段之间可能出现空档，
            # 前端不能按"从某天起连续 N 天"去推（推出来的日期是错的）。
            return {
                "ok": False, "need_temps": True,
                "reason": merged["reason"],
                "day_axis": merged["day_axis"],
                "segments": merged["segments"],
                "start": (merged["day_axis"] or [payload.start])[0],
                "n_days": len(merged["day_axis"]) or payload.n_days,
                "days": days,
                "n_items_in_closet": len(items),
                **{k: v for k, v in empty.items() if k != "days"},
            }
        wmeta = merged["weather_meta"]
    else:
        fc = _plan_weather(payload)
        if not fc.get("ok"):
            return {"ok": False, "reason": fc.get("reason") or "取不到天气。",
                    "need_city": bool(fc.get("need_city")), **empty}
        wmeta = {k: fc.get(k) for k in
                 ("city", "label", "stale", "cached", "reason", "bias")}
        days = plan.days_from_weather(fc, start=payload.start, count=payload.n_days)
        if len(days) < payload.n_days:
            # 预报只到 16 天。旅行常常排在一个月后 —— 那时候谁也查不到当天天气。
            # **不猜。** 把已经能查到的几天先给出去（前端拿它们当预填），
            # 剩下的让用户给个大概温度，照样能排出来。
            until = (fc.get("days") or [{}])[-1].get("date")
            got = len(days)
            reason = (f"你想排的这几天里，只有前 {got} 天有预报（预报到 {until}）。"
                      if got else
                      f"这段时间还没有预报 —— 天气预报最多只到 {until}。")
            return {
                "ok": False, "need_temps": True, "reason": reason,
                "forecast_until": until, "city": fc.get("label") or "",
                "start": payload.start, "n_days": payload.n_days,
                "days": days,
                "n_items_in_closet": len(items),
                **{k: v for k, v in empty.items() if k != "days"},
            }

    closet = graph.Closet(items, comfort=prefs.comfort())
    res = plan.build_plan(closet, days, occasion=payload.occasion,
                          weights=weights, worn=worn, mode=payload.mode,
                          seed=payload.seed, use_llm=payload.use_llm,
                          must_ids=payload.must_ids)
    res["n_items_in_closet"] = len(items)
    if wmeta:
        res["weather_meta"] = wmeta

    # 一天都排不出来 → 说清卡在哪一层，别让用户自己猜
    if res.get("ok") and not res.get("n_planned"):
        res["diag"] = closet.diagnose(float(days[0].get("temp") or 20), payload.occasion)
    return res


@app.get("/api/plan/week")
def api_plan_week(days: int = 7, occasion: Optional[str] = None,
                  start: Optional[str] = None, mode: str = "week"):
    """GET 版，方便直接用浏览器试（?days=7&mode=trip&start=2026-10-01）。"""
    return api_plan(PlanIn(mode=mode, n_days=days, occasion=occasion, start=start))


# ══════════════════════════════════════════════
# 套装：自己搭的一整套，之后推荐直接能用
# ══════════════════════════════════════════════

class SetIn(BaseModel):
    # 全部可空：PATCH 只改传上来的那几个字段，见 api_sets_update
    name: Optional[str] = None
    item_ids: Optional[list[int]] = None
    occasion: Optional[str] = None
    note: Optional[str] = None


class SetEvalIn(BaseModel):
    item_ids: list[int] = []
    occasion: Optional[str] = None


class SetUseIn(BaseModel):
    temp: float = 20
    occasion: Optional[str] = None


@app.get("/api/sets")
def api_sets(temp: Optional[float] = None, occasion: Optional[str] = None):
    """我搭的套装。给了 temp 就顺带标出「这个温度下哪些能直接穿」。

    能不能穿一律**现算**（`sets.can_wear` → 引擎枚举），库里不存温度区间 ——
    存了就会和引擎漂移：你改一件单品的温度，那个区间就悄悄过期了。
    """
    conn = db.get_conn()
    try:
        return {"sets": sets.all_with_state(conn, temp, occasion),
                "count": conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]}
    finally:
        conn.close()


@app.post("/api/sets/evaluate")
def api_sets_evaluate(payload: SetEvalIn):
    """搭的过程中实时问一句「这样挑能穿吗」。**不落库**，纯预演。"""
    conn = db.get_conn()
    try:
        items, missing = sets.pick_items(conn, payload.item_ids)
        ev = dict(sets.evaluate(items, payload.occasion))
        ev["items"] = [outfits.public_item(it) for it in items]
        ev["count"] = len(items)
        # 取不到的 id 也报出来 —— 前端草稿里存着已被删的件时，用户看到的
        # 应该是"少了 X 件"，而不是少了几件却不知道少了什么
        ev["missing_ids"] = missing
        return ev
    finally:
        conn.close()


@app.post("/api/sets")
def api_sets_create(payload: SetIn):
    """存一套。**存下来的必须是能穿的** —— 存一套永远穿不出去的东西，
    只会在今日页里变成一个永不出现的幽灵，用户还以为功能坏了。"""
    conn = db.get_conn()
    try:
        try:
            row = sets.save_set(conn, payload.name, payload.item_ids,
                                payload.occasion, payload.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return row
    finally:
        conn.close()


@app.patch("/api/sets/{set_id}")
def api_sets_update(set_id: int, payload: SetIn):
    """只改请求里**真的出现过**的字段。

    ⚠️ 不能 `model_dump()` 一把覆盖 —— 那样"没传"会变成"清空"，
    改个名字就把单品和备注一起抹掉（探针实测过：PATCH 只带 name 返回 400）。
    """
    sent = payload.model_fields_set
    conn = db.get_conn()
    try:
        try:
            row = sets.save_set(
                conn,
                name=payload.name if "name" in sent else None,
                item_ids=payload.item_ids if "item_ids" in sent else None,
                occasion=payload.occasion if "occasion" in sent else sets.UNSET,
                note=payload.note if "note" in sent else sets.UNSET,
                set_id=set_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return row
    finally:
        conn.close()


@app.delete("/api/sets/{set_id}")
def api_sets_delete(set_id: int):
    conn = db.get_conn()
    try:
        res = sets.delete_set(conn, set_id)
        if not res.get("ok"):
            raise HTTPException(404, "这套不存在")
        return res
    finally:
        conn.close()


@app.post("/api/sets/{set_id}/use")
def api_sets_use(set_id: int, payload: SetUseIn):
    """今天穿这套（自己搭的）。

    落一条 `outfits` 记录再挂 `auto_worn` —— 走的是和推荐**完全相同**的反馈路径。
    这样"我搭的"和"它推的"在穿着历史上是一视同仁的：wear_log、单品穿着次数、
    Beta 权重都会正常累加，不会因为来源不同就被漏掉。
    """
    conn = db.get_conn()
    try:
        row = sets.get_set(conn, set_id)
        if not row:
            raise HTTPException(404, "这套不存在")
        items = row["items"]
        if not items:
            raise HTTPException(400, "这套里没有单品")
        if not sets.can_wear(items, payload.temp, payload.occasion):
            raise HTTPException(
                400, f"{payload.temp:g}℃ 下穿不了这套 —— 里面的单品属性改过了？")
        oid = outfits.save_outfits(
            conn,
            [{"items": [outfits.public_item(it) for it in items], "score": None}],
            payload.temp, payload.occasion)[0]
        outfits.apply_feedback(conn, oid, "auto_worn")
        sets.mark_used(conn, set_id)
        conn.commit()
        return {"ok": True, "outfit_id": oid, "set_id": set_id,
                "items": len(items), "name": row.get("name")}
    finally:
        conn.close()


# ══════════════════════════════════════════════
# 图片
# ══════════════════════════════════════════════

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """存原图 + 生成展示图与模型图 + **就地提取主色**。

    顺序很重要，三步都不能换：
      ① `imgs.strip_bands` 先从截图里裁出商品照片本身（状态栏 / 价格 / 标题 / 购买栏）
      ② 主色必须在**裁完之后**算 —— 外壳还在时四角取到的是那两条 UI 栏，
         颜色会整个测反（实测黑色皮夹克测成纯白）
      ③ 用 4:5 竖版画布合成展示图，内容占 96%。原来缩进正方形再补白边，
         竖长的衣服照片左右各留 40% 白，看起来就是"图太小"

    **不要再把这个调用包进裸 `except`。** 曾经这里的函数名和 imgs.py 对不上
    （`strip_bands` 早改名叫 `crop_to_content`），异常被 `except Exception` 吞掉，
    于是裁剪静默失效了整整一版：每张展示图都是没裁过的整屏截图，
    用户看到的是"图很小"和"图片里没衣服只有文字"。失败要说出来，不能装作成功。
    """
    from PIL import Image, ImageOps
    import io
    import uuid

    raw = await file.read()
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as exc:
        raise HTTPException(400, f"图片无法解析：{exc}")

    db.IMG_DIR.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:12]
    raw_path = db.IMG_DIR / f"{uid}_raw.jpg"
    disp_path = db.IMG_DIR / f"{uid}_display.webp"
    llm_path = db.IMG_DIR / f"{uid}_llm.jpg"

    im.save(raw_path, "JPEG", quality=92)   # 原图永久归档，标准化会丢信息

    clean, box, trim = imgs.strip_bands(im)

    try:
        color = colorlab.dominant_color(clean)
    except Exception as exc:
        color = {"name": "", "hex": "", "neutral": 0, "observed_hex": "",
                 "share": 0.0, "confident": False, "framed": False,
                 "color_error": str(exc)}
    color["trimmed"] = trim["cropped"]
    color["trim_box"] = list(box)
    color["garment_ok"] = trim["garment_ok"]
    color["keep"] = trim["keep"]

    imgs.compose(clean, 1080).save(disp_path, "WEBP", quality=88)
    imgs.compose(clean, 512).save(llm_path, "JPEG", quality=80)

    return {"uid": uid,
            "img_raw": f"/img/{raw_path.name}",
            "img_display": f"/img/{disp_path.name}",
            "img_llm": f"/img/{llm_path.name}",
            "trimmed": trim["cropped"],
            "trim": trim,
            "color": color}


# ══════════════════════════════════════════════
# 视觉识别
# ══════════════════════════════════════════════

class VisionConfigIn(BaseModel):
    provider: Optional[str] = None
    kind: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[int] = None
    enabled: Optional[bool] = None


@app.get("/api/vision/config")
def api_vision_config():
    return vision.public_config()


@app.post("/api/vision/config")
def api_vision_config_save(payload: VisionConfigIn):
    vision.save_config(payload.model_dump(exclude_none=True))
    return vision.public_config()


@app.post("/api/vision/test")
def api_vision_test():
    return vision.test_connection()


@app.post("/api/vision/analyze")
def api_vision_analyze(payload: dict = Body(...)):
    """批量识别。

    入参：{"items": [{"key": "任意前端标识", "uid": "上传时返回的 uid",
                      "slot": "可选，品类反推不出来时的兜底槽位"}]}
    出参：{"enabled": bool, "results": {key: {...}}, "errors": {key: "..."}}

    并发跑 3 路：串行 80 件太慢，全并发又容易撞到服务商的限流。
    """
    from concurrent.futures import ThreadPoolExecutor

    items = payload.get("items") or []
    enabled, reason = vision.available()
    if not enabled:
        return {"enabled": False, "reason": reason, "results": {}, "errors": {}}

    def one(entry: dict):
        key = str(entry.get("key"))
        uid = str(entry.get("uid") or "")

        # 只接受 uid，不接受任意路径 —— 避免拼出 ../ 读到别的文件
        if not uid or not all(c in "0123456789abcdef" for c in uid):
            return key, {"ok": False, "error": "图片标识不合法"}
        path = db.IMG_DIR / f"{uid}_llm.jpg"
        if not path.exists():
            path = db.IMG_DIR / f"{uid}_raw.jpg"
        if not path.exists():
            return key, {"ok": False, "error": "找不到图片"}

        slot = entry.get("slot") or None
        return key, vision.analyze(path, fallback_slot=slot)

    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for key, res in pool.map(one, items[:200]):
            if res.get("ok"):
                results[key] = res
            else:
                errors[key] = res.get("error", "识别失败")
    return {"enabled": True, "results": results, "errors": errors}


# ══════════════════════════════════════════════
# 数据备份
# ══════════════════════════════════════════════

@app.get("/api/backup")
def api_backup():
    """把整个 `data/` 打包成一个 zip 下载。

    这是**唯一能把云上数据拿回来**的通道。云上的沙箱不归自己管 ——
    没有这个口子，数据就等于只存在别人家里，随时可能没了而自己不知道。

    ⚠️ 库要用 sqlite3 的在线备份 API 做**一致快照**再打包，
    不能边写边拷文件：拷出来可能是个坏库，而且**拷坏了不会报错**。
    ⚠️ 它在 `/api/` 下 → **口令门自动挡住未登录**（gate 对 `/api/*` 回 401）。
    """
    import io as _io
    import zipfile

    conn = db.get_conn()
    try:
        snap = db.backup_db(conn)          # 一致快照，落在 data/ 下
    finally:
        conn.close()

    try:
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "data/closet.db")
            for p in sorted(db.DATA_DIR.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(db.DATA_DIR)
                # ⚠️ 必须按**路径任一段**判断：`data/images.bak-2026…/` 里的文件，
                # 它的 `.bak-` 在**父目录名**上。只查 `p.name` 会让整棵旧备份树漏进包
                # （实测多打 479 张旧图、24MB），而且**不报错、只是包变大**。
                if any(".bak-" in part or part == "__pycache__" for part in rel.parts):
                    continue               # 旧备份不进包，省体积
                # `closet.db.*`（-wal / -shm / .bak-…）是**正在被写的那个库**的伴生物。
                # 放进包毫无用处，还原时还可能把快照带歪 —— 上面那份快照是自洽的。
                if rel.name.startswith("closet.db."):
                    continue
                if p.resolve() in (snap.resolve(), db.DB_PATH.resolve()):
                    continue               # 用快照，不用正在被写的那个
                z.write(p, "data/" + rel.as_posix())
        blob = buf.getvalue()
    finally:
        snap.unlink(missing_ok=True)       # 快照只是这次请求的临时产物

    name = f"closet-data-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return Response(
        blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ══════════════════════════════════════════════
# 静态页面
# ══════════════════════════════════════════════

# 同时接受 HEAD：FastAPI 的 @app.get 不会自动支持 HEAD，探测工具（浏览器预览
# 面板、存活检查）用 HEAD 探活时会拿到 405，从而误判"服务不可达"。
# HEAD 只回状态码，不回文件体。

def _page(filename: str, method: str):
    # no-store：开发期一直在改页面，浏览器缓存住旧 HTML 会让人以为改动没生效
    # （这个坑真踩过 —— 用户刷新后看到的还是上一版界面）。
    headers = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
    if method == "HEAD":
        return Response(status_code=200, headers=headers)
    return FileResponse(WEB_DIR / filename, headers=headers)


@app.api_route("/", methods=["GET", "HEAD"])
def index(request: Request):
    """首页 = 今日。打开就有一套能直接穿出门的搭配。"""
    return _page("today.html", request.method)


@app.api_route("/today", methods=["GET", "HEAD"])
def page_today(request: Request):
    return _page("today.html", request.method)


@app.api_route("/stats", methods=["GET", "HEAD"])
def page_stats(request: Request):
    """衣橱体检 —— 月报性质，不需要天天看，所以从首页降级到这里。"""
    return _page("index.html", request.method)


@app.api_route("/settings", methods=["GET", "HEAD"])
def page_settings(request: Request):
    """模型接入页。所有「去配置」的入口都指向这里 —— 之前把配置面板折叠在录入页里，
    用户点「去配置」跳过去什么也看不到。接入入口必须是一个稳定、独立、直接打开的地址。"""
    return _page("settings.html", request.method)


@app.api_route("/wardrobe", methods=["GET", "HEAD"])
def page_wardrobe(request: Request):
    return _page("wardrobe.html", request.method)


@app.api_route("/prebuy", methods=["GET", "HEAD"])
def page_prebuy(request: Request):
    return _page("prebuy.html", request.method)


@app.api_route("/plan", methods=["GET", "HEAD"])
def page_plan(request: Request):
    """一周穿搭 / 旅行穿搭。两个模式共用同一屏 —— 它们的区别只有一句代价函数。"""
    return _page("plan.html", request.method)


@app.api_route("/sets", methods=["GET", "HEAD"])
def page_sets(request: Request):
    """自己搭的套装。存下来之后，今日页会优先端出来。"""
    return _page("sets.html", request.method)


db.IMG_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/img", StaticFiles(directory=str(db.IMG_DIR)), name="img")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
