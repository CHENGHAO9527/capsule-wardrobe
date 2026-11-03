"""远程访问：把「出门也能用手机打开」这条路一次走通。

    python scripts/setup_remote.py             检查并配置（该做什么就做什么）
    python scripts/setup_remote.py --status    只看状态，不改任何东西

为什么要做这件事
----------------
局域网那套（`start-lan.bat`）只在**手机和电脑连同一个 WiFi** 时成立。
出门在外（地铁上、公司、回老家）要用，得靠 **Tailscale**：电脑和手机各装一个、
登录**同一个账号**，就组成一张私有网。它跟"把端口开放到公网"是两回事：

  · 只有你自己登录的设备能连（不是"谁有网址谁能连"）
  · 流量端到端加密、直连，衣服照片和衣橱数据**不出这台电脑**
  · 免费（个人用足够）

三道门，缺一道手机就打不开
--------------------------
  ① **装**     电脑和手机各一个 Tailscale
  ② **登录**   电脑这边跑 `tailscale up`，它会打印一个网址，浏览器登录
  ③ **反代**   `tailscale serve` 把本机端口变成
                `https://<机器名>.<tailnet>.ts.net`
                （顺带拿到证书 —— 于是浏览器认它是"安全上下文"，
                  安卓 Chrome 才愿意把它**装成 App**；局域网那种 http 不给装）

⚠️⚠️ 用 `serve`，**绝不用 `funnel`**
----------------------------------
`serve` = 只有你的设备能连（私有）；`funnel` = 挂到**公网**（谁拿到网址谁进）。
两者只差一个词，后果差一个量级 —— 而本应用是**零鉴权**的
（`api_key` 只是大模型配置，不是登录密码），裸暴露到公网等于把衣橱交给别人删。
这个脚本因此**只调 serve**，永远不碰 funnel。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve  # noqa: E402  复用它的判定 —— 不写第二套"装没装/通不通"的逻辑

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def head(t: str) -> None:
    print()
    print("  " + "-" * 52)
    print(f"  {t}")
    print("  " + "-" * 52)


def say(t: str = "") -> None:
    print(f"  {t}" if t else "")


# ────────────────────────────────────────────── 跑 tailscale

def ts_run(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """跑一条 tailscale 子命令，返回 `(退出码, 合并后的输出)`。

    ⚠️ 不用 `subprocess.run(check=True)`：CLI 返回非零**不是程序出错** ——
    "没提权""没登录""tailscaled 没起来"全都是**要讲给用户听的状态**。
    把它们当异常抛掉，用户只会看到一句 traceback，然后不知道该干嘛。
    """
    exe = serve.tailscale_exe()
    if not exe:
        return 127, "tailscale.exe 不存在"
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout:.0f} 秒）"
    except OSError as exc:
        return 126, f"启动失败：{exc}"
    blob = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, blob.strip()


def _permission_hint(out: str) -> bool:
    """输出里像不像"权限不够"。Tailscale 在 Windows 上操作 tailscaled 要管理员。"""
    low = out.lower()
    return any(k in low for k in ("access is denied", "access denied",
                                  "requires elevation", "administrator",
                                  "权限", "拒绝访问"))


# ────────────────────────────────────────────── 三道门

def gate_installed() -> str | None:
    exe = serve.tailscale_exe()
    if exe:
        say(f"✓ 装好了        {exe}")
        return exe
    say("✗ 还没装        Tailscale")
    print()
    say("  下一步：双击项目里的")
    say("      tools\\tailscale-setup.exe")
    say("  按提示装完（会问一次管理员权限），再跑一次这个脚本。")
    return None


def gate_login(exe: str, check_only: bool) -> bool:
    """没登录就去登录。返回"现在是否已登录"。"""
    info = serve.tailscale_info(timeout=8)
    if info["ok"]:
        say(f"✓ 已加入私有网  {info['dns'] or info['ip']}")
        if info["ip"]:
            say(f"                设备地址 {info['ip']}")
        return True

    state = info["state"] or "读不到状态"
    say(f"✗ 还没登录      （{state}）")
    if check_only:
        return False

    print()
    head("登录 —— 第 ② 道门")
    say("接下来 tailscale 会打印一个网址（login.tailscale.com/...）。")
    say("用**电脑浏览器**打开它、登录 Tailscale 账号。")
    say("然后手机上装 Tailscale App、登**同一个账号**，两边就互通了。")
    say()
    say("⚠️ 如果它说权限不够，就把  start-remote.bat  右键「以管理员身份运行」。")
    print()

    # ⚠️ 故意**不捕获输出**：让 tailscale 直接往这个终端里打字。
    # 它自己会把登录网址印得很清楚；我们再去解析一遍反而容易漏（版本间格式会变）。
    try:
        proc = subprocess.Popen([exe, "up"], creationflags=NO_WINDOW)
    except OSError as exc:
        say(f"✗ 起不来：{exc}")
        return False
    try:
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        print()
        say("⚠️ 等了 10 分钟还没登录完 —— 先关掉，等你有空再双击一次 start-remote.bat。")
        return False

    print()
    if proc.returncode != 0:
        code, out = ts_run(["status"])
        say(f"✗ 登录没成功（退出码 {proc.returncode}）。")
        if _permission_hint(out):
            say("  看起来是权限问题 —— 请右键  start-remote.bat  →「以管理员身份运行」。")
        elif out:
            say(f"  tailscale 说：{out[:200]}")
        return False

    say("✓ 登录完成。")
    return serve.tailscale_info(timeout=8)["ok"]


def gate_serve(exe: str, port: int, check_only: bool) -> bool:
    """配 `tailscale serve`（反代 + https）。返回"现在是否已配好"。"""
    info = serve.tailscale_info(timeout=8)
    if info["https"]:
        say("✓ 已配好反代    https（能装成 App）")
        return True

    say("✗ 还没配反代    现在是 http，装不成 App")
    if check_only:
        return False

    print()
    head("反代 + 证书 —— 第 ③ 道门")
    say(f"把本机 {port} 端口挂到私有网里，并申请一张证书。")
    print()

    code, out = ts_run(["serve", "--bg", str(port)], timeout=90)
    low = out.lower()
    if code != 0 and ("https" in low and ("not enabled" in low or "disabled" in low
                                          or "cert" in low)):
        say("✗ 你的私有网还没开 HTTPS 证书。去开一次（点一下就行）：")
        print()
        say("      https://login.tailscale.com/admin/dns")
        say("      找到  HTTPS Certificates  →  Enable")
        print()
        say("  开完再双击一次 start-remote.bat。")
        return False
    if code != 0:
        say(f"✗ 配置失败（退出码 {code}）")
        if out:
            say(f"  tailscale 说：{out[:300]}")
        if _permission_hint(out):
            say("  看起来是权限问题 —— 请右键  start-remote.bat  →「以管理员身份运行」。")
        return False

    if out:
        say(out[:300])
    say("✓ 配好了。")
    return serve.tailscale_info(timeout=8)["https"]


# ────────────────────────────────────────────── 报告

def report(port: int) -> int:
    info = serve.tailscale_info(timeout=8)
    url = serve.remote_url_from(info, port)

    head("现在怎么用")
    if not url:
        say("远程入口还没就绪 —— 先按上面的提示走完那三道门。")
        return 1

    say(f"远程地址    {url}")
    say("            （手机在任何网络下都能打开，不用跟电脑同一个 WiFi）")
    print()
    say("手机上：")
    say("  1. 装 Tailscale App，登录**同一个账号**（这一步不做，地址打不开）")
    say("  2. 浏览器打开上面的地址")
    say("  3. 菜单里选「添加到主屏幕」/「安装应用」")
    if info["https"]:
        say("     → https 是「安全上下文」，浏览器才肯真把它装成 App")
    else:
        say("     ⚠️ 现在是 http，安卓 Chrome 只会给个快捷方式（不装也行，照用）")
    print()
    say("出二维码：双击  phone-url.bat")
    print()
    say("⚠️ 电脑要开着（手机只是显示器，衣服和照片都在这台电脑上）。")
    say("⚠️ 别用 `tailscale funnel` —— 那是挂到公网，谁有网址谁能改你的衣橱。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把远程访问这条路走通")
    ap.add_argument("--status", action="store_true", help="只看状态，不做任何改动")
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print()
    print("  胶囊衣橱 · 远程访问（出门也能用手机打开）")

    head("检查" if not args.status else "状态")
    exe = gate_installed()
    if not exe:
        return 1
    if not gate_login(exe, args.status):
        return 1

    if not args.status:
        gate_serve(exe, args.port, args.status)

    return report(args.port)


if __name__ == "__main__":
    sys.exit(main())
