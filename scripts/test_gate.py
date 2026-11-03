"""口令门自检 —— 门是安全边界，必须有断言盯着它，不能靠"看着对"。

    python scripts/test_gate.py
    python scripts/test_gate.py --out scripts/test_gate.out.txt

**它自己起一个临时服务**（端口现找、口令走 `CLOSET_PASSWORD` 环境变量），
所以：不需要你先把服务跑起来、不碰你正在跑的那个、也不碰你的 `gate.json`。
读完自己收摊（`finally` 里 kill 子进程）。

分两段：
  A. 纯函数/状态（不起服务）—— 重点验"文件坏了会不会把门悄悄打开"
  B. 真发 HTTP（起临时服务）—— 重点验"没登进来到底看得见什么"
  C. 发布闸（不起服务）—— 重点验"没有口令时 `stage_deploy` 拒不拒绝发布"
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gate  # noqa: E402

PY = sys.executable
PW = "test-gate-pw-9f3a"
_FAILS: list[str] = []


def _fail(msg: str) -> None:
    _FAILS.append(msg)
    print(f"  FAIL {msg}")


def _ok(msg: str) -> None:
    print(f"  ok   {msg}")


# ══════════════════════════════════════════════
# A. 状态机（不起服务）
# ══════════════════════════════════════════════

def part_a() -> None:
    print("\n[A] 口令状态：文件在与'文件是好的'是两件事")
    real = gate.GATE_CANDIDATES
    saved_cache = dict(gate._cache)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        good = tmp / "gate.json"
        broken = tmp / "gate.json"

        # ① 没有文件 → 门关着（本机常态，行为与加门之前一致）
        gate.GATE_CANDIDATES = (tmp / "nope.json",)
        gate._cache.update({"mtime": None, "pw": None})
        s = gate.state()
        if s["enabled"]:
            _fail("没有口令文件时门却是开的")
        else:
            _ok("没有口令文件 → 门关着（本机行为不变）")

        # ② 好文件 → 门开着，口令对得上
        good.write_text(json.dumps({"password": "abc12345"}), encoding="utf-8")
        gate.GATE_CANDIDATES = (good,)
        gate._cache.update({"mtime": None, "pw": None})
        s = gate.state()
        if s["enabled"] and s["pw"] == "abc12345" and not s["broken"]:
            _ok("口令文件正常 → 门开着")
        else:
            _fail(f"口令文件正常的判定不对：{s}")

        # ③ 坏文件（JSON 语法错）→ **必须关死**，绝不能敞开
        broken.write_text("{ 这不是 json", encoding="utf-8")
        gate.GATE_CANDIDATES = (broken,)
        gate._cache.update({"mtime": None, "pw": None})
        s = gate.state()
        if s["enabled"] and s["broken"] and s["pw"] is None:
            _ok("文件坏了 → 门关死（不是敞开）")
        else:
            _fail(f"文件坏了竟没关死：{s}")

        # ④ 文件在但没 password 键 → 同样关死
        broken.write_text(json.dumps({"other": 1}), encoding="utf-8")
        os.utime(broken, (time.time() + 2, time.time() + 2))   # 骗过 mtime 缓存
        gate.GATE_CANDIDATES = (broken,)
        s = gate.state()
        if s["enabled"] and s["broken"]:
            _ok("文件里没有 password → 门关死")
        else:
            _fail(f"缺 password 键时判定不对：{s}")

        # ⑤ password 不是字符串（比如写成了数字）→ 关死
        broken.write_text(json.dumps({"password": 12345678}), encoding="utf-8")
        os.utime(broken, (time.time() + 4, time.time() + 4))
        gate.GATE_CANDIDATES = (broken,)
        s = gate.state()
        if s["enabled"] and s["broken"]:
            _ok("password 不是字符串 → 门关死")
        else:
            _fail(f"password 类型不对时判定不对：{s}")

    gate.GATE_CANDIDATES = real
    gate._cache.update(saved_cache)

    # ⑥⑦ 环境变量这一组，**全程只在自己的临时目录里判断**。
    #   ⚠️ 第一版这里直接读 `gate.state()`，于是断言变成了"这台机器上有没有 gate.json"——
    #   我刚配好云端口令，它就红了。**断言依赖跑测机器的偶然状态**是本项目第 ⑩ 条明令禁止的，
    #   所以候选路径一律指向临时文件，真实 gate.json 一眼都不看。
    with tempfile.TemporaryDirectory() as td:
        some = Path(td) / "gate.json"
        ghost = Path(td) / "ghost.json"
        some.write_text(json.dumps({"password": "file-pw-xyz"}), encoding="utf-8")

        gate.GATE_CANDIDATES = (some,)
        gate._cache.update({"mtime": None, "pw": None})
        os.environ["CLOSET_PASSWORD"] = "from-env-123"
        s = gate.state()
        if s["enabled"] and s["pw"] == "from-env-123":
            _ok("环境变量优先于文件")
        else:
            _fail(f"环境变量没生效：{s}")

        # 本机启动器的关闸。**本机免密全靠它** —— 口令文件在项目根，本机与云上读同一份，
        # 没有这个开关，坐在自己电脑前的人也要先输口令。
        os.environ["CLOSET_GATE"] = "off"
        if not gate.state()["enabled"]:
            _ok("CLOSET_GATE=off 能关掉门（本机免密靠它）")
        else:
            _fail("本机关闸没生效，本机也会被拦")
        os.environ.pop("CLOSET_GATE", None)

        # 去掉环境变量 → 回到"看文件"：文件在 → 开着
        os.environ.pop("CLOSET_PASSWORD", None)
        gate.GATE_CANDIDATES = (some,)
        gate._cache.update({"mtime": None, "pw": None})
        if gate.state()["enabled"]:
            _ok("去掉环境变量 → 回到看文件（文件在 → 开着）")
        else:
            _fail("口令文件在，去掉环境变量后却判成关着")

        # 换成一个不存在的路径 → 关着（本机常态）
        gate.GATE_CANDIDATES = (ghost,)
        gate._cache.update({"mtime": None, "pw": None})
        if not gate.state()["enabled"]:
            _ok("没有口令文件 → 门关着")
        else:
            _fail("没有口令文件却判成开着")

    gate.GATE_CANDIDATES = real
    gate._cache.update(saved_cache)

    # ⑦ cookie 值确定性（沙箱重启不该把用户踢出去）
    if gate.token("x") == gate.token("x"):
        _ok("cookie 值确定（重启后登录态还在）")
    else:
        _fail("cookie 值不稳定")

    # ⑧ 反推不出原文
    if "x" not in gate.token("x"):
        _ok("cookie 值不可反推口令")
    else:
        _fail("cookie 值里带着口令原文")

    # ⑨ 开放重定向要挡住
    for bad in ("//evil.com", "http://evil.com", ""):
        if gate._safe_next(bad) != "/":
            _fail(f"_safe_next 放过了 {bad!r}")
    if gate._safe_next("/plan?x=1") == "/plan?x=1":
        _ok("next 只收站内路径（挡开放重定向）")
    else:
        _fail("_safe_next 把合法的站内路径也拦了")


# ══════════════════════════════════════════════
# B. 真发 HTTP（临时服务）
# ══════════════════════════════════════════════

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """要看 303 本身，不要被自动跳走。"""

    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(opener, url, method="GET", body=None, cookie=None, ctype=None):
    """→ (状态码, 头, 正文)。**头一律归一化成小写键**：
    uvicorn 回的是小写（`set-cookie`），写成 `.get("Set-Cookie")` 会拿到 None ——
    于是测试报"应用没发 cookie"，而其实是测试自己没看见。"""
    req = urllib.request.Request(url, data=body, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    if ctype:
        req.add_header("Content-Type", ctype)

    def norm(hs):
        return {k.lower(): v for k, v in hs.items()}

    try:
        with opener.open(req, timeout=8) as r:
            return r.status, norm(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, norm(e.headers), e.read().decode("utf-8", "replace")


def part_b() -> None:
    print("\n[B] 真发 HTTP：没登进来的人看到什么")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, CLOSET_PASSWORD=PW, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    log = open(ROOT / "scripts" / "_gate_test_server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                code, _, _ = _call(opener, f"{base}/api/ping")
                if code == 200:
                    ready = True
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.4)
        if not ready:
            _fail("临时服务没起来（看 scripts/_gate_test_server.log）")
            return
        _ok(f"临时服务起来了（:{port}，口令走环境变量）")

        # ① 首页未登录 → **200 + 登录页**（不是 302，也别把内容漏出去）
        code, hdrs, body = _call(opener, f"{base}/")
        if code == 200 and "type=\"password\"" in body and "口令" in body:
            _ok("未登录 GET / → 200 + 登录页")
        else:
            _fail(f"未登录 GET / 拿到 {code}，是登录页吗？{body[:80]!r}")
        if "tempv" in body or "今日" in body:
            _fail("首页内容在未登录时就漏出去了")
        else:
            _ok("未登录看不到首页内容")

        # ② 接口 → 401 JSON（前端能识别，不会当成 HTML 去 parse）
        code, _, body = _call(opener, f"{base}/api/meta")
        if code == 401 and "\"need_login\"" in body:
            _ok("未登录 GET /api/meta → 401 + need_login")
        else:
            _fail(f"未登录接口返回 {code} / {body[:80]!r}")

        # ③ 图片 → 401（别返回 HTML 当图）
        code, _, _ = _call(opener, f"{base}/img/nope_raw.jpg")
        if code == 401:
            _ok("未登录取图片 → 401")
        else:
            _fail(f"未登录取图片返回 {code}")

        # ④ 存活探测免密（否则自检脚本与发布平台健康检查全死）
        code, _, _ = _call(opener, f"{base}/api/ping")
        if code == 200:
            _ok("/api/ping 免密可达")
        else:
            _fail(f"/api/ping 被门挡了（{code}）")

        # ⑤ 登录页自己要的那份样式必须放行 —— 否则登录页是一片没样式的裸 HTML
        code, _, _ = _call(opener, f"{base}/static/css/app.css")
        if code == 200:
            _ok("/static/css/app.css 放行（登录页能穿衣服）")
        else:
            _fail(f"登录页的样式被挡住了（{code}）")

        # ⑥ 但 /static 下的页面不许放行（那是应用本体）
        code, _, _ = _call(opener, f"{base}/static/today.html")
        if code == 200 and "type=\"password\"" in _call(opener, f"{base}/static/today.html")[2]:
            _ok("/static/<页面>.html 仍要过门")
        else:
            _fail("/static 下的页面被放行了")

        # ⑦ 错口令 → 200 + 明说口令不对，且不发 cookie
        code, hdrs, body = _call(opener, f"{base}/login", "POST",
                                 body=b"password=wrong-pw",
                                 ctype="application/x-www-form-urlencoded")
        if code == 200 and "口令不对" in body and "set-cookie" not in hdrs:
            _ok("错口令 → 明说口令不对、不发 cookie")
        else:
            _fail(f"错口令的处理不对：{code} / set-cookie={hdrs.get('Set-Cookie')!r}")

        # ⑧ 对口令 → 303 + cookie，属性齐
        code, hdrs, _ = _call(opener, f"{base}/login", "POST",
                              body=f"password={PW}".encode(),
                              ctype="application/x-www-form-urlencoded")
        cookie = hdrs.get("set-cookie", "").split(";")[0]
        low = (hdrs.get("set-cookie") or "").lower()
        if code == 303 and cookie and "httponly" in low and "samesite=lax" in low:
            _ok("对口令 → 303 + HttpOnly/SameSite=Lax 的 cookie")
        else:
            _fail(f"登录成功但应答不对：{code} / {hdrs.get('set-cookie')!r}")

        # ⑨ 带 cookie → 拿到真首页
        code, _, body = _call(opener, f"{base}/", cookie=cookie)
        if code == 200 and ("tempv" in body or "今日" in body) and "type=\"password\"" not in body:
            _ok("带 cookie → 真首页")
        else:
            _fail(f"带 cookie 仍拿不到首页：{code} / {body[:80]!r}")

        # ⑩ JSON 提交也认（脚本用）
        code, hdrs2, _ = _call(opener, f"{base}/login", "POST",
                               body=json.dumps({"password": PW}).encode(),
                               ctype="application/json")
        if code == 303:
            _ok("JSON 提交口令也认")
        else:
            _fail(f"JSON 提交没认：{code}")

        # ⑪ next 只收站内
        code, hdrs3, _ = _call(opener, f"{base}/login", "POST",
                               body=b"password=" + PW.encode() + b"&next=%2F%2Fevil.com",
                               ctype="application/x-www-form-urlencoded")
        loc = hdrs3.get("location", "")
        if code == 303 and loc == "/":
            _ok("next=//evil.com 被拉回站内")
        else:
            _fail(f"开放重定向没挡住：Location={loc!r}")

        # ⑫ /logout 清 cookie
        code, hdrs4, _ = _call(opener, f"{base}/logout")
        if code == 303 and "max-age=0" in (hdrs4.get("set-cookie") or "").lower():
            _ok("/logout 清掉 cookie")
        else:
            _fail(f"/logout 没清 cookie：{hdrs4.get('set-cookie')!r}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def part_c() -> None:
    """发布闸：**没有口令就不许发布**。

    这一条只能在发布时拦得住 —— 本机免密是设计（坐在自己电脑前不该被自己的门拦下），
    所以"没门"在本机看不出任何异样。忘了设口令 + 发布成功 + 页面上一切正常
    = 衣柜挂公网上任人读写，而**没有任何一处会报错**。

    ⚠️ 断言只在**临时目录**里判断：真实的 `data/` 和你的 `gate.json` 一眼都不看
    （第 ⑩ 条）。所以下面把 `GATE_FILE`/`MANIFEST`/`STAGE` 全指到 temp 里，
    并且把"要搬的东西"清空 —— 就算闸门失灵，它也没有东西可搬。
    """
    print("\n[C] 发布闸：没有口令不许发布")
    import importlib.util

    spec = importlib.util.spec_from_file_location("_stage_deploy",
                                                 ROOT / "scripts" / "stage_deploy.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ① 口令文件不在 → 必须拒绝，而且必须**在搬任何东西之前**就拒绝
        m.GATE_FILE = tmp / "nope.json"
        m.MANIFEST = tmp / "_manifest.json"
        before = sorted(p.name for p in ROOT.iterdir())
        rc = m.stage(no_data=True)
        after = sorted(p.name for p in ROOT.iterdir())
        if rc != 0 and before == after and not m.MANIFEST.exists():
            _ok("没有 gate.json → 拒绝发布，且项目目录一个文件都没动")
        else:
            _fail(f"没有 gate.json 时没拦住（rc={rc}，动了东西={before != after}）")

        # ② 口令文件在 → 闸门放行。**(这段绝不真的搬 data/)**
        #    做法：把 STAGE/MANIFEST 指到 temp，并把 MOVES/GLOBS/SANITIZE 清空 ——
        #    这样"放行"是真的走一遍 stage()，但它手上没有可搬的东西。
        good = tmp / "gate.json"
        good.write_text(json.dumps({"password": "x" * 12}), encoding="utf-8")
        m.GATE_FILE = good
        m.STAGE = tmp / "stage"
        m.MOVES, m.GLOBS, m.SANITIZE = [], [], []
        rc = m.stage(no_data=False)
        if rc == 0 and m.MANIFEST.exists():
            _ok("有 gate.json → 放行（走的是一次空搬运）")
        else:
            _fail(f"有 gate.json 却被拦了：rc={rc}")

        # ③ 显式 --allow-no-gate 才允许无门发布（且这条要**明说**）
        m.GATE_FILE = tmp / "nope.json"
        m.MANIFEST = tmp / "_manifest2.json"
        rc = m.stage(no_data=False, allow_no_gate=True)
        if rc == 0 and m.MANIFEST.exists():
            _ok("--allow-no-gate 才放行无门发布（默认拦住，故意的）")
        else:
            _fail(f"--allow-no-gate 没生效：rc={rc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", metavar="PATH", help="把输出也写一份到这个文件（UTF-8）")
    args = ap.parse_args()

    buf: list[str] = []
    orig = print

    def tee(*a, **kw):
        s = " ".join(str(x) for x in a)
        buf.append(s)
        orig(*a, **kw)

    globals()["print"] = tee
    started = datetime.now()
    tee(f"# test_gate · 生成于 {started:%Y-%m-%d %H:%M:%S}")
    part_a()
    part_b()
    part_c()

    tee("")
    if _FAILS:
        tee(f"❌ {len(_FAILS)} 项没过：")
        for f in _FAILS:
            tee(f"   - {f}")
    else:
        tee("✅ 全部通过。")

    if args.out:
        Path(args.out).write_text("\n".join(buf) + "\n", encoding="utf-8")
    return 1 if _FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
