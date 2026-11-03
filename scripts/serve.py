# -*- coding: utf-8 -*-
"""胶囊衣橱 · 启动器。

    python scripts/serve.py                仅本机   http://127.0.0.1:8077
    python scripts/serve.py --lan          局域网   http://<本机IP>:8077（手机同 WiFi 可用）
    python scripts/serve.py --port 9000    换端口

启动脚本（start.bat）只负责找 python.exe，所有中文提示都放在这里 —— 因为
cmd.exe 是按系统代码页逐行解析 .bat 的，文件里出现多字节字符（中文）再叠加
chcp 切换代码页，cmd 会丢失文件读取位置，把后面的行读成碎片，屏幕上就是一堆
"xxx 不是内部或外部命令"。所以 .bat 保持纯 ASCII，中文全归 Python。

端口处理（踩过坑）：直接 uvicorn.run 撞上已占用的端口会甩一段
[Errno 10048] WinError 10048 的英文 traceback，看不出该怎么办。这里改成先探活：
  · 端口上跑的就是本应用 → 说明服务已经在跑了，直接给地址，不当成错误
  · 端口被别的程序占着     → 自动往后找空闲端口，并说明换了哪个
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_TAG = "capsule-wardrobe"


def _unreachable(ip: str) -> bool:
    """这个地址手机不可能连上。

    代理软件（Clash 等的 fake-ip 模式）会建一块虚拟网卡并**抢走默认路由**，
    于是"连一个不可达地址"那一招返回的是 198.18.x.x —— 局域网里没这台机器。
    ⚠️ 实测：本机 ipconfig 的第一行就是 198.18.0.1，不改的话横幅会把它印给手机，
    用户只会看到"打不开"，而且完全不知道为什么。所以这里是**枚举 + 过滤**，
    不能只信路由表。
    """
    parts = ip.split(".")
    if len(parts) != 4:
        return True
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return True
    if any(o < 0 or o > 255 for o in octets):
        # 畸形串（`192.168.1.256` 这种）。`int()` 能过，但它不是地址 ——
        # 印在横幅上，用户照样只会看到"打不开"。
        return True
    a, b = octets[0], octets[1]
    if a == 127:                       # 回环
        return True
    if a == 169 and b == 254:          # 链路本地
        return True
    if a == 198 and b in (18, 19):     # 代理 fake-ip（198.18.0.0/15、198.19.0.0/16）
        return True
    if a == 0:
        return True
    return False


def _is_tailnet(ip: str) -> bool:
    """100.64.0.0/10 —— Tailscale（以及运营商 CGNAT）给设备分的地址段。

    ⚠️ 它**不该混进 `lan_ips()`**：那一屏的字面意思是"手机连同一个 WiFi"，
    而 100.x 恰好相反 —— 手机**不在**同一个 WiFi 时才用得上它。
    混在一张列表里，用户会以为"出门也得先连同一个 WiFi"，在外面白折腾半天。

    （2026-09-29 才分开。`test_lan.py` 原来特意把它留在 GOOD 里，理由是
    "宁多给不误杀" —— 那个理由对**过滤**成立，对**分组**不成立。）
    """
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    return octets[0] == 100 and 64 <= octets[1] <= 127


def lan_ips() -> list[str]:
    """本机所有"手机可能连得上"的 IPv4，最可能的排在最前面。

    枚举网卡地址而不是只看默认路由。家用路由器绝大多数是 192.168.x，
    所以先排它；末尾 `.1` 的多半是虚拟网卡/热点网关，排到后面去。

    ⚠️ **不含 tailnet（100.64/10）** —— 那条路是"任何网络都能用"，
    属于 `remote_url()`，不属于这一屏。
    """
    found: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found and not _unreachable(ip) and not _is_tailnet(ip):
            found.append(ip)

    found.sort(key=lambda ip: (not ip.startswith("192.168."),
                               not ip.startswith("10."),
                               ip.endswith(".1"),
                               ip))
    return found


def lan_ip() -> str:
    """最可能被手机访问到的那个地址。一个都没有就退回回环（等于没法用）。"""
    ips = lan_ips()
    return ips[0] if ips else "127.0.0.1"


def tailnet_ips() -> list[str]:
    """本机的 tailnet 地址（100.64.0.0/10）。没装 Tailscale 时是空列表。"""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out and _is_tailnet(ip):
            out.append(ip)
    return out


# ── Tailscale：出门在外也能用（2026-09-29）────────────────────────────
#
# 上面那套（lan_ips）解决"手机和电脑在同一个 WiFi"。出门在外要靠 **Tailscale**：
# 电脑和手机各装一个、登录**同一个账号**，就组成一张私有网。三个性质正好对上：
#   · 不暴露公网 —— 只有你自己登录的设备能连（跟"端口转发到公网"是两回事）
#   · 数据不出本机 —— 端到端加密直连，衣服照片和衣橱数据全留在电脑里
#   · 免费
#
# 远程地址有两种，优先用域名那个：
#   https://<机器名>.<tailnet>.ts.net   ← 配了 `tailscale serve`（反代本机端口 + 自动证书）
#   http://100.x.x.x:<port>             ← 直连（要防火墙放行，且没有 https）
#
# ⚠️⚠️ **绝对不要 `tailscale funnel`** —— 那是把端口暴露到**公网**。
# 本应用**零鉴权**（`api_key` 只是大模型配置），谁拿到网址谁能改能删你的衣橱。
# `serve` 是私有（只有你的设备能连），`funnel` 是公开。两者只差一个词，后果差一个量级。

_TS_CANDIDATES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
)


def tailscale_exe() -> str | None:
    """tailscale.exe 在哪。没装返回 None。"""
    for p in _TS_CANDIDATES:
        if Path(p).exists():
            return p
    try:
        from shutil import which
        return which("tailscale")
    except Exception:
        return None


def _run_ts(args: list[str], timeout: float = 3.0) -> str | None:
    """跑一次 tailscale 子命令。**任何失败都返回 None —— 绝不抛、绝不拖住启动。**"""
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            # ⚠️ 不给这个，Windows 上每次调用都会闪一个黑窗
            # （tailscale.exe 带 GUI 子系统，纯命令行调用也照样弹）。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def tailscale_info(timeout: float = 3.0) -> dict:
    """Tailscale 的现状。**没装 / 没登录 / 超时一律 `ok=False`，不编地址。**

    （同"天气取不到时不编温度"的口径：失败分支给的是**状态**，
    而不是一个看起来能用的假地址 —— 后者只会让人在外面白等。）
    """
    out: dict = {"installed": False, "ok": False, "state": None,
                 "dns": None, "ip": None, "https": False}
    if tailscale_exe() is None:
        return out
    out["installed"] = True

    raw = _run_ts(["status", "--json"], timeout)
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            out["state"] = data.get("BackendState")
            me = data.get("Self") or {}
            out["dns"] = (me.get("DNSName") or "").rstrip(".") or None
            ips = [i for i in (me.get("TailscaleIPs") or []) if _is_tailnet(i)]
            out["ip"] = ips[0] if ips else None
            out["ok"] = out["state"] == "Running" and bool(out["dns"] or out["ip"])

    # serve 有没有在反代 —— 决定远程入口是 https（能装成 App）还是 http+端口
    sraw = _run_ts(["serve", "status", "--json"], timeout)
    if sraw:
        try:
            sv = json.loads(sraw)
            out["https"] = bool((sv.get("TCP") or {}).get("443")) or bool(sv.get("Web"))
        except ValueError:
            pass
    return out


def remote_url_from(info: dict, port: int) -> str | None:
    """由 `tailscale_info()` 的结果拼远程入口。`ok=False` → None，不编。"""
    if not info.get("ok"):
        return None
    if info.get("https") and info.get("dns"):
        return f"https://{info['dns']}"
    if info.get("dns"):
        return f"http://{info['dns']}:{port}"
    return f"http://{info['ip']}:{port}"


def remote_url(port: int) -> str | None:
    """远程入口（手机在任何网络都能打开的地址）。没装 / 没登录 → None。"""
    return remote_url_from(tailscale_info(), port)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def can_bind(host: str, port: int) -> bool:
    """**我要绑的那个地址**能不能绑上 —— 端口判断唯一准确的依据。

    ⚠️ 为什么不能用 port_in_use 代替（2026-09-27 踩的）：
    `port_in_use` 永远探 `127.0.0.1`，但我们要绑的可能是 `0.0.0.0`，
    **这是两个不同的地址，占不占没关系**。Windows 允许：
        0.0.0.0:8077    → 本应用
        127.0.0.1:8077  → 另一个程序的 http.server
    两者共存。此时用 port_in_use 判断会得出"端口被占"的结论，
    于是自动换到 8078 —— 而其实我们**完全绑得上** 8077，换端口纯属自伤：
    用户手机里存的是 8077，一换就打不开了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _ping(host: str, port: int) -> bool:
    """请求 /api/ping，判断这个地址上是不是本应用。探测失败一律当成"不是"。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=1.2) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_TAG
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def is_our_service(port: int) -> bool:
    return _ping("127.0.0.1", port)


def find_our_service(port: int, host: str) -> str | None:
    """在**我们要绑的那个地址**上，找找有没有已经在跑的自己。返回命中的地址。

    ⚠️ 为什么必须试多个地址（2026-09-27 踩的，代价是一次重启直接废掉手机地址）：
    Windows 允许 `0.0.0.0:8077` 和 `127.0.0.1:8077` **同时被两个不同进程监听**
    —— 只要后绑的那个用更具体的地址。于是会出现这种局面：

        0.0.0.0:8077    → 本应用（手机走局域网打得通）
        127.0.0.1:8077  → 【别的程序】（当时是另一个项目起的 `python -m http.server`）

    此时若只探 `127.0.0.1`，探到的是**别人**，于是把"我自己已经在跑"
    误判成"端口被别的程序占用" → 自动换到 8078 → **用户手机上的地址当场失效**，
    而他手机上看到的还是旧页面（甚至打不开），完全没有线索。

    所以绑 `0.0.0.0` 时，必须优先试**局域网地址** —— 那条路不会被别人的回环监听挡住。
    """
    cands: list[str] = [host]
    if host in ("0.0.0.0", "::"):
        cands = lan_ips() + ["127.0.0.1"]
    for h in cands:
        if _ping(h, port):
            return h
    return None


def loopback_shadowed(port: int, host: str) -> bool:
    """回环地址上是不是**别人**在听（我们绑的是 0.0.0.0，而 127.0.0.1 另有其人）。

    这种情况必须说破：在电脑上打开 `http://127.0.0.1:8077` 看到的会是别的东西，
    用户会以为整条链路坏了。手机走局域网其实是好的。
    """
    if host not in ("0.0.0.0", "::"):
        return False
    return port_in_use(port) and not _ping("127.0.0.1", port)


def next_free_port(start: int, limit: int = 20) -> int | None:
    for p in range(start + 1, start + 1 + limit):
        if not port_in_use(p):
            return p
    return None


def banner(port: int, lan: bool, host: str) -> None:
    print()
    print("  胶囊衣橱")
    print("  " + "=" * 48)

    # ⚠️ 回环地址被别的程序占着时，**不要再印 127.0.0.1** ——
    # 电脑上打开它会看到别人的东西（本项目 2026-09-27 实拍过：另一个项目起的
    # `python -m http.server` 抢了 127.0.0.1:8077，打开是一条 Directory listing）。
    shadowed = loopback_shadowed(port, host)
    if shadowed:
        print(f"  ⚠️  本机 127.0.0.1:{port} 被**另一个程序**占着。")
        print("      电脑上请用下面的局域网地址，别用 127.0.0.1。")
    else:
        print(f"  本机访问    http://127.0.0.1:{port}")
    ips: list[str] = []
    if lan:
        ips = lan_ips()
        if not ips:
            print("  手机访问    没找到可用的局域网地址 —— 电脑先连上 WiFi。")
        else:
            print(f"  手机访问    http://{ips[0]}:{port}   （手机连同一个 WiFi）")
            for ip in ips[1:]:
                print(f"    连不上换这个  http://{ip}:{port}")
    else:
        print("  手机访问    未开启 —— 双击 start-lan.bat（手机连同一个 WiFi）")

    # ── 远程：手机在任何网络都能打开（Tailscale 私有网）──
    # ⚠️ 这一段**两种模式都印**：`tailscale serve` 是反代到 127.0.0.1 的，
    # 所以不带 --lan 也能远程访问（只是局域网那条路就不通了）。
    ts = tailscale_info()
    rurl = remote_url_from(ts, port)
    if rurl:
        print(f"  远程访问    {rurl}")
        if not ts["https"]:
            print("              想要 https 就双击 start-remote.bat")
    elif not ts["installed"]:
        print("  远程访问    未开通 —— 双击 start-remote.bat（免费私有网，出门也能用）")
    else:
        print(f"  远程访问    Tailscale 没就绪（{ts['state'] or '未登录'}）"
              " —— 双击 start-remote.bat")

    if lan and ips:
        print()
        print("  装到手机上：手机浏览器打开上面的地址 →")
        print("              菜单里选「添加到主屏幕」/「安装应用」。")
        print("              之后就是一个没有地址栏的图标，跟 App 一样。")
        print("              要装成 App 得用**远程**那个 https 地址 ——")
        print("              局域网是 http，安卓 Chrome 不给装（只给快捷方式）。")
        print()
        print("  手机首次打不开：Windows 防火墙会拦 Python。")
        print("  在弹窗里选「允许访问」，或放行专用网络。")
    print("  " + "=" * 48)
    print("  停止服务    Ctrl+C")
    print()


def main() -> int:
    # ⚠️ 本机启动器 = 本机免密。
    # 口令文件 `gate.json` 在项目根，**本机跑的和云上跑的是同一份代码、同一个文件**。
    # 坐在自己电脑前的人不该被自己的门拦下，所以这里显式把门关掉。
    # **云上不用这个脚本**（发布时直接 `uvicorn app.main:app`），不设这个开关 → 云上照常要口令。
    os.environ["CLOSET_GATE"] = "off"

    ap = argparse.ArgumentParser(description="胶囊衣橱本地服务")
    ap.add_argument("--lan", action="store_true", help="监听 0.0.0.0，让手机也能访问")
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--host", default=None, help="手动指定监听地址，覆盖 --lan")
    args = ap.parse_args()

    lan = args.lan
    host = args.host or ("0.0.0.0" if lan else "127.0.0.1")
    port = args.port

    # ── 端口探活 ──
    # 判断依据是「**我要绑的那个地址**能不能绑」（can_bind），不是"127.0.0.1 上有没有人"。
    # 两条都踩过：① 127.0.0.1 上是**别人** → 把"自己已在跑"误判成"被占"而换端口；
    #           ② 127.0.0.1 被占但 0.0.0.0 完全绑得上 → 换成 8078 纯属自伤
    #              （用户手机里存的是 8077，一换就打不开）。
    running_at = find_our_service(port, host)
    if running_at:
        print()
        print("  服务已经在运行了 —— 不用再启动一次。")
        print(f"  直接打开    http://{running_at}:{port}")
        if loopback_shadowed(port, host):
            print()
            print(f"  ⚠️  注意：本机 127.0.0.1:{port} 上是**另一个程序**，")
            print("      在电脑上用那个地址会打开别的东西 —— 请用上面这个。")
        print()
        print("  （上一次的服务还开着。想重启就先关掉那个窗口。）")
        print()
        return 0

    if not can_bind(host, port):
        new_port = next_free_port(port)
        if new_port is None:
            print(f"  端口 {port} 被别的程序占用，后面 20 个端口也被占了。")
            print("  用 --port 指定一个别的端口。")
            return 1
        print()
        print(f"  端口 {port} 被别的程序占用 —— 改用 {new_port}。")
        port = new_port

    banner(port, lan, host)

    try:
        import uvicorn
    except ImportError:
        print("  [错误] 缺少依赖。先运行：")
        print(f'  "{sys.executable}" -m pip install -r requirements.txt')
        return 1

    try:
        # use_colors=False：老式 cmd.exe 不认 ANSI 转义，日志里会露出 "[32m"
        # 这种乱码字符（截图里能看到）。宁可不要颜色。
        uvicorn.run("app.main:app", host=host, port=port,
                    log_level="info", use_colors=False)
    except OSError as e:
        code = getattr(e, "errno", None)
        if code in (10048, 48, 98):  # Windows / macOS / Linux 的"地址已占用"
            print()
            print(f"  端口 {port} 刚被别的程序抢走了。换一个：")
            print(f"    python scripts/serve.py --port {port + 1}")
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
