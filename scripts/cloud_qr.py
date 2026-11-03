"""云端网址的二维码 —— 手机扫一下就打开，不用连同一个 WiFi、电脑也不用开。

和 `phone_qr.py` 的分工：
  - `phone_qr.py` 算的是**本机地址**（局域网 IP / Tailscale），每次现算、还要探可达。
  - 这个用的是**云端固定网址**，不用算、不用探 —— 所以独立成一条，别互相污染。

    python scripts/cloud_qr.py            # 出图到 docs/phone-qr-cloud.png
    python scripts/cloud_qr.py --open     # 出图并打开
    python scripts/cloud_qr.py --url https://别的域名/

复用 `phone_qr` 的卡片样式与**解码回读自检**（二维码坏了是典型静默失败：
扫不出来的人只会怀疑网络，不会怀疑图）。
"""
import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import phone_qr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 你自己的线上地址。换部署就设环境变量 `CLOSET_URL`，**别改代码** ——
# 这份源码是要分享出去的，里面不该钉死谁的域名（别人跑起来会打到我的站上）。
CLOUD_URL = os.environ.get("CLOSET_URL") or "https://your-app.example.com/"
DEFAULT_OUT = ROOT / "docs" / "phone-qr-cloud.png"


def main() -> int:
    ap = argparse.ArgumentParser(description="胶囊衣橱 云端网址二维码")
    ap.add_argument("--url", default=CLOUD_URL)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--open", action="store_true", help="出图后打开看一眼")
    args = ap.parse_args()

    url = args.url
    out = pathlib.Path(args.out)
    print(f"  云端网址    {url}")

    # ips 传空 —— 云端只有一个固定地址，没有"换个 IP 试试"这一说
    if not phone_qr.build_qr(url, out, [], subtitle="手机随时可开 · 不用连同一个 WiFi · 电脑不用开机"):
        return 0
    print(f"  二维码      {out}")

    status = phone_qr.verify_qr(out, url)
    if status is None:
        print("  自检        跳过解码验证（没装 zxing-cpp）")
    elif status == "OK":
        print(f"  自检        ✔ 解码验证通过：图里确实是 {url}")
    else:
        print(f"  自检        ✗ {status}")
        print("\n  这张图别用 —— 先告诉我，我查。")
        return 1

    if args.open:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", str(out)])  # noqa: S603,S607
        except Exception as exc:  # noqa: BLE001 - 打不开不算失败
            print(f"  （打不开看图工具：{exc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
