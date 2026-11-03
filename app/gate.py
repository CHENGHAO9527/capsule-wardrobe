"""口令门 · **只在「配置了口令」时启用**。

═══════════════════════════════════════════════════════════════════
为什么判据是「有没有配口令」，而不是「请求是不是来自本机」？

    云端跑在反向代理后面，`request.client.host` 拿到的是**代理自己**的地址 ——
    而它常常正好是 `127.0.0.1`。照着它放行，等于「本机免密」在公网上把门敞开，
    **而且不报任何错**。这正是本项目第 ⑲ 类静默失败：看起来一切正常，门其实是开的。

    所以这里只留一个判据：

      · 本机开发：**没有 `data/gate.json`** → 门不启用，行为与加它之前一字不差
      · 云端：**有这个文件** → 所有人（含你自己）都要先过门

    ⚠️ 注意"有这个文件"和"文件是好的"是**两件事**：
       文件在、但读不出可用口令 → **关死**（谁都进不去），
       并且登录页直接写明"口令文件坏了"。
       **绝不因为"读不出来"就把门打开** —— 那才是真正的灾难：
       一次手滑的编辑 = 衣柜挂公网上且毫无提示。

口令从哪来（先命中先用）
    1. 环境变量 `CLOSET_PASSWORD`
    2. `data/gate.json` 的 `password`（字符串）
    改完不用重启：按 mtime 重新读。

放行的路径（都是"还没进门就得拿到"的东西）
    /login                           登录页 + 提交口
    /logout                          退出（清 cookie）
    /api/ping                        存活探测（serve.py / 自检脚本 / 发布平台的健康检查）
    /static/css/app.css              登录页复用全站那一份样式（视觉系统只有一份）
    /static/manifest.webmanifest
    /static/icon-*                   图标，免得控制台一堆 401
═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

GATE_FILE = Path(__file__).resolve().parent.parent / "gate.json"

# ⚠️ 口令文件放**项目根**（源码），**不放 `data/`**（应用状态）。
#    理由：`data/` 是会被重写的那一半。要是云沙箱重新发布时把 `data/` 清了，
#    口令文件跟着消失 = **门自动打开**、而公网地址还在 —— 最糟的组合。
#    放在源码里就跟着每次上传走，永远在。
#    第二个候选路径只是兼容：万一有人把口令放 data/ 里，也认。
GATE_CANDIDATES = (GATE_FILE, Path(__file__).resolve().parent.parent / "data" / "gate.json")

COOKIE_NAME = "closet_gate"
_SALT = b"closet-gate-v1"
_MAX_AGE = 400 * 24 * 3600          # 400 天 —— 手机上别老让你重新登
_FAIL_DELAY = 0.6                   # 口令错时的刹车，压一下脚本穷举

_OPEN_EXACT = {"/login", "/logout", "/api/ping"}
_OPEN_PREFIX = ("/static/css/app.css", "/static/manifest.webmanifest", "/static/icon-")

_cache: dict = {"mtime": None, "pw": None}


# ══════════════════════════════════════════════
# 状态
# ══════════════════════════════════════════════

def state() -> dict:
    """{"enabled": 门开不开, "pw": 口令或 None, "broken": 文件在但读不出}

    `CLOSET_GATE=off` 是**本机启动器专用的关闸**（见 `scripts/serve.py`）。
    为什么需要它：口令文件在项目根，本机跑的和云上跑的是**同一份代码、同一个文件** ——
    没有这个开关，本机想进门也得先输口令，而坐在自己电脑前的人不该被自己的门拦下。
    云上不设这个变量，所以云上照常要口令。
    """
    if (os.environ.get("CLOSET_GATE") or "").strip().lower() in ("off", "0", "false", "no"):
        return {"enabled": False, "pw": None, "broken": False}

    env = (os.environ.get("CLOSET_PASSWORD") or "").strip()
    if env:
        return {"enabled": True, "pw": env, "broken": False}

    target: Optional[Path] = None
    stamp = 0
    for cand in GATE_CANDIDATES:
        try:
            stamp = cand.stat().st_mtime_ns
        except OSError:
            continue
        target = cand
        break
    if target is None:
        # 一个候选文件都不在 —— 本机常态，门不启用
        return {"enabled": False, "pw": None, "broken": False}

    key = (str(target), stamp)
    if _cache["mtime"] != key:
        pw: Optional[str] = None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            raw = data.get("password") if isinstance(data, dict) else None
            pw = str(raw).strip() if isinstance(raw, str) and raw.strip() else None
        except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
            pw = None
        _cache["mtime"], _cache["pw"] = key, pw

    pw = _cache["pw"]
    # 文件在 → 门必须启用。读不出可用口令 → broken（**关死**，不是敞开）。
    return {"enabled": True, "pw": pw, "broken": pw is None}


def where() -> Optional[Path]:
    """当前生效的口令文件（没配则 None）。给状态查看用。"""
    for cand in GATE_CANDIDATES:
        if cand.exists():
            return cand
    return None


def token(pw: str) -> str:
    """cookie 值：由口令派生的 HMAC。

    刻意做成**确定性**的（不随机）—— 云沙箱重启后不会把你踢出去。
    它不可反推口令，也不带任何权限信息。
    """
    return hmac.new(pw.encode("utf-8"), _SALT, hashlib.sha256).hexdigest()


def _cookie_ok(request: Request, pw: str) -> bool:
    got = request.cookies.get(COOKIE_NAME) or ""
    return hmac.compare_digest(got, token(pw))


def _is_open(path: str) -> bool:
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX)


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return fwd == "https"


def _safe_next(raw: Optional[str]) -> str:
    """只接受站内路径。挡住 `//evil.com` 那种开放重定向 ——
    门是我加的，不能顺手变成把口令递给别人的跳板。"""
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


async def _fields(request: Request) -> dict:
    """把提交体解析成 dict。解析失败 = 空 dict（→ 显示"口令不对"），
    **不是**吞掉功能调用，也不会把任何错误变成"通过"。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    try:
        form = await request.form()          # Starlette 会缓存，重复调用安全
    except Exception:
        return {}
    return {k: form.get(k) for k in ("password", "next")}


# ══════════════════════════════════════════════
# 登录页（复用 /static/css/app.css，不另起一套视觉）
# ══════════════════════════════════════════════

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f4f0ea" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1a1917" media="(prefers-color-scheme: dark)">
<meta name="robots" content="noindex,nofollow">
<title>衣橱 · 口令</title>
<link rel="stylesheet" href="/static/css/app.css">
<style>
  /* 这屏不进底栏。卡片放视线偏上 —— 垂直正中在手机上会被键盘顶飞。 */
  .gate-wrap { max-width: 360px; margin: 0 auto; padding: 16vh 16px 24px; }
  .gate-title { font-family: var(--serif); font-size: 23px; font-weight: 400;
                letter-spacing: .10em; color: var(--text); margin: 0 0 6px; }
  .gate-sub { font-size: 12.5px; letter-spacing: .04em; color: var(--text3);
              margin: 0 0 18px; line-height: 1.75; }
  /* 触屏输入框必须 16px：小于 16px 时 iOS 会在聚焦瞬间把整页放大 */
  .gate-input { font-size: 16px; letter-spacing: .06em; }
  .gate-btn { margin-top: 12px; }
  .gate-err { font-size: 12.5px; letter-spacing: .04em; color: var(--red);
              margin: 12px 0 0; line-height: 1.7; }
</style>
</head>
<body>
<div class="wrap gate-wrap">
  <div class="card">
    <div class="gate-title">衣橱</div>
    <p class="gate-sub">__SUB__</p>
    __FORM__
    __ERR__
  </div>
</div>
</body>
</html>
"""

_FORM = """<form method="post" action="/login">
      <input type="hidden" name="next" value="__NEXT__">
      <input class="gate-input" type="password" name="password" placeholder="口令"
             autocomplete="current-password" autofocus enterkeyhint="go" required>
      <button class="btn primary block gate-btn" type="submit">进入</button>
    </form>"""


def login_page(next_path: str = "/", error: str = "", broken: bool = False) -> str:
    if broken:
        sub = "口令文件读不出来（`data/gate.json`）。<br>门是关着的 —— 修好它才能进。"
        form = ""
    else:
        sub = "这个地址在外面，先对个口令。<br>对过一次，这台设备就不用再对了。"
        form = _FORM.replace("__NEXT__", html.escape(next_path, quote=True))
    err = f'<p class="gate-err">{html.escape(error)}</p>' if error else ""
    return (_PAGE
            .replace("__SUB__", sub)
            .replace("__FORM__", form)
            .replace("__ERR__", err))


# ══════════════════════════════════════════════
# 装上
# ══════════════════════════════════════════════

def install(app) -> None:
    """挂到 FastAPI 上。门没配时整段直接穿透：零开销、零行为变化。"""

    st = state()
    if st["enabled"]:
        print(f"[gate] 口令门已启用{'（⚠️ 口令文件损坏，当前关死）' if st['broken'] else ''}",
              flush=True)
    elif where() is not None:
        # 有口令文件却被关掉了 ⇒ 只能是本机启动器显式关的。**说出来**，
        # 免得哪天在这台机器上按"云上那样"启动（没设开关）时以为门开着 —— 那是敞开的。
        print("[gate] 口令文件在，但被本机启动器显式关掉了（CLOSET_GATE=off）→ 本机免密。"
              "云上不设这个开关，照常要口令。", flush=True)

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        st = state()
        if not st["enabled"]:
            return await call_next(request)

        path = request.url.path

        if path == "/logout":
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp

        if path == "/login" and request.method == "POST":
            if st["broken"]:
                return HTMLResponse(login_page(broken=True), status_code=200)
            fields = await _fields(request)
            nxt = _safe_next(str(fields.get("next") or ""))
            supplied = str(fields.get("password") or "")
            if hmac.compare_digest(supplied, st["pw"]):
                resp = RedirectResponse(nxt, status_code=303)
                resp.set_cookie(COOKIE_NAME, token(st["pw"]), max_age=_MAX_AGE,
                                httponly=True, samesite="lax", path="/",
                                secure=_is_https(request))
                return resp
            await asyncio.sleep(_FAIL_DELAY)
            return HTMLResponse(login_page(nxt, "口令不对"), status_code=200)

        if _is_open(path):
            return await call_next(request)

        if st["pw"] and _cookie_ok(request, st["pw"]):
            return await call_next(request)

        # ── 未登录 ──
        if path.startswith("/api/") or path.startswith("/img/"):
            return JSONResponse({"ok": False, "error": "未登录", "need_login": True},
                                status_code=401)
        # 页面：**返回 200 + 登录页**，不是 302。
        # 302 会让"看状态码"的东西（发布平台健康检查、浏览器预取、链接预览）判成异常；
        # 200 + 登录页 才是人眼和机器都读得懂的答案。
        return HTMLResponse(login_page(path, broken=st["broken"]), status_code=200)
