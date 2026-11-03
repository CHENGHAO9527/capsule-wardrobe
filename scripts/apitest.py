"""接口冒烟测试：起服务后跑一遍，确认 HTTP 层通了。

用法：python scripts/apitest.py --out apitest.out.txt [--port 8077]
"""

from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _resolve_base(port: int = 8077) -> str:
    """挑一个**真的在跑本应用**的地址。

    ⚠️ 原来这里写死 `http://127.0.0.1:8077`（2026-09-27 修）：
    Windows 允许 `0.0.0.0:8077`（本应用）和 `127.0.0.1:8077`（**另一个程序**）
    同时监听 —— 当天另一个项目起的 `python -m http.server` 就占着回环。
    写死回环的话，这一整套断言**测的是别人家的服务**：每条都失败，
    而失败信息完全指不到原因（看起来像"整个后端都挂了"）。
    所以逐个候选地址 /api/ping 验明正身，用第一个认得出本应用的。
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    cands = [f"http://127.0.0.1:{port}"]
    try:
        from serve import lan_ips  # noqa: PLC0415
        cands += [f"http://{ip}:{port}" for ip in lan_ips()]
    except Exception:
        pass
    for base in cands:
        try:
            req = urllib.request.Request(base + "/api/ping",
                                         headers={"Cache-Control": "no-store"})
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if json.loads(r.read().decode("utf-8")).get("app") == "capsule-wardrobe":
                    return base
        except Exception:
            continue
    raise SystemExit(
        "  8077 上找不到本应用，试过这些地址：\n    "
        + "\n    ".join(cands)
        + "\n  服务没起？双击 start-lan.bat"
    )


BASE = _resolve_base()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import db, laundry  # noqa: E402

# 测试会写进这些表。**收尾时必须精确还原，不能整表 DELETE** ——
# 以前这里写的是 `DELETE FROM outfits`，跑一次就把用户的搭配历史清空一次，
# 顺带把 w_item / w_pair 里学到的权重清零（"学习闭环看着接上了实际没启动"的原因之一），
# 还因为裸 sqlite3.connect 默认 foreign_keys=OFF，级联不触发，
# 在 outfit_items 里攒了 979 行孤儿。快照 → 还原才是对的。
_SNAP_TABLES = ("outfits", "outfit_items", "feedback", "w_item", "w_pair")

# 模块级：脚本中途崩了也要能在 __main__ 的 finally 里还原
_SNAP: dict | None = None

# 断言失败的清单。**结论行据此变红** ——
# 以前这里所有断言都只是 `print("  FAIL ...")`，而结尾无条件打印「全部通过。」，
# 于是 grep 那一行永远为真：断言压根拦不住任何东西（写洗衣篮那节时发现的）。
_FAILS: list[str] = []


def _fail(msg: str) -> None:
    _FAILS.append(msg)
    print(f"  FAIL {msg}")


def _snapshot(conn) -> dict:
    snap = {}
    for t in _SNAP_TABLES:
        try:
            snap[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
        except Exception:
            snap[t] = []
    snap["_wear_count"] = [dict(r) for r in
                           conn.execute("SELECT id, wear_count FROM items")]
    # 洗衣篮（`data/laundry.json`）也是**真写盘**的 —— 一起快照。
    # 不快照的话，跑一次 apitest 就把用户正在洗的衣服清空了，
    # 而且不报错（只会让人某天发现"我明明洗了三件，怎么又都回来了"）。
    try:
        snap["_laundry_file"] = (laundry.CONFIG_PATH.read_bytes()
                                 if laundry.CONFIG_PATH.exists() else None)
    except OSError:
        snap["_laundry_file"] = None
    return snap


def _restore(conn, snap: dict) -> None:
    for t in _SNAP_TABLES:
        rows = snap.get(t) or []
        conn.execute(f"DELETE FROM {t}")
        if rows:
            cols = list(rows[0])
            ph = ",".join("?" * len(cols))
            conn.executemany(
                f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph})",
                [[r[c] for c in cols] for r in rows])
    for r in snap.get("_wear_count") or []:
        conn.execute("UPDATE items SET wear_count = ? WHERE id = ?",
                     (r["wear_count"], r["id"]))
    conn.commit()

    # 洗衣篮：原本没有这个文件就删掉 —— 别在用户的数据目录里留一个测试造出来的空篮子。
    raw = snap.get("_laundry_file", None)
    try:
        if raw is None:
            laundry.CONFIG_PATH.unlink(missing_ok=True)
        else:
            laundry.CONFIG_PATH.write_bytes(raw)
    except OSError as e:
        print(f"  （洗衣篮还原失败：{e}）")


def _clamp_outfit_seq(conn) -> None:
    """把 outfits 的自增序列夹回 MAX(id)。

    测试为了造场景会显式插 `id=900777`，还原历史时也是按原 id 插回去的 ——
    这两件事都会把 `sqlite_sequence` 顶高，**之后真实保存的搭配就拿到 900778+
    这种假 id**。实测真实库里因此攒出 15 条 id 900791~900805，看着像测试垃圾，
    其实全是用户自己看过的今日推荐。

    功能上无害（id 只要唯一就行），但会让排查的人 —— 包括我自己 ——
    把真实数据误判成脏数据。测试可以造数据，但不该在用户的表里留指纹。
    """
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='outfits'").fetchone()
    if not row:
        return
    mx = int(conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM outfits").fetchone()[0] or 0)
    if int(row[0] or 0) > mx:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name='outfits'", (mx,))
        conn.commit()


def _call(method: str, path: str, body=None, timeout: int = 90):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def _call_status(method: str, path: str, body=None, timeout: int = 90):
    """同 _call，但 4xx/5xx 也把状态码返回来，而不是抛异常 ——
    探"应当返回 404"这类负向行为时必须用这个。"""
    try:
        return _call(method, path, body, timeout)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""


def _upload(path: Path) -> dict:
    """用标准库拼 multipart，不额外装 requests。"""
    boundary = "----capsule" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="probe.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_up(seconds: int = 25) -> bool:
    for _ in range(seconds * 2):
        try:
            _call("GET", "/api/meta", timeout=3)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> None:
    global _SNAP
    out = sys.stdout

    if not wait_up():
        print("服务没起来，先确认 uvicorn 在跑。")
        return

    # 写任何东西之前先拍快照。收尾时精确还原，不做整表 DELETE。
    _SNAP = _snapshot(db.get_conn())
    print(f"[快照]                  outfits {len(_SNAP['outfits'])} · "
          f"outfit_items {len(_SNAP['outfit_items'])} · "
          f"feedback {len(_SNAP['feedback'])} · "
          f"w_item {len(_SNAP['w_item'])} · w_pair {len(_SNAP['w_pair'])} · "
          f"洗衣篮 {len(laundry.ids())} 件（跑完原样还原）")

    st, txt = _call("GET", "/api/meta")
    meta = json.loads(txt)
    print(f"[GET /api/meta]        {st}  槽位 {len(meta['slots'])} · 颜色 {len(meta['colors'])} "
          f"· 场合 {len(meta['occasions'])} · 标签 {len(meta.get('tags') or [])}")
    for t in meta.get("tags") or []:
        print(f"                        标签 · {t['name']}（{t['key']}）限 {'/'.join(t['slots'])}"
              f" —— {t['desc']}")

    for page in ("/", "/wardrobe", "/prebuy"):
        st, txt = _call("GET", page)
        print(f"[GET {page:<19}] {st}  {len(txt)} 字节")

    st, txt = _call("GET", "/api/stats?temp=20&detail=true")
    s = json.loads(txt)
    print(f"[GET /api/stats]       {st}  {s['n_items']} 件 · {s['n_cores']} 个核心 "
          f"（变体 {s['n_variants']}）· 密度 {s['density'] * 100:.0f}% · 效率 {s['efficiency']}")
    print(f"                        全季孤岛 {len(s['islands'])} · 当季不参与 {len(s['inactive'])}")
    # 「组合密度」在体检页被展开成算式，三因子就是这句话的依据 —— 必须自己先自洽。
    f = s.get("capacity_factors")
    if not f:
        _fail("/api/stats 缺 capacity_factors（体检页的密度算式会整块消失）")
    else:
        prod = f["tops"] * f["bottoms"] * f["shoes"]
        print(f"                        算式 {f['tops']}×{f['bottoms']}×{f['shoes']} = {prod}"
              f"（core_capacity {s['core_capacity']}）")
        if prod != s["core_capacity"]:
            _fail(f"capacity_factors 乘积 {prod} != core_capacity {s['core_capacity']}"
                  "（页面会写出一个对不上账的算式）")
    for row in s.get("hinges", [])[:3]:
        print(f"                        枢纽 · {row['name']} −{row['delta_n']} 个核心")

    st, txt = _call("GET", "/api/efficiency-curve")
    curve = json.loads(txt)
    print(f"[GET /api/efficiency-curve] {st}  " +
          " | ".join(f"{c['temp']}℃:{c['n_cores']}个/{c['density'] * 100:.0f}%" for c in curve))

    st, txt = _call("GET", "/api/duplicates")
    dup = json.loads(txt)
    exact = sum(1 for p in dup["pairs"] if p.get("exact"))
    print(f"[GET /api/duplicates]  {st}  可疑 {len(dup['pairs'])} 组"
          + (f"，其中 {exact} 组是像素完全相同的同一张图" if dup["pairs"] else ""))

    # 建一个候选件
    cand = {
        "name": "接口测试·驼色大衣", "slot": "L3", "category": "大衣",
        "stage": "candidate", "color_main": "驼色", "color_hex": "#B08968",
        "is_neutral": 1, "formality": 3, "temp_min": -5, "temp_max": 15,
        "thickness": 4, "collar": "翻领", "length_band": "长", "sleeve": "长袖",
        "fit": "宽松", "material": "羊毛", "purchase_price": 1599,
        "eval_batch_id": 99, "source_url": "https://example.com/item",
    }
    st, txt = _call("POST", "/api/items", cand)
    item = json.loads(txt)
    print(f"[POST /api/items]      {st}  候选件 id={item['id']} stage={item['stage']}")

    st, txt = _call("POST", f"/api/prebuy/{item['id']}", {"save": True})
    r = json.loads(txt)
    print(f"[POST /api/prebuy]     {st}  ΔN={r['delta_n']} (+{round(r['growth'] * 100)}%) "
          f"· 覆盖 {r['seasons']} · 替代率 {r['replace_rate']} · 救活 {r['revived_count']}")
    print(f"                        判定 [{r['verdict']}] {r['verdict_text']}")
    for n in r.get("notes") or []:
        print(f"                        ! {n}")
    print(f"                        搭档 {r['partner_total']} 件，前 5：")
    for p in r["partners"][:5]:
        print(f"                          +{p['count']:>3} 套  {p['name']}")
    st, txt = _call("GET", "/api/prebuy/batch/99")
    b = json.loads(txt)
    print(f"[GET /api/prebuy/batch] {st}  同批 {len(b['candidates'])} 件")

    st, txt = _call("GET", "/api/items?stage=candidate")
    print(f"[GET /api/items]       {st}  候选 {len(json.loads(txt))} 件")

    st, txt = _call("POST", f"/api/items/{item['id']}/reject")
    print(f"[POST reject]          {st}  {txt.strip()}")

    st, txt = _call("DELETE", f"/api/items/{item['id']}")
    print(f"[DELETE /api/items]    {st}  清理完成")

    # ── 自动识别链路 ──
    print()
    print("自动识别链路：")

    st, txt = _call("GET", "/api/vision/config")
    vc = json.loads(txt)
    print(f"[GET /api/vision/config] {st}  enabled={vc['enabled']} configured={vc['configured']} "
          f"· 预设 {len(vc['presets'])} 个")
    assert "api_key" not in vc, "配置接口回传了 api_key 明文"

    st, txt = _call("POST", "/api/vision/analyze", {"items": [{"key": "x", "uid": "deadbeef"}]})
    va = json.loads(txt)
    print(f"[POST /api/vision/analyze] {st}  enabled={va['enabled']} "
          f"（未配置时应当优雅返回，不报错）")

    # 造一张"藏蓝衣服 + 白底"的假照片走一遍上传
    from PIL import Image, ImageDraw
    probe = ROOT / "_apitest_probe.jpg"
    im = Image.new("RGB", (420, 420), (255, 255, 255))
    ImageDraw.Draw(im).rounded_rectangle([84, 42, 336, 378], radius=18, fill=(31, 42, 68))
    im.save(probe, "JPEG", quality=92)
    try:
        up = _upload(probe)
    finally:
        probe.unlink(missing_ok=True)
    col = up["color"]
    print(f"[POST /api/upload]     {st}  uid={up['uid']}")
    print(f"                        本地测色 → {col['name']} {col['hex']} "
          f"（实测 {col['observed_hex']}，占比 {col['share']}，可信 {col['confident']}）")
    # 裁剪必须真的跑过。这个接口曾经把裁剪包在裸 except 里，
    # 而调用的函数名早就改了 —— 于是静默失效一整版，展示图全是没裁过的截图。
    tr = up.get("trim") or {}
    print(f"                        裁剪 → 裁动 {up.get('trimmed')} · 留 {round(tr.get('keep', 1) * 100)}% "
          f"· 外壳占比 {tr.get('chrome_share')} · 有衣服 {col.get('garment_ok')} · 框 {tr.get('box')}")
    if "trim" not in up:
        _fail("上传返回里没有 trim —— 裁剪信息丢了，说明这一步没走到")
    print(f"                        三份图：{up['img_raw'].split('/')[-1].split('_')[-1]} / "
          f"{up['img_display'].split('/')[-1].split('_')[-1]} / "
          f"{up['img_llm'].split('/')[-1].split('_')[-1]}")

    # 批量入库 + 清理。**必须带上图片字段和 tags** —— 这是回归测试：
    # 曾经 ItemIn 漏声明 img_* ，导致上传的衣服入库后全是没有图的空壳。
    meta_defaults = json.loads(_call("GET", "/api/meta")[1])["category_defaults"]
    bulk = [
        {"name": "接口测试·衬衫A", "slot": "L1", "category": "衬衫", **meta_defaults["衬衫"],
         "tags": ["solo_only"],
         "img_raw": up["img_raw"], "img_display": up["img_display"], "img_llm": up["img_llm"]},
        {"name": "接口测试·牛仔裤A", "slot": "L4", "category": "牛仔裤",
         **meta_defaults["牛仔裤"]},
    ]
    st, txt = _call("POST", "/api/items/bulk", bulk)
    b = json.loads(txt)
    print(f"[POST /api/items/bulk] {st}  新建 {b['count']} 件，失败 {len(b['failed'])} 件")

    items = json.loads(_call("GET", "/api/items")[1])
    got = next((i for i in items if i["name"] == "接口测试·衬衫A"), None)
    if got is None:
        _fail("找不到刚入库的单品")
    else:
        if got.get("img_display"):
            print(f"                        图片已落库 → {got['img_display']}")
        else:
            _fail("图片没落库！img_display 为空（ItemIn 是不是又漏声明 img_* 了？）")
        # tags 同理：DB 有列 ≠ 能存，没在 ItemIn 里声明就被 ITEM_FIELDS 静默丢掉
        if "solo_only" in (json.loads(got.get("tags") or "[]")):
            print(f"                        标签已落库 → {got['tags']}")
        else:
            _fail(f"标签没落库！（tags={got.get('tags')!r}，ItemIn 是不是又漏声明了？）")
        st, txt = _call("PATCH", f"/api/items/{got['id']}", {"tags": ["never_outer"]})
        pt = json.loads(txt)
        print(f"[PATCH /api/items]     {st}  tags → {pt.get('tags')!r}")
        if json.loads(pt.get("tags") or "[]") == ["never_outer"]:
            print("                        标签切换正常（数组写法已归一化成 JSON 字符串）")
        else:
            _fail("标签 PATCH 没生效")
    for i in b["created"]:
        _call("DELETE", f"/api/items/{i}")
    print(f"                        已清理 {len(b['created'])} 件")

    # 上传落下的三张图也要收走 —— `db.delete_item()` 按设计**不删图片**（图是归档，
    # 删除留孤儿是刻意的），但测试自己造的东西得自己带走，否则每跑一次就往
    # `data/images/` 里攒一组三张（`checkthick.mjs` 对同一件事有断言）。
    # ⚠️ 删之前先确认库里没有任何一行还引用这个 uid —— 快照还原发生在最后，
    #    此刻不能想当然。
    _uid = up["uid"]
    _imgs = [db.IMG_DIR / f"{_uid}_{s}" for s in ("raw.jpg", "display.webp", "llm.jpg")]
    conn = db.get_conn()
    try:
        _refs = conn.execute(
            "SELECT COUNT(*) FROM items WHERE img_raw LIKE ? OR img_llm LIKE ? "
            "OR img_display LIKE ?", (f"%{_uid}_%",) * 3).fetchone()[0]
    finally:
        conn.close()
    if _refs:
        print(f"                        ⚠️ 还有 {_refs} 件引用这组图，先不删")
    else:
        _gone = []
        for _f in _imgs:
            if _f.exists():
                _f.unlink()
                _gone.append(_f.name)
        print(f"                        上传落下的图已收走 {len(_gone)}/3 张"
              + (f" → {', '.join(_gone)}" if _gone else "（一张都没落盘？）"))

    # ── 删除链路 ──────────────────────────────────────────────────
    # 用户报过「页面上没办法删除」。根因：outfit_items.item_id 少了 ON DELETE
    # 子句，删一件进过搭配的衣服撞外键 → 500，而前端一句裸 `await` 把异常吞掉，
    # 于是"点了什么也没发生"。下面这条路径必须一直绿。
    print()
    print("删除链路：")
    victim = None
    try:
        st, txt = _call("POST", "/api/items",
                        {"name": "__apitest_删除用__", "slot": "L1", "category": "T恤",
                         "stage": "owned", "status": "in_wardrobe"})
        if st != 200:
            _fail(f"造不出测试单品（{st} {txt.strip()[:120]}）")
        else:
            victim = json.loads(txt)["id"]
        # 直接把它挂进一条搭配，制造"被引用"的状态（走 HTTP 造不出来）
        conn = db.get_conn()
        try:
            conn.execute("INSERT INTO outfits (id, date, occasion, temp_actual, source, "
                         "local_score) VALUES (900777, date('now','localtime'), '通勤', 16, "
                         "'local', 80)")
            conn.execute("INSERT OR REPLACE INTO outfit_items (outfit_id, item_id, slot) "
                         "VALUES (900777, ?, 'L1')", (victim,))
            conn.commit()
            n_ref = conn.execute("SELECT COUNT(*) FROM outfit_items WHERE item_id=?",
                                 (victim,)).fetchone()[0]
        finally:
            conn.close()
        print(f"[造场景]               单品 {victim} 被挂进 1 条搭配（引用 {n_ref} 处）")
        if n_ref != 1:
            _fail("没能造出「被引用」的状态，下面的结论不可信")

        st, txt = _call("DELETE", f"/api/items/{victim}")
        print(f"[DELETE /api/items]    {st}  {txt.strip()[:80]}")
        if st == 200:
            print("                        删「进过搭配」的单品成功 —— 就是这一步以前 500")
        else:
            _fail("删除失败！outfit_items 的外键是不是又少 ON DELETE CASCADE 了？")

        conn = db.get_conn()
        try:
            left_item = conn.execute("SELECT 1 FROM items WHERE id=?", (victim,)).fetchone()
            left_ref = conn.execute("SELECT 1 FROM outfit_items WHERE item_id=?",
                                    (victim,)).fetchone()
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            print(f"                        残留：items {'有' if left_item else '无'} · "
                  f"搭配引用 {'有' if left_ref else '无'} · "
                  f"外键检查{'干净' if not bad else f'{len(bad)} 条违规'}")
            if left_item or left_ref or bad:
                _fail("删除后有残留")
        finally:
            conn.close()

        st, txt = _call_status("DELETE", "/api/items/999999")
        print(f"[DELETE 不存在的 id]   {st}  （期望 404，不是假装成功）")
        if st != 404:
            _fail("删不存在的 id 应当返回 404")
    except Exception as e:                      # 别让这一段把整个冒烟测试带崩
        _fail(f"删除链路检查异常：{type(e).__name__}: {e}")
    finally:
        conn = db.get_conn()
        try:
            if victim:
                conn.execute("DELETE FROM items WHERE id=?", (victim,))
            conn.execute("DELETE FROM outfits WHERE id = 900777")
            conn.commit()
        finally:
            conn.close()

    # ── 今日推荐 + 反馈 ──
    print()
    print("今日推荐：")
    st, txt = _call("GET", "/api/today?temp=20&occasion=%E9%80%9A%E5%8B%A4&n=3&use_llm=false")
    t = json.loads(txt)
    if t.get("empty"):
        print(f"[GET /api/today]       {st}  empty（{t.get('reason')}）")
    else:
        print(f"[GET /api/today]       {st}  在库 {t['n_items']} 件 · "
              f"核心组合 {t['cores']} 个（变体 {t['variants']}）· "
              f"选出 {len(t['outfits'])} 套 · 模型排序={t['llm_used']}")
        if t.get("inactive"):
            print(f"                        这个温度穿不上 {len(t['inactive'])} 件："
                  + "、".join(f"{x['name']}({x['temp_min']}~{x['temp_max']}℃)"
                              for x in t["inactive"][:4]))
        for i, o in enumerate(t["outfits"], 1):
            print(f"                        {i}. [{o['score']} 分] "
                  + " ＋ ".join(it["name"] for it in o["items"]))
            print(f"                           {o['reason']}")

        first = t["outfits"][0]
        st, txt = _call("POST", "/api/today/feedback",
                        {"outfit_id": first["outfit_id"], "gesture": "rejected"})
        print(f"[POST feedback]        {st}  已记录 rejected，影响 {len(json.loads(txt)['items'])} 件")

        st, txt = _call("GET", "/api/today/stats")
        s = json.loads(txt)
        print(f"[GET /api/today/stats] {st}  总反馈 {s['total']} 条 "
              f"· 降权单品 {len(s['down'])} 件 · 成对冲突 {len(s['conflicts'])} 对")

        st, txt = _call("GET", "/api/today/history")
        h = json.loads(txt)
        print(f"[GET today/history]    {st}  最近 {h['days']} 天穿了 {h['count']} 件次")

        # ── 洗衣篮：洗了的件不进今日推荐，恢复之后原样回来 ──
        # 它**真写 data/laundry.json**，所以上面有快照、下面有还原。
        # 「洗了的件真的消失」这条性质由 scripts/test_wash.py 钉死（那个不用起服务）；
        # 这里验的是 HTTP 通路本身：端点在不在、参数名对不对、字段名对不对。
        # ⚠️ 洗衣篮是**用户的真实状态** —— 不能假设"跑测试时篮子是空的"，
        #    否则用户真洗了一件，这条断言就变红（而且红得莫名其妙）。
        basket_before = set(laundry.ids())
        victim = t["outfits"][0]["items"][0]
        st, txt = _call("POST", "/api/laundry", {"item_id": victim["id"]})
        w = json.loads(txt)
        print(f"[POST /api/laundry]    {st}  洗了「{victim['name']}」→ {w['washed_ids']}")
        if victim["id"] not in (w.get("washed_ids") or []):
            _fail("POST /api/laundry 没把它记进洗衣篮（参数名是不是改了？）")

        expect_washed = len(basket_before | {victim["id"]})
        st, txt = _call("GET", "/api/today?temp=20&save=0&use_llm=false")
        t2 = json.loads(txt)
        still = [x["name"] for o in (t2.get("outfits") or []) for x in o["items"]
                 if x["id"] == victim["id"]]
        print(f"[GET /api/today]       {st}  n_washed={t2.get('n_washed')}"
              f"（原有 {len(basket_before)} 件 + 刚洗这 1 件 = {expect_washed}） · "
              f"推荐里还有被洗的那件：{'有！' if still else '没有'}")
        if still:
            _fail(f"洗了的「{victim['name']}」仍然出现在今日推荐里")
        if t2.get("n_washed") != expect_washed:
            _fail(f"`n_washed` 应当是 {expect_washed}，实际 {t2.get('n_washed')}")
        if t2.get("n_items") != t["n_items"]:
            _fail("`n_items` 应当是衣橱总件数 —— 洗了不该让它变小（看着像衣服丢了）")

        st, txt = _call("POST", "/api/laundry",
                        {"item_id": victim["id"], "washed": False})
        print(f"[POST /api/laundry]    {st}  单件放回 → {json.loads(txt)['washed_ids']}")

        st, txt = _call("POST", "/api/laundry/reset")
        r = json.loads(txt)
        print(f"[POST laundry/reset]   {st}  全部恢复 → {r['washed_ids']}")
        if r.get("washed_ids"):
            _fail("reset 之后洗衣篮应当是空的")

        # 精确还原测试前的状态（快照在 main() 开头就取了）。
        # 绝不整表 DELETE —— 那会把用户的搭配历史和学习到的权重一起抹掉。
        conn = db.get_conn()
        try:
            _restore(conn, _SNAP)
            _clamp_outfit_seq(conn)
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            n_oi = conn.execute("SELECT COUNT(*) FROM outfit_items").fetchone()[0]
            orphans = conn.execute(
                "SELECT COUNT(*) FROM outfit_items oi "
                "WHERE NOT EXISTS (SELECT 1 FROM outfits o WHERE o.id = oi.outfit_id)"
            ).fetchone()[0]
            print(f"                        已还原测试前的数据"
                  f"（outfit_items {n_oi} 行 / 孤儿 {orphans} · "
                  f"外键检查{'干净' if not bad else f'有 {len(bad)} 条违规！'}）")
        finally:
            conn.close()
        _SNAP = None

    if _FAILS:
        # 结论必须跟着断言走。以前这里无条件打印「全部通过。」，
        # 于是不管挂了几条，那一行都是绿的。
        print(f"\n✗ {len(_FAILS)} 项没过：")
        for f in _FAILS:
            print(f"   - {f}")
        raise SystemExit(1)
    print("\n全部通过。")


if __name__ == "__main__":
    out_path = None
    argv = sys.argv[1:]
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            out_path = argv[i + 1]
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            BASE = _resolve_base(int(argv[i + 1]))
    if out_path:
        fh = open(out_path, "w", encoding="utf-8")
        sys.stdout = fh
    try:
        main()
    except BaseException:
        # ⚠️ 原来这里是**裸 `try/finally`**：异常一路抛出去，报告里却只剩
        # 最后那句「（测试中断，已还原到运行前的数据）」—— **看不到任何原因**。
        # 而 `--out` 是把 stdout 重定向进文件的，traceback 默认走 stderr，
        # 于是"为什么红"根本没落进那份报告（批量跑时 stderr 通常还被丢掉）。
        # 这本身就是本项目最怕的静默失败：灯红了，红在哪全靠猜。
        # 现在显式写进 stdout；异常继续抛（退出码仍是 1，不改变原有语义）。
        print("\n✗ 测试中途抛异常，原因如下：")
        traceback.print_exc(file=sys.stdout)
        raise
    finally:
        if _SNAP is not None:            # main() 中途崩了也要还原
            try:
                _c = db.get_conn()
                try:
                    _restore(_c, _SNAP)
                    _clamp_outfit_seq(_c)
                finally:
                    _c.close()
                print("\n（测试中断，已还原到运行前的数据）")
            except Exception as e:
                print(f"\n（测试中断，还原失败：{e} —— 备份在 data/ 下）")
        if out_path:
            fh.close()
