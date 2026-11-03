"""线上验收：发布之后，用一条命令确认云上那份"门挡得住、数据在、写得进"。

它**不打本机服务**，只打线上网址 —— 所以跟那 20 项自检是分开的，**不算进那 20 项**
（那 20 项要能离线/本机跑；这个必须有网 + 云上活着）。

用法：
  python scripts/checklive.py                 # 默认打已发布的网址
  python scripts/checklive.py --url https://xxx.app.workbuddy.host
  python scripts/checklive.py --recheck       # 重新发布之后只复查"数据还在不在"

口令从 `gate.json` 读（不写死在这里）—— 也不要用 `--pw` 把口令写进命令行历史。
⚠️ 它会在云上**真的改一次** `comfort`（改成 21.5）来证明"写得进"，**写完立刻还原**（快照→还原）。
`--recheck` 一个字都不写。
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 换部署就设环境变量 `CLOSET_URL`，**别改代码**：这份源码是要分享出去的，
# 里面不该钉死谁的域名。
DEFAULT_URL = os.environ.get("CLOSET_URL") or "https://your-app.example.com"
FAILS: list[str] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """登录要自己看 303 和 Set-Cookie，别被自动跟走。"""

    def redirect_request(self, *a, **k):
        return None


def _opener(follow=True):
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if not follow:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers), jar


def _read_password() -> str:
    f = ROOT / "gate.json"
    if not f.exists():
        print("⚠️ 没有 gate.json —— 云上那份是免密的？那这台机器上的口令也对不上。")
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        print("⚠️ gate.json 读不出来")
        return ""
    return str(data.get("password") or "").strip() if isinstance(data, dict) else ""


class Client:
    def __init__(self, base: str, follow: bool = True):
        self.base = base.rstrip("/")
        self.op, self.jar = _opener(follow)

    def call(self, path, method="GET", data=None, headers=None, timeout=25):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except Exception as e:  # noqa: BLE001 - 网络层的任何意外都要变成一句人话
            return 0, {}, str(e).encode()

    def login(self, pw: str):
        body = urllib.parse.urlencode({"password": pw, "next": "/"}).encode()
        return self.call("/login", "POST", body,
                         {"Content-Type": "application/x-www-form-urlencoded"})


def _ok(msg: str) -> None:
    print("  ✓", msg)


def _bad(msg: str) -> None:
    FAILS.append(msg)
    print("  ✗", msg)


def _items_count(body: bytes):
    d = json.loads(body.decode())
    return len(d if isinstance(d, list) else d.get("items", []))


def _comfort(body: bytes):
    return json.loads(body.decode()).get("prefs", {}).get("comfort")


def full(args) -> int:
    pw = args.pw or _read_password()
    if not pw:
        print("没有口令，验不了门。先 `python scripts/set_gate_password.py`。")
        return 2

    print("=== ① 站点活着吗 ===")
    for _ in range(20):
        if Client(args.url).call("/api/ping")[0] == 200:
            _ok("/api/ping 通")
            break
        time.sleep(3)
    else:
        _bad("20 次轮询都没通（云上没起来？刚发布要等一会儿）")
        return 1

    print("=== ② 未登录必须被挡住 ===")
    c = Client(args.url, follow=False)
    code, _, body = c.call("/")
    if code == 200 and 'type="password"' in body.decode("utf-8", "replace"):
        _ok("GET / → 登录页")
    else:
        _bad(f"GET / → {code}，不是登录页")
    code, _, _ = c.call("/api/items")
    _ok("GET /api/items → 401") if code == 401 else _bad(f"GET /api/items → {code}（应 401）")

    print("=== ③ 错口令不过 ===")
    body = urllib.parse.urlencode({"password": "deliberately-wrong", "next": "/"}).encode()
    code, _, b = c.call("/login", "POST", body,
                        {"Content-Type": "application/x-www-form-urlencoded"})
    if code == 200 and "口令不对" in b.decode("utf-8", "replace") and not c.jar:
        _ok("错口令 → 停在登录页、不发 cookie")
    else:
        _bad(f"错口令处理不对：{code} / cookie 数={len(c.jar)}")

    print("=== ④ 对口令 → 拿到会话 ===")
    code, _, _ = c.login(pw)
    if code in (200, 302, 303) and len(c.jar) >= 1:
        _ok(f"登录成功（{code}，cookie {len(c.jar)} 个）")
    else:
        _bad(f"登录没成功：{code} / cookie 数={len(c.jar)}")

    print("=== ⑤ 登录后能看衣柜 ===")
    d = Client(args.url)
    d.login(pw)
    code, _, body = d.call("/")
    if code == 200 and "tempv" in body.decode("utf-8", "replace"):
        _ok("GET / → 今日页（不是登录页）")
    else:
        _bad(f"GET / 登录后异常：{code}")
    code, _, body = d.call("/api/items")
    if code == 200:
        n = _items_count(body)
        _ok(f"GET /api/items → {n} 件")
        # ⚠️ 不许写死件数（第 ⑩ 条）：只报数，跟本机库比
        try:
            import sqlite3
            k = sqlite3.connect(f"file:{ROOT/'data'/'closet.db'}?mode=ro", uri=True)
            local = k.execute("select count(*) from items").fetchone()[0]
            k.close()
            _ok(f"本机库 {local} 件 → 一致") if n == local else _bad(f"件数对不上：线上 {n} / 本机 {local}")
        except Exception:  # noqa: BLE001 - 比不了就跳过，不编结论
            print("  · （本机库读不到，跳过比对）")
    else:
        _bad(f"GET /api/items → {code}")

    print("=== ⑥ 云上写一次（写完还原） ===")
    # ⚠️ 精确快照 → 精确还原：这是**线上那份**的用户设置，改完必须放回去，
    #    否则用户打开手机看到的是被测试改过的值。
    before = _comfort(d.call("/api/prefs")[2])
    code, _, _ = d.call("/api/prefs", "POST", json.dumps({"comfort": 21.5}).encode(),
                        {"Content-Type": "application/json"})
    got = _comfort(d.call("/api/prefs")[2])
    _ok("写入生效：comfort=21.5") if (code == 200 and got == 21.5) else _bad(
        f"写入没生效：POST {code}，回读 {got}")
    if isinstance(before, (int, float)) and before != 21.5:
        d.call("/api/prefs", "POST", json.dumps({"comfort": before}).encode(),
               {"Content-Type": "application/json"})
        back = _comfort(d.call("/api/prefs")[2])
        _ok(f"已还原成原来的 comfort={before}") if back == before else _bad(
            f"没还原回去：期望 {before}，实际 {back}")
    else:
        print(f"  · （原来就是 {before}，不用还原）")

    return _report()


def recheck(args) -> int:
    """重新发布之后：件数还在吗、云上那次改动还在吗。

    这是唯一能回答"重发会不会顶掉云上数据"的检查 —— **答案见 README 已知限制第 0 条**。
    `--marker` 是实验时写进云上的那个值（不写死，否则就是第 ⑩ 条禁的"依赖偶然状态"）。
    """
    pw = args.pw or _read_password()
    c = Client(args.url)
    c.login(pw)
    code, _, body = c.call("/api/items")
    print(f"  · 件数：{_items_count(body) if code == 200 else f'读不到（{code}）'}")
    code, _, body = c.call("/api/prefs")
    if code != 200:
        print(f"  · GET /api/prefs → {code}")
        return 1
    got = _comfort(body)
    print(f"  · comfort：{got}（实验时写进去的是 {args.marker}）")
    if got == args.marker:
        print("  → 云上改动【活下来了】= 这次发布没动 data/")
    else:
        print("  → 云上改动【被顶回本机版本】= 重发就是拿电脑上的 data/ 覆盖云上")
    return 0


def _report() -> int:
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="胶囊衣橱 线上验收")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--pw", default="", help="口令。默认从 gate.json 读，不建议写在命令行里")
    ap.add_argument("--recheck", action="store_true", help="重新发布之后只复查数据")
    ap.add_argument("--marker", type=float, default=21.5,
                    help="复查时：实验写进云上的那个值（默认 21.5）")
    args = ap.parse_args()
    return recheck(args) if args.recheck else full(args)


if __name__ == "__main__":
    sys.exit(main())
