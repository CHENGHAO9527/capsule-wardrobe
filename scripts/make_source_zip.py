"""把源码打包成一份**可以给别人**的 zip。

    python scripts/make_source_zip.py
    python scripts/make_source_zip.py --out D:/share/capsule-src.zip
    python scripts/make_source_zip.py --list-only      # 只报告要打什么，不落盘

它做的事不止"压一下"：

  ① **排除你自己的东西**：`data/`（整柜衣服和照片）、`gate.json`（口令）、
     `.workbuddy/`、`tools/`、旧备份、自检输出、发布产物
  ② **打之前先扫真正要进包的内容**：口令、大模型 Key、`sk-` 一律不许出现 ——
     发现就**中止**，一个字节都不写
  ③ **个人痕迹**（你自己的线上网址、用户名、本机绝对路径）只报告、不中止 ——
     这类东西别人拿去跑不起来，但你自己看得到、能决定要不要留
  ④ **打完把 zip 里的内容再扫一遍** —— 防"扫的和打的不是同一批"

⚠️ 第 ② 步有个前提：**得先知道要找什么**。
   `data/` 被 `stage_deploy.py` 挪走时，本脚本读不到你的大模型 Key ——
   此时"没发现密钥"就是一句空话。所以这种情况它会**明说**，不装绿。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 根目录用**白名单**：宁可漏报（下面会点名"还有这些没进包"），也不要手滑把一个
# 新出现的敏感文件打进去。
TOP_FILES = [
    "requirements.txt", "README.md", "RUN-FIRST.md",
    "start.bat", "start-lan.bat", "start-remote.bat", "phone-url.bat",
]
TOP_DIRS = ["app", "web", "scripts", "docs"]

# ⚠️ 这两个文件都在**项目根**，也就是"配置"那一类 —— 它们会跟着发布上云
#    （见 app/vision.py 顶部：配置必须离开 `data/`，否则手机那份永远没有）。
#    所以**必须显式拦住**，不能靠"白名单里没写它们"来兜底：
#    那种保护在某次手滑加个通配符时就静默失效，而后果是把你的口令和模型 Key
#    一起送出去。打完包还会按这份名单**点名复验**。
SECRET_FILES = {
    "gate.json",   # 进门口令
    "llm.json",    # 模型 Key（明文）
}

# 路径任一段命中即排除
SKIP_PARTS = {"__pycache__", ".workbuddy", ".git", ".venv", "venv",
              "node_modules", "data", "tools", "backups", "dist"}
# 文件名/后缀命中即排除
SKIP_SUFFIX = (".out.txt", ".pyc", ".log", ".genie")
SKIP_PREFIX = ("serve.out", "credentials")
# 这些图里编着**你自己的**线上地址
SKIP_EXACT = {"docs/phone-qr-cloud.png"}

ARC_ROOT = "capsule-wardrobe"          # 解开是一个文件夹，不是一堆散文件
# 本脚本自己装着上面那些**对照样本**，扫它必然命中自己 —— 跳过它，
# 但这个跳过要**报出来**（沉默地跳过 = 造假绿）。
_SELF_ARC = f"{ARC_ROOT}/scripts/{Path(__file__).name}"

# 无条件要扫的：跟"你有没有配"无关。
# ⚠️ 值那一截要求是**像密钥的字符集**（字母数字 _ - .），不能写成 `[^"]{12,}`：
#    第一版就是那样，于是 `print("  api_key:", repr(cfg.get("api_key")))`
#    这行**普通代码**被判成明文密钥，直接把打包打断了。太松的规则 = 假警报，
#    而假警报的结局通常是"把断言放宽"，最后连真密钥也放过去。
ALWAYS_PATTERNS = [
    ("大模型 Key（sk- 形状）", re.compile(rb"sk-[A-Za-z0-9_\-]{16,}")),
    ("疑似明文密钥", re.compile(rb"api_key\"?\s*[:=]\s*\"[A-Za-z0-9_\-\.]{16,}\"")),
]
# 这两条规则得先证明**它自己还活着**：一个永远说"没发现"的扫描器，
# 和真的扫描器长得一模一样（本项目第 ⑧ 条）。
PATTERN_SAMPLES = [
    ("大模型 Key（sk- 形状）", b'  "api_key": "sk-abc1234567890abcdefgh",'),
    ("疑似明文密钥", b'{"api_key": "abcdefghijklmnop1234"}'),
]
PATTERN_NOT_SAMPLES = [
    b'        print("  api_key:", repr(cfg.get("api_key")), "| enabled:")',
    b'    cfg["api_key"] = ""',
]
# 只报告、不中止的底子；用户名和项目路径在 _secrets() 里现拼（别改模块级常量，会被叠加）
TRACE_BASE = [
    ("你自己的线上网址", re.compile(rb"[A-Za-z0-9\-]+\.app\.workbuddy\.host")),
    ("别的沙箱/隧道域名", re.compile(rb"\.(ts\.net|ngrok|trycloudflare)\b")),
]


def _pattern_selftest() -> list[str]:
    """规则自检：该抓的抓到、不该抓的别抓。返回问题列表。"""
    bad: list[str] = []
    table = dict(ALWAYS_PATTERNS)
    for label, sample in PATTERN_SAMPLES:
        rx = table.get(label)
        if rx is None:
            bad.append(f"规则「{label}」不在了，样本没法验")
        elif not rx.search(sample):
            bad.append(f"规则「{label}」抓不到自己的样本 → 它已经失效了")
    for sample in PATTERN_NOT_SAMPLES:
        for label, rx in ALWAYS_PATTERNS:
            if rx.search(sample):
                bad.append(f"规则「{label}」把普通代码误判成密钥：{sample.decode()[:50]}")
    return bad


# ══════════════════════════════════════════════
# 要打什么
# ══════════════════════════════════════════════

def _keep(rel: Path) -> bool:
    if any(part in SKIP_PARTS for part in rel.parts):
        return False
    if any(".bak-" in part for part in rel.parts):
        return False
    name = rel.name
    if name in SECRET_FILES:
        return False
    if name.startswith(SKIP_PREFIX) or name.endswith(SKIP_SUFFIX):
        return False
    if rel.as_posix() in SKIP_EXACT:
        return False
    return True


def collect() -> tuple[list[tuple[Path, str]], list[str]]:
    """→ ([(源文件, 包内路径)], [没进包的根目录项])"""
    out: list[tuple[Path, str]] = []
    for f in TOP_FILES:
        p = ROOT / f
        if p.is_file():
            out.append((p, f"{ARC_ROOT}/{f}"))
    for d in TOP_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if _keep(rel):
                out.append((p, f"{ARC_ROOT}/{rel.as_posix()}"))

    packed_roots = set(TOP_FILES) | set(TOP_DIRS)
    left = sorted(x.name for x in ROOT.iterdir() if x.name not in packed_roots)
    return out, left


# ══════════════════════════════════════════════
# 秘密 / 痕迹
# ══════════════════════════════════════════════

def _secrets() -> tuple[list[tuple[str, bytes]], list[str], list[tuple[str, "re.Pattern[bytes]"]]]:
    """读出"要找什么"。→ ([(说明, 字节)], [警告], [(痕迹说明, 正则)])

    前两项是**致命**的（命中就中止）；第三项只报告。
    """
    found: list[tuple[str, bytes]] = []
    warns: list[str] = []
    traces: list[tuple[str, "re.Pattern[bytes]"]] = list(TRACE_BASE)

    gp = ROOT / "gate.json"
    if gp.exists():
        try:
            pw = (json.loads(gp.read_text(encoding="utf-8")) or {}).get("password")
            if isinstance(pw, str) and pw.strip():
                found.append(("你的口令", pw.strip().encode()))
            else:
                warns.append("gate.json 里没有可用的 password → 口令这一项**没在扫**")
        except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
            warns.append("gate.json 读不出来 → 口令这一项**没在扫**")
    else:
        warns.append("没有 gate.json → 口令这一项**没在扫**")

    # 模型 Key 现在在**项目根**（2026-10-01 从 data/vision.json 迁过来）。
    # ⚠️ 这个查找顺序必须和 `app/vision.py` 的 `_config_source()` **保持一致** ——
    #    不一致的话，扫描器读的是老文件、应用读的是新文件，
    #    于是"扫了但扫的不是那份"，而报告看着一切正常（第 ⑧ 条）。
    vp = next((c for c in (ROOT / "llm.json", ROOT / "data" / "vision.json") if c.exists()), None)
    if vp is not None:
        try:
            key = (json.loads(vp.read_text(encoding="utf-8")) or {}).get("api_key")
            if isinstance(key, str) and len(key.strip()) >= 8:
                found.append(("你的大模型 Key", key.strip().encode()))
            else:
                warns.append(f"{vp.name} 里没有 api_key → Key 这一项**没在扫**")
        except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
            warns.append(f"{vp.name} 读不出来 → Key 这一项**没在扫**")
    else:
        # ⚠️ 这条是关键：`data/` 不在时，上面那些无条件正则也帮不上忙 ——
        #    明文密钥在**别的格式**下（比如没带 api_key 字样）就扫不出来。
        #    所以这里必须说出来，不能让"没发现"冒充"安全"。
        warns.append("找不到 llm.json / data/vision.json → **扫不到你的 Key**。"
                     "要打分享包请先在设置页把模型配好（或先 restore）。")

    # 本机痕迹：用户名 + 项目绝对路径
    try:
        user = Path.home().name
    except (RuntimeError, OSError):
        user = ""
    if user:
        traces.append((f"本机用户名（{user}）", re.compile(re.escape(user).encode())))
    for label, pat in (("项目绝对路径（含盘符）", re.compile(re.escape(str(ROOT)).encode())),
                       ("项目绝对路径（斜杠版）", re.compile(re.escape(ROOT.as_posix()).encode()))):
        traces.append((label, pat))

    return found, warns, traces


def _snippet(data: bytes, rx: "re.Pattern[bytes]") -> str:
    """命中处前后各截一点 —— 报告里必须看得见**具体是什么**，
    否则只能靠猜，而猜的结果通常是"把断言放宽"（本项目第 ③ 条）。"""
    m = rx.search(data)
    if not m:
        return ""
    lo = max(0, m.start() - 24)
    hi = min(len(data), m.end() + 24)
    return data[lo:hi].decode("utf-8", "replace").replace("\n", "⏎")


def _scan(chunks: list[tuple[str, bytes]],
          secrets: list[tuple[str, bytes]],
          traces: list[tuple[str, "re.Pattern[bytes]"]]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """→ (致命命中, {痕迹说明: [文件…]}, 被跳过的文件)

    ⚠️ 跳过的文件会被**原样返回**，由调用方打印出来。悄悄跳过 = 把"没扫"
    说成"没问题"，这正是本项目第 ⑧ 条。
    """
    fatal: list[str] = []
    trace: dict[str, list[str]] = {}
    skipped: list[str] = []

    for name, data in chunks:
        if name == _SELF_ARC:                  # 本脚本装着对照样本，扫自己必命中
            skipped.append(name)
            continue
        for label, needle in secrets:
            if needle in data:
                fatal.append(f"{name}  ← 有{label}")
        for label, rx in ALWAYS_PATTERNS:
            if rx.search(data):
                fatal.append(f"{name}  ← 命中「{label}」：…{_snippet(data, rx)}…")
        for label, rx in traces:
            if rx.search(data):
                trace.setdefault(label, []).append(name)
    return fatal, trace, skipped


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="打一份可分享的源码包")
    ap.add_argument("--out", metavar="PATH", help="输出路径（默认 dist/capsule-wardrobe-src-日期.zip）")
    ap.add_argument("--list-only", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    files, left = collect()
    if not files:
        print("❌ 一个文件都没收集到，先确认在项目根跑。")
        return 1

    groups: dict[str, int] = {}
    for _, arc in files:
        top = arc[len(ARC_ROOT) + 1:].split("/")[0]
        groups[top] = groups.get(top, 0) + 1
    total = sum(p.stat().st_size for p, _ in files)

    print("=== 要打进去 ===")
    for k in sorted(groups):
        print(f"  {k:<20} {groups[k]:>4} 个文件")
    print(f"  {'合计':<20} {len(files):>4} 个文件 / {total / 1048576:.1f} MB（未压缩）")

    print("\n=== 没打进去的根目录项（确认一下这些是故意排除的） ===")
    for n in left:
        print("  ·", n)

    secrets, warns, traces = _secrets()
    print("\n=== 安全扫描（打之前） ===")
    bad_rules = _pattern_selftest()
    if bad_rules:
        print(f"  ❌ 扫描规则自检没过（{len(bad_rules)} 条）—— 先修规则，别信它的结论：")
        for b in bad_rules:
            print("     -", b)
        return 1
    print(f"  ok   扫描规则自检通过（{len(PATTERN_SAMPLES)} 个样本抓得到、"
          f"{len(PATTERN_NOT_SAMPLES)} 个反例不误判）")
    for w in warns:
        print("  ⚠️ ", w)
    if secrets:
        print("  要拦的字符串：" + "、".join(l for l, _ in secrets))
    chunks = [(arc, p.read_bytes()) for p, arc in files]
    fatal, trace, skipped = _scan(chunks, secrets, traces)
    if skipped:
        print(f"  ok   跳过 {len(skipped)} 个文件不扫：{', '.join(skipped)}"
              f"（本脚本装着对照样本，扫它必命中自己）")
    else:
        print("  ⚠️  本该跳过的本脚本没出现在待扫清单里 —— 跳过这一步是不是没生效？")
    if fatal:
        print(f"  ❌ 命中 {len(fatal)} 处 —— 中止，不写任何文件：")
        for f in fatal[:20]:
            print("     -", f)
        return 1
    print("  ok   没有发现口令 / 密钥")
    if trace:
        print("  ⚠️  个人痕迹（不中止，你自己决定要不要留）：")
        for label, hits in trace.items():
            print(f"     - {label}：{len(hits)} 个文件，例如 {hits[0]}")
    else:
        print("  ok   没有发现个人网址 / 用户名 / 绝对路径")

    if args.list_only:
        print("\n（--list-only：没有写文件）")
        return 0

    out = Path(args.out) if args.out else \
        DIST / f"capsule-wardrobe-src-{datetime.now():%Y%m%d}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p, arc in files:
            z.write(p, arc)

    # ④ 回头把**真写进去的内容**再扫一遍：扫的和打的如果不是同一批，前面全白扫
    with zipfile.ZipFile(out) as z:
        back = [(i.filename, z.read(i.filename)) for i in z.infolist()]
    if len(back) != len(files):
        print(f"\n❌ 回读到的条目数（{len(back)}）与要打的（{len(files)}）不一致 —— 中止")
        out.unlink(missing_ok=True)
        return 1
    fatal2, _, _ = _scan(back, secrets, traces)
    if fatal2:
        print(f"\n❌ 回读又扫出 {len(fatal2)} 处命中 —— 删掉这个包：")
        for f in fatal2[:20]:
            print("   -", f)
        out.unlink(missing_ok=True)
        return 1

    # ④-b **点名复验**：那几个敏感文件一个都不许进包。
    #      上面那条是按**内容**找线索，这条是按**名字**点名 —— 两者防的不是同一件事：
    #      内容扫描认不出"格式陌生"的密钥（第 ⑧ 条说过的），而点名复验认不出
    #      "密钥被抄进别的文件"。两条都在，才敢说"包里没有"。
    leaked = sorted({Path(n).name for n, _ in back} & SECRET_FILES)
    if leaked:
        print(f"\n❌ 敏感文件进了包：{leaked} —— 删掉这个包")
        out.unlink(missing_ok=True)
        return 1

    size = out.stat().st_size
    print(f"\n=== 打好了 ===")
    print(f"  {out}")
    print(f"  {len(back)} 个文件 | 压缩后 {size / 1048576:.1f} MB")
    print(f"  点名复验：{'、'.join(sorted(SECRET_FILES))} 都不在包里")
    print(f"  回读复扫通过（扫的和打的确实是同一批）")
    print(f"\n  别人拿到之后：解压 → 看 RUN-FIRST.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
