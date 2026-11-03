"""口令门的管理口 —— 生成 / 更换 / 查看 / 关闭。

用法（在项目根跑）：
    python scripts/set_gate_password.py            随机生成一个新口令并写入（换口令也用它）
    python scripts/set_gate_password.py --show     看当前状态（含口令本身）
    python scripts/set_gate_password.py --set 你的口令
    python scripts/set_gate_password.py --off      删掉口令文件 = 关掉门

⚠️ 口令文件放**项目根**（`gate.json`），不放 `data/`。
   因为挂到云上之后 `data/` 是"应用状态"，而源码是"每次发布都会带上去的东西"。
   放源码里，门才永远在。详见 `app/gate.py` 顶部。

改完不用重启服务：`gate.state()` 按 mtime 重新读。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gate  # noqa: E402  （要先把项目根塞进 sys.path）

# 去掉 0/O/1/l/I 这些"看着一样"的字符 —— 这个口令要在手机上敲
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def make_password() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def main() -> int:
    ap = argparse.ArgumentParser(description="胶囊衣橱 · 口令门管理")
    ap.add_argument("--set", metavar="PASSWORD", help="直接指定口令")
    ap.add_argument("--show", action="store_true", help="只看当前状态")
    ap.add_argument("--off", action="store_true", help="删掉口令文件，关掉门")
    ap.add_argument("--bare", action="store_true", help="只输出口令一行（给脚本用）")
    args = ap.parse_args()

    target = gate.GATE_CANDIDATES[0]          # 项目根 gate.json

    if args.show:
        st = gate.state()
        where = gate.where()
        if not st["enabled"]:
            print("口令门：**关着**（没找到口令文件）")
            print(f"  → 打开：python scripts/set_gate_password.py")
            return 0
        if st["broken"]:
            print(f"口令门：**关死** —— 文件在但读不出可用口令：{where}")
            print(f"  → 修：删了重建，或 python scripts/set_gate_password.py --set 新口令")
            return 0
        print("口令门：**开着**")
        print(f"  文件：{where}")
        print(f"  口令：{st['pw']}")
        print("  （改完不用重启服务）")
        return 0

    if args.off:
        removed = []
        for cand in gate.GATE_CANDIDATES:
            if cand.exists():
                cand.unlink()
                removed.append(cand)
        if removed:
            for p in removed:
                print(f"已删除 {p}")
            print("口令门已关掉 —— 现在谁都能直接打开。⚠️ 挂公网前记得再打开。")
        else:
            print("本来就没开。")
        return 0

    pw = (args.set or "").strip() or make_password()
    if len(pw) < 8:
        print("口令太短了（至少 8 位）。没写任何东西。", file=sys.stderr)
        return 1

    target.write_text(json.dumps({"password": pw}, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    if args.bare:
        print(pw)
    else:
        print(f"已写入 {target}")
        print(f"口令：{pw}")
        print("⚠️ 这个口令就是你在手机上开衣橱要敲的东西，存进密码管理器。")
        print("   它也会随源码一起被发布到线上（服务端用，浏览器拿不到）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
