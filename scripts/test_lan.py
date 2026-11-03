# -*- coding: utf-8 -*-
"""局域网 / 远程地址自检。**离线，不联网，不碰用户数据。**

    python scripts/test_lan.py --out test_lan.out.txt

为什么单独一个文件：这段逻辑出错的症状是「**手机打不开，而且不知道为什么**」——
手机上只有转圈，没有任何报错，电脑这边也一切正常。而它的根子是一条
**看不见的环境事实**：

  · 本机装了 Clash 一类的代理（fake-ip 模式），它建一块虚拟网卡并**抢走默认路由**。
    于是「连一个不可达地址，让系统按路由表选出本机网卡」那一招返回的是
    `198.18.0.1` —— 局域网里根本没有这台机器。
  · 实测：本机 `ipconfig` 的**第一行**就是它。旧版横幅直接把它印给手机，永远打不开。

所以规矩是：**枚举网卡地址 + 过滤掉不可能连上的**，不能只信路由表。
这个文件钉死三件事：
  ① 过滤表本身（哪些段一定连不上）
  ② **横幅上印出来的手机地址，一定都得是能连的** —— 这条是用户唯一看得见的东西
  ③ **"任何网络都能用"（远程）与"手机连同一个 WiFi"（局域网）不许混成一栏**
     （2026-09-29 加；混了的话用户出门在外会以为还得先连同一个 WiFi）

覆盖：
  ① `_unreachable` 的过滤表（回环 / 链路本地 / 代理 fake-ip / 畸形串）
  ② `lan_ips` 枚举、去重、排序（打桩 getaddrinfo，喂一份"真实世界那种"的乱序结果）
  ③ 横幅端到端：印出来的地址逐条复查，且主力地址就是 `lan_ips()[0]`
  ④ **tailnet 段（100.64.0.0/10）**：边界值、"分组 ≠ 过滤"、横幅的四种状态
     （⚠️ `tailscale_info()` 一律**打桩** —— 真实环境里装没装它是会变的，
     拿它当依据就等于断言依赖跑测机器，同第 ② 节那次踩的是同一个坑）
  ⑤ 真实环境的不变量（**不写死具体 IP** —— 换个 WiFi 就该照过）
"""

from __future__ import annotations

import argparse
import io
import re
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve  # noqa: E402  （import 时会把 cwd 切到项目根 —— 这里本来就是）

FAILS: list[str] = []

PORT = 8077


@contextmanager
def _stub(obj, name: str, value):
    """临时替换 `obj.name`。

    ⚠️ 为什么这条断言必须打桩（2026-09-27 踩）：横幅里印不印
    `本机访问 127.0.0.1`，取决于"回环此刻有没有被别的程序占着"。
    本机另一个项目正好起了个 `python -m http.server` 占着 127.0.0.1:8077，
    断言就在**别人的进程状态**下变红 —— 跟被测代码对不对毫无关系。
    断言不该依赖跑测机器上的偶然状态。
    """
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def head(t: str) -> None:
    print()
    print("─" * 60)
    print(t)
    print("─" * 60)


def say(t: str = "") -> None:
    if t:
        print(f"  {t}")


def ok(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


@contextmanager
def fake_gai(ips: list[str]):
    """让 `socket.getaddrinfo` 返回一份指定的地址表。

    ⚠️ `serve.socket` 就是 `socket` 模块本身，改它是**全局副作用**，
    所以必须原样还回去 —— 不还的话后面所有真实网络调用都拿到假地址，
    而且不会报错（又是一次静默失败）。
    """
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        if family not in (0, socket.AF_INET):
            return []
        # 形状跟真的 getaddrinfo 一致：(family, type, proto, canonname, sockaddr)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    socket.getaddrinfo = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real  # type: ignore[assignment]


# ══════════════════════════════════════════════
# ① 过滤表
# ══════════════════════════════════════════════

BAD = [
    ("127.0.0.1", "回环 —— 手机上访问它等于访问手机自己"),
    ("127.1.2.3", "整个 127/8 都是回环"),
    ("169.254.3.4", "链路本地 —— 拿不到 DHCP 时的自赋值，连不通"),
    ("198.18.0.1", "代理 fake-ip（198.18.0.0/15）—— **就是本机 ipconfig 第一行那个**"),
    ("198.19.255.254", "代理 fake-ip 的另一半（198.19.0.0/16）"),
    ("0.0.0.0", "未指定地址 —— 它是「监听全部」，不是「某个网卡」"),
    ("1.2.3", "只有三段，畸形"),
    ("1.2.3.4.5", "五段，畸形"),
    ("abc.def.gh.ij", "压根不是 IP"),
    ("", "空串"),
    ("192.168.1.256", "最后一段越界（>255）—— int() 能过，但不是合法 IP"),
]

GOOD = [
    ("192.168.1.188", "家用路由器最常见的段"),
    ("192.168.88.1", "虚拟网卡 / 热点网关，仍然可能是能连的"),
    ("10.0.0.5", "企业网 / 热点常见"),
    ("172.16.3.4", "私有段"),
    ("100.64.0.1", "tailnet 段 —— `_unreachable` **不拦**（它确实能通），"
                   "但也不进 `lan_ips`，见 ⑤"),
]


def test_filter() -> None:
    head("① 过滤表：哪些地址手机一定连不上")

    for ip, why in BAD:
        ok(serve._unreachable(ip), f"剔除 {ip:<18s} {why}")

    for ip, why in GOOD:
        ok(not serve._unreachable(ip), f"保留 {ip:<18s} {why}")

    ok(serve._unreachable("198.18.0.1") and serve._unreachable("198.19.0.1"),
       "**回归钉**：198.18/198.19 整段都剔除 —— 这条以前没有，"
       "横幅于是把 198.18.0.1 印给了手机")


# ══════════════════════════════════════════════
# ② 枚举与排序
# ══════════════════════════════════════════════

# 一份"真实世界那种"的乱序结果：fake-ip 排在最前（因为它抢了默认路由），
# 中间夹着回环、重复项、链路本地。
MESSY = [
    "198.18.0.1",        # fake-ip，最危险
    "127.0.0.1",         # 回环
    "169.254.11.22",     # 链路本地
    "192.168.213.1",     # 虚拟网卡网关
    "192.168.1.188",    # 真实 WiFi
    "192.168.1.188",    # 重复
    "192.168.88.1",      # 另一个虚拟网关
]


def test_enum() -> None:
    head("② 枚举、去重、排序")

    with fake_gai(MESSY):
        ips = serve.lan_ips()

    # ⚠️ 顺序是**规则**算出来的，不是随便一个列表：
    #    先按"像不像真实网卡"排（192.168 > 10 > 其它），末尾 `.1` 的一律垫后
    #    （家用路由器给电脑分的是 .x，`.1` 多半是网关/虚拟网卡/热点）。
    #    两个都垫后的之间按字符串排，所以 '192.168.213.1' 在 '192.168.88.1' 前（'2'<'8'）。
    ok(ips == ["192.168.1.188", "192.168.213.1", "192.168.88.1"],
       f"结果 = {ips}：非 .1 的真实网卡在最前，两个 .1 结尾的虚拟网关垫后")
    ok(len(ips) == len(set(ips)), "没有重复项")
    ok(all(not serve._unreachable(ip) for ip in ips), "剩下的每一条都过了过滤表")
    ok(ips[0] == "192.168.1.188",
       "主力地址是真实 WiFi 那个 —— **不是** fake-ip、**不是**回环、**也不是**末尾 .1 的虚拟网关")

    with fake_gai(MESSY):
        ok(serve.lan_ip() == "192.168.1.188", "`lan_ip()` 取第一个")

    # 只剩坏的 → 退回回环。宁可明确"没得用"，也不编一个地址出来。
    with fake_gai(["198.18.0.1", "127.0.0.1", "169.254.1.1"]):
        ok(serve.lan_ips() == [], "全是坏地址 → 空列表（不编）")
        ok(serve.lan_ip() == "127.0.0.1", "`lan_ip()` 退回回环（等于承认没法用）")

    # getaddrinfo 抛异常（网卡全掉 / DNS 配置坏了）→ 不许把启动流程带崩。
    real = socket.getaddrinfo

    def boom(*a, **k):
        raise OSError("模拟网卡掉了")

    socket.getaddrinfo = boom  # type: ignore[assignment]
    try:
        ok(serve.lan_ips() == [], "`getaddrinfo` 抛 OSError → 空列表，不抛")
    finally:
        socket.getaddrinfo = real  # type: ignore[assignment]

    # 没有 .1 结尾的虚拟网关时，纯 192.168 段之间按字符串排（稳定，可复现）
    with fake_gai(["192.168.9.7", "192.168.10.7"]):
        ok(serve.lan_ips() == ["192.168.10.7", "192.168.9.7"],
           "同段内排序稳定可复现（不是 set 的随机顺序）")


# ══════════════════════════════════════════════
# ③ 横幅端到端 —— 用户唯一看得见的东西
# ══════════════════════════════════════════════

URL_RE = re.compile(r"http://([0-9.]+):(\d+)")


# 打桩用的 Tailscale 状态。
# ⚠️ 为什么必须打桩而不是读真实环境：`tailscale_info()` 的结果取决于
# **这台机器此刻装没装 Tailscale、登没登录** —— 拿它当断言依据，就是让断言
# 依赖跑测机器的偶然状态（本项目为此栽过一次，见上面 `_stub` 的注释）。
# 三种情形都钉住，谁都不许靠"我这儿恰好是这样"。
_TS_OFF = {"installed": False, "ok": False, "state": None,
           "dns": None, "ip": None, "https": False}
_TS_NO_HTTPS = {"installed": True, "ok": True, "state": "Running",
                "dns": "desktop-abcd.tail1234.ts.net",
                "ip": "100.101.102.103", "https": False}
_TS_HTTPS = dict(_TS_NO_HTTPS, https=True)


def _banner_text(port: int, lan: bool, ips: list[str],
                 ts: dict | None = None) -> str:
    """跑一遍 `serve.banner`，把输出抓成字符串。

    `ts` 默认按"**没装 Tailscale**"跑（最常见的情形）；要测装了之后的行为就显式传。
    """
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    stub = dict(ts if ts is not None else _TS_OFF)
    try:
        with fake_gai(ips), _stub(serve, "tailscale_info", lambda *a, **k: dict(stub)):
            serve.banner(port, lan, "0.0.0.0")
    finally:
        sys.stdout = old
    return buf.getvalue()


# ══════════════════════════════════════════════
# ⑤ tailnet 段：与"同一个 WiFi"那一屏**分开**
# ══════════════════════════════════════════════

TAILNET_YES = ["100.64.0.1", "100.101.102.103", "100.127.255.254",
               "100.100.100.100"]
TAILNET_NO = ["100.63.255.255", "100.128.0.1", "192.168.1.5", "10.0.0.1",
              "100.64.1", "100.x.0.1", "100.64.0.256", ""]


def test_tailnet() -> None:
    head("⑤ tailnet 段（100.64.0.0/10）不与局域网混在一起")

    for ip in TAILNET_YES:
        ok(serve._is_tailnet(ip), f"{ip:<17s} 认成 tailnet")

    for ip in TAILNET_NO:
        ok(not serve._is_tailnet(ip), f"{ip!r:<19s} 不算 tailnet")

    # ⚠️ 这一节存在的理由：**分组 ≠ 过滤**。
    # tailnet 地址是**能连**的（所以 `_unreachable` 放行），
    # 但它的语义是"任何网络"，而 `lan_ips` 的语义是"手机连同一个 WiFi"。
    # 混进 `lan_ips`，用户出门在外会以为"得先连上同一个 WiFi 才行"，白折腾半天。
    MIXED = ["198.18.0.1", "127.0.0.1", "192.168.1.188",
             "100.101.102.103", "192.168.88.1"]
    with fake_gai(MIXED):
        lan = serve.lan_ips()
        net = serve.tailnet_ips()

    ok(lan == ["192.168.1.188", "192.168.88.1"],
       f"`lan_ips` 只剩局域网（拿到 {lan}）")
    ok(net == ["100.101.102.103"], f"`tailnet_ips` 挑出 100.x（拿到 {net}）")
    ok(not (set(lan) & set(net)), "两张表没有交集")
    ok(not serve._unreachable("100.101.102.103"),
       "tailnet 地址**不**被 `_unreachable` 拦 —— 它是能连的，只是要分开放")

    # ── 横幅四种状态 ──
    # ① 没装：明说"未开通"+ 指路，**不许**沉默（沉默 = 用户以为没这条路）
    t_off = _banner_text(PORT, True, MIXED)
    ok("远程访问" in t_off, "横幅里有「远程访问」一行 —— 没装也得让人知道有这条路")
    ok("未开通" in t_off and "start-remote.bat" in t_off,
       "没装时明说「未开通」并告诉双击哪个文件")

    # ② 装了但没配 https：印**域名**（不是 100.x 那串没人记得住的数字）
    t_http = _banner_text(PORT, True, MIXED, _TS_NO_HTTPS)
    ok("desktop-abcd.tail1234.ts.net" in t_http, "装了之后印 MagicDNS 域名")
    ok("100.101.102.103" not in t_http, "有域名就用域名，**不**印 100.x")
    ok("想要 https" in t_http, "提醒怎么变成 https（装成 App 需要它）")

    # ③ 配好 serve：直接印 https，且不再劝他去配
    t_https = _banner_text(PORT, True, MIXED, _TS_HTTPS)
    ok("https://desktop-abcd.tail1234.ts.net" in t_https,
       "配好 serve 后印 https 地址")
    ok("想要 https" not in t_https, "已经 https 了就不再劝")

    # ④ **不带 --lan 也要印远程**：`tailscale serve` 是反代到 127.0.0.1 的，
    #    所以"只开本机"照样能远程访问 —— 漏了这行，用户会以为必须开 --lan。
    t_nolan = _banner_text(PORT, False, MIXED, _TS_HTTPS)
    ok("https://desktop-abcd.tail1234.ts.net" in t_nolan,
       "不带 --lan 时**也**印远程地址（serve 反代的是回环，不需要 --lan）")
    ok("192.168" not in t_nolan, "但不带 --lan 时仍然不印局域网地址")

    # ⑤ 没装时**一个远程地址都不许编**（同"天气取不到不编温度"的口径）
    ok("ts.net" not in t_off, "没装时不许编域名")
    ok("100." not in t_off, "没装时不许编 tailnet 地址")


def test_banner() -> None:
    head("③ 横幅：印出来的手机地址必须条条能连")

    text = _banner_text(PORT, True, MESSY)
    lines = [ln.strip() for ln in text.splitlines()]

    phone = [ln for ln in lines if "手机访问" in ln or "换这个" in ln]
    ok(len(phone) >= 2, f"横幅里出现了手机地址（{len(phone)} 行）")
    ok(any("192.168.1.188" in ln for ln in phone),
       "真实 WiFi 地址在横幅里")

    # 逐条复查：手机那一堆地址里，**一条都不许**是过滤表里的
    urls = [u for ln in phone for u in URL_RE.findall(ln)]
    ok(bool(urls), f"解析出 {len(urls)} 条地址")
    bad = [f"{ip}:{p}" for ip, p in urls if serve._unreachable(ip)]
    ok(not bad, f"没有一条连不上（连不上的是 {bad}）" if bad else "没有一条连不上")
    ok(all(p == str(PORT) for _, p in urls), f"端口都是 {PORT}")

    ok(not any("198.18" in ln for ln in lines),
       "**整份横幅里不许出现 198.18** —— 这正是修之前的那次事故")

    # 本机那一行必须是回环，不能是网卡地址（用户在自己电脑上打开）。
    # ⚠️ 但**回环被别人占着时不许印**（2026-09-27 加）：那天另一个项目起的
    #    `python -m http.server` 抢了 127.0.0.1:8077 —— 印出来用户点开看到的
    #    是别人的东西（实拍：一条 `Directory listing for /`）。
    #    两种情况都钉住，且用打桩决定，不依赖跑测机器此刻的端口状态。
    with _stub(serve, "loopback_shadowed", lambda *a, **k: False):
        clean = _banner_text(PORT, True, MESSY).splitlines()
        local = [ln for ln in clean if "本机访问" in ln]
        ok(len(local) == 1 and "127.0.0.1" in local[0],
           "回环干净时：本机访问 是 127.0.0.1")
    with _stub(serve, "loopback_shadowed", lambda *a, **k: True):
        shadow = _banner_text(PORT, True, MESSY).splitlines()
        ok(not any("本机访问" in ln for ln in shadow),
           "回环被占时：**不印** 127.0.0.1（点了会打开别人的东西）")
        ok(any("另一个程序" in ln for ln in shadow),
           "而且要明说被占，别让用户自己猜")

    # 一个可用地址都没有 → 说清楚，而不是印一个假的
    text2 = _banner_text(PORT, True, ["198.18.0.1"])
    ok("没找到可用的局域网地址" in text2,
       "一个都连不上时明说「没找到可用的局域网地址」")
    ok("198.18" not in text2, "而且**不许**退而求其次印 fake-ip")

    # 不开 --lan：不该印任何手机地址
    text3 = _banner_text(PORT, False, MESSY)
    ok("未开启" in text3 and "192.168" not in text3,
       "不开 --lan 时印「未开启」，不引诱用户去连一个连不上的地址")


# ══════════════════════════════════════════════
# ④ 真实环境的不变量
# ══════════════════════════════════════════════

def test_real_env() -> None:
    head("④ 真实环境：只查不变量，**不写死具体 IP**（换个 WiFi 就该照过）")

    ips = serve.lan_ips()
    say(f"本机枚举到 {len(ips)} 个可用地址：{ips or '（无）'}")
    say(f"横幅会印：http://{serve.lan_ip()}:{PORT}")

    ok(all(not serve._unreachable(ip) for ip in ips), "枚举结果全部过了过滤表")
    ok(not any(ip.startswith("198.18.") or ip.startswith("198.19.") for ip in ips),
       "结果里没有代理 fake-ip")
    ok(not any(ip.startswith("127.") for ip in ips), "结果里没有回环")

    if ips:
        # 有一条可用的，横幅主力就不能是回环 —— 否则等于明明能用却告诉用户不能用
        ok(serve.lan_ip() == ips[0] and serve.lan_ip() != "127.0.0.1",
           "有可用地址时，横幅主力不是回环")
    else:
        say("⚠️ 本机当前一个可用局域网地址都没有（没连 WiFi？）—— 跳过依赖它的断言")


# ══════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把报告写到文件"
                                  "（PowerShell 抓不到子进程 stdout，用它）")
    args = ap.parse_args()

    lines: list[str] = []

    class Tee:
        def write(self, s: str) -> None:
            sys.__stdout__.write(s)
            lines.append(s)

        def flush(self) -> None:
            sys.__stdout__.flush()

    old = sys.stdout
    sys.stdout = Tee()  # type: ignore[assignment]
    try:
        print("局域网 / 远程地址自检（离线）")
        test_filter()
        test_enum()
        test_banner()
        test_tailnet()
        test_real_env()

        print()
        print("=" * 60)
        if FAILS:
            print(f"✗ {len(FAILS)} 项没过：")
            for f in FAILS:
                print(f"   - {f}")
        else:
            print("✓ 全部通过")
        print("=" * 60)
    finally:
        sys.stdout = old

    if args.out:
        Path(args.out).write_text("".join(lines), encoding="utf-8")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
