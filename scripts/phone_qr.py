"""手机访问入口：算出地址 + 生成能扫的二维码。

    python scripts/phone_qr.py              局域网（默认）；配了远程就同时出远程
    python scripts/phone_qr.py --lan        只出局域网
    python scripts/phone_qr.py --remote     只出远程
    python scripts/phone_qr.py --open       生成后用系统看图打开
    python scripts/phone_qr.py --out x.png  指定输出路径（--remote 时是远程那张）

两种地址，两种用法
------------------
· **局域网**（`http://192.168.x.x:8077`）—— 手机得连**同一个 WiFi**。
  这个 IP 是路由器分配（DHCP）的，换网络、重启路由器、租约到期都会变，
  所以"每次变了都来问一遍链接"显然不行 → 把「查 IP + 出二维码」做成脚本。
· **远程**（`https://<机器名>.<tailnet>.ts.net`）—— 手机在**任何网络**都能开，
  靠 Tailscale 私有网。要先跑一次 `start-remote.bat` 把路铺好（见 README）。
  ⚠️ 它不出 `100.x.x.x` 那种形式 —— 那串数字没人记得住，有域名就用域名。

设计上的三条约束（都是踩坑换来的）
--------------------------------
1. **复用 `serve.py` 的判定，不再写第二套。** 项目里这类「两处判定漂移」
   出过事故（温度口径改一处忘一处，静默抵消）。取地址同理：serve 启动横幅上印的，
   和这里出二维码的，必须永远是同一个 —— 局域网用 `serve.lan_ips()`，
   远程用 `serve.remote_url()`。
2. **必须真的验一次"这个地址能不能打开"。** 服务默认只绑 127.0.0.1，
   此时打印出来的 IP 是死的 —— 二维码扫出来转圈、报「无法访问」，
   用户却以为是手机的问题。脚本自己先探一次，探不通就说清该双击哪个脚本，
   **不生成假二维码**。
3. **二维码出完立刻回读一遍**（需要 `zxingcpp`）。图坏了是典型静默失败：
   扫不出来的人只会怀疑网络、怀疑手机，不会怀疑这张图。

⚠️ 远程那条路有个**诚实的边界**：本机访问自己的 tailnet 地址**未必**走得通
（回环/hairpin 不一定支持）。所以探不到时**不说"手机也不行"** ——
只把事实报出来，让用户拿手机试。给错结论比不给结论更糟。
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "phone-qr-lan.png"
DEFAULT_OUT_REMOTE = ROOT / "docs" / "phone-qr-remote.png"


# ---------------------------------------------------------------- 复用 serve.py

def _serve():
    """拿到 serve 模块本身 —— 所有"地址怎么算"都从它那儿来，不另写一套。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import serve  # type: ignore
    return serve


def lan_ips() -> list[str]:
    """复用 `scripts/serve.py` 的判定，保证和启动横幅印的是同一份答案。"""
    try:
        return list(_serve().lan_ips())
    except Exception as exc:  # serve.py 改坏了也不该让这个脚本跟着瞎
        print(f"  ! 复用 serve.lan_ips() 失败：{exc}")
        return []


def local_fallback() -> list[str]:
    """serve.py 不可用时的兜底：只取本机默认路由出口的那个地址。"""
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("223.5.5.5", 80))   # 不发包，只为让内核选出出口网卡
            ip = sock.getsockname()[0]
        return [ip] if not ip.startswith("127.") else []
    except Exception:
        return []


# ---------------------------------------------------------------- 验证可达性

def probe(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """从本机走这个地址探一次 /api/ping。

    必须显式关掉代理 —— 本机长期挂着 Mihomo/Clash，HTTP_PROXY 会让
    回环和局域网请求也被代理走一圈，探不通就成了假警报。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "/api/ping", timeout=timeout) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 服务在，只是这个路径不对 —— 也算通
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


# ---------------------------------------------------------------- 画二维码

def build_qr(url: str, out: pathlib.Path, ips: list[str],
             subtitle: str = "手机需与电脑连同一个 WiFi") -> bool:
    """纸感卡片：上面二维码，下面把网址写出来（扫码失败还能手输）。"""
    try:
        import segno
    except ImportError:
        print("  ! 没装 segno，跳过二维码生成。装上就能出图：")
        print("      \"%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\pip.exe\" install segno")
        return False

    # ⚠️ 别用 `segno.QRCode.to_pil()` —— segno 只在检测到某些依赖时才挂载这个方法，
    # 本机环境里它不存在（Pillow 装着也一样）。改用它自带的纯 Python PNG writer
    # 输出成字节流，再交给 Pillow 拼卡片：零歧义，且不依赖 segno 的内部版本行为。
    # border=4 是二维码规范的静区宽度（4 个模块）。实测 border=2 时解码器也能读，
    # 但那是理想条件；手机拍照有斜角、反光和光线问题，静区给足才「好扫」而不只是「能扫」。
    buf = io.BytesIO()
    segno.make(url, error="h").save(
        buf, kind="png", scale=10, border=4, dark="#1a1a1a", light="#f4f0ea"
    )
    buf.seek(0)

    try:
        from PIL import Image, ImageDraw, ImageFont
        qr_img = Image.open(buf).convert("RGB")
    except ImportError:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(buf.getvalue())   # 至少把二维码存下来
        return True

    W = qr_img.width + 80
    pad_top, pad_bottom = 92, 150
    H = pad_top + qr_img.height + pad_bottom
    card = Image.new("RGB", (W, H), "#f4f0ea").convert("RGBA")
    draw = ImageDraw.Draw(card)

    # 中文字体：本机只有宋体（没有真粗体，所以别指望加粗）
    def font(size: int):
        for name in ("simsun.ttc", "msyh.ttc", "simhei.ttf"):
            p = pathlib.Path("C:/Windows/Fonts") / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def center(text: str, y: int, f, fill: str):
        w = draw.textlength(text, font=f)
        draw.text(((W - w) / 2, y), text, font=f, fill=fill)

    center("胶囊衣橱 · 手机访问", 26, font(30), "#1a1a1a")
    center(subtitle, 62, font(19), "#8a8178")

    card.paste(qr_img, (40, pad_top))

    y = pad_top + qr_img.height + 18
    center(url, y, font(22), "#1a1a1a")
    y += 34
    center("扫不出来就手动输上面这行", y, font(18), "#8a8178")
    y += 28
    if len(ips) > 1:
        center("（若打不开，换：" + " / ".join(ips[1:]) + "）", y, font(17), "#a89f95")

    out.parent.mkdir(parents=True, exist_ok=True)
    card.convert("RGB").save(out, quality=95)
    return True


# ---------------------------------------------------------------- 自带解码自检

def verify_qr(path: pathlib.Path, url: str) -> str | None:
    """生成完立刻把图丢给解码器读一遍。

    二维码坏掉是「静默失败」的典型：扫不出来的人只会怀疑网络、怀疑手机，
    不会怀疑这张图。所以这里不等用户来报，自己先验一次 ——
    读出来的必须正好是刚写进去的那个网址。

    返回 None 表示「没能力验证」（没装 zxing-cpp），**不假装验过了**。
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return None
    try:
        res = zxingcpp.read_barcodes(Image.open(path))
    except Exception as exc:
        return f"解码器出错：{exc}"
    if not res:
        return "解不出来，图可能坏了"
    got = res[0].text
    return "OK" if got == url else f"读出来是 {got}，跟预期不符"


def _emit_qr(url: str, out: pathlib.Path, ips: list[str], subtitle: str,
             open_it: bool) -> int:
    """出图 + 解码自检 + （可选）打开。返回 0/1。"""
    if not build_qr(url, out, ips, subtitle):
        return 0        # 没装 segno —— 地址已经印出来了，不算失败
    print(f"  二维码      {out}")

    status = verify_qr(out, url)
    if status is None:
        print("  自检        跳过解码验证（没装 zxing-cpp，装了就自动验）")
    elif status == "OK":
        print(f"  自检        ✔ 解码验证通过：图里确实是 {url}")
    else:
        print(f"  自检        ✗ {status}")
        print()
        print("  这张图别用 —— 先告诉我，我查。")
        return 1

    if open_it and not os.environ.get("PHONE_QR_NO_OPEN"):
        # ⚠️ 留一个环境变量开关，是为了让自动化测试能原样跑 phone-url.bat
        # 而不弹出一张图打扰用户（bat 里写死了 --open）。
        try:
            os.startfile(str(out))  # noqa: S606  Windows 专用
        except Exception as e:
            print(f"  （自动打开图片失败：{e}）")
    return 0


# ---------------------------------------------------------------- 两个入口

def do_lan(args) -> int:
    print()
    print("  ── 局域网（手机连同一个 WiFi）" + "─" * 18)
    ips = lan_ips() or local_fallback()
    if not ips:
        print("  没有找到可用的局域网地址。")
        print("  可能是没连任何网络 —— 手机和电脑要先在同一个 WiFi 下。")
        return 1

    ip = ips[0]
    url = f"http://{ip}:{args.port}"
    print(f"  电脑在局域网里的地址    {ip}")
    if len(ips) > 1:
        print(f"  （其它候选，一般用不上） {' / '.join(ips[1:])}")
    print()

    ok, detail = probe(url)
    if not ok:
        print(f"  手机链接    {url}   ← 但现在还打不开（{detail}）")
        print()
        print("  原因几乎一定是：服务只监听了本机（127.0.0.1），没开 LAN 模式。")
        print("  解决：关掉当前服务，双击项目里的  start-lan.bat  再试一次。")
        print()
        return 1

    print(f"  手机链接    {url}   ✔ 已确认从局域网可访问")
    print()
    return _emit_qr(url, pathlib.Path(args.out or DEFAULT_OUT), ips,
                    "手机需与电脑连同一个 WiFi", args.open)


def do_remote(args) -> int:
    print()
    print("  ── 远程（手机在任何网络）" + "─" * 20)
    try:
        info = _serve().tailscale_info()
        url = _serve().remote_url_from(info, args.port)
    except Exception as exc:
        print(f"  读不到 Tailscale 状态（{exc}）—— 跳过。")
        return 1

    if not url:
        print("  还没开通。")
        if not info["installed"]:
            print("  双击  start-remote.bat  装一下（免费私有网，出门也能用）。")
        else:
            print(f"  Tailscale 没就绪（{info['state'] or '未登录'}）"
                  " —— 双击  start-remote.bat")
        print()
        return 1

    print(f"  远程地址    {url}")
    print()
    ok, detail = probe(url)
    if ok:
        print(f"  远程链接    {url}   ✔ 从本机验证可访问")
    else:
        # ⚠️ **不能报成失败**：本机访问自己的 tailnet 地址常常不走回头路。
        # 这只能说明"本机探不到"，说明不了"手机打不开"。
        print(f"  远程链接    {url}")
        print(f"              从本机没探通（{detail}）—— 本机访问自己的 tailnet 地址")
        print("              常常不走回头路，**不代表**手机也不行，拿手机试一次。")
    print()
    return _emit_qr(url, pathlib.Path(args.out or DEFAULT_OUT_REMOTE), [],
                    "手机在任何网络都能开", args.open)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="打印手机访问地址并生成二维码")
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true", help="生成后用系统看图打开")
    ap.add_argument("--lan", action="store_true", help="只出局域网")
    ap.add_argument("--remote", action="store_true", help="只出远程")
    args = ap.parse_args()

    print()
    print("  胶囊衣橱 · 手机访问地址")
    print("  " + "-" * 46)

    # 默认两个都出；配了远程就两张卡片，没配就只出局域网（并说明怎么开远程）
    rc = 0
    if not args.remote:
        rc |= do_lan(args)
    if not args.lan:
        rc |= do_remote(args)

    print()
    print("  手机上怎么用")
    print("  " + "-" * 46)
    print("  1. 相机或微信扫码（或手动输上面那行网址）")
    print("  2. 打开后点浏览器菜单「添加到主屏幕」")
    print("     → 之后就是个全屏 App 图标，不用再输地址")
    print("  3. 要真装成 App，用**远程**那张（https）——")
    print("     局域网的 http 安卓 Chrome 不给装（只给快捷方式）")
    print()
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
