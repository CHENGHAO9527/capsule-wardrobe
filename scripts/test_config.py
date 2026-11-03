"""配置归属自检 —— 「配置」和「状态」不能放错那一半。

    python scripts/test_config.py
    python scripts/test_config.py --out scripts/test_config.out.txt

为什么要有这个文件
------------------
2026-10-01 用户报：「感觉手机上逻辑有点不统一啊，明明看着今日页已经配了大模型，
在录入的地方又让我输入 API」。

根因跟前端无关：**模型配置放错了地方**。它在 `data/vision.json`，
而发布时 `data/` 整个被挪走（那正是"手机上改的留得住"的实现方式）
→ 云上那份永远没有 Key → 手机上的录入页就催你去配一个**电脑上早就配好**的东西。

`app/gate.py` 顶部为口令论证过同一件事。这类文件因此分成两半：

  · **源码那一半**（跟着每次发布走）：口令 `gate.json`、模型配置 `llm.json`
  · **状态那一半**（每台设备各留各的）：`data/` 里的库和图片

放错哪一半，后果都**不报错**（页面看着一切正常），所以必须由断言盯着：
  · 配置落进 `data/` → 云上永远读不到（这次的 bug）
  · 配置落进分享包 → **口令和模型 Key 被送给拿到源码的人**（更严重）

全程用临时目录，**不碰你正在用的配置**；打出来的包也落临时目录，不污染 `dist/`。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import vision  # noqa: E402

_FAILS: list[str] = []


def _fail(msg: str) -> None:
    _FAILS.append(msg)
    print(f"  FAIL {msg}")


def _ok(msg: str) -> None:
    print(f"  ok   {msg}")


# ══════════════════════════════════════════════
# A. 它在哪一半
# ══════════════════════════════════════════════

def part_a() -> None:
    print("\n[A] 模型配置属于『源码那一半』—— 必须在项目根，不在 data/ 下")
    p = vision.CONFIG_PATH
    try:
        rel = p.relative_to(ROOT).as_posix()
    except ValueError:
        _fail(f"CONFIG_PATH 跑到项目外面去了：{p}")
        return

    if p.parent == ROOT:
        _ok(f"就放在项目根：{rel}")
    else:
        _fail(f"不在项目根（现在 {rel}）—— 发布时会被甩掉，云上就永远没有 Key")

    if "data" in p.parts:
        _fail("路径里带 `data/` —— 那个目录默认不发布，放里面等于没配")
    else:
        _ok("路径里没有 `data/`")

    leg = [x.relative_to(ROOT).as_posix() for x in vision.LEGACY_CONFIG_PATHS]
    if "data/vision.json" in leg:
        _ok(f"老路径仍登记着（只读兼容，升级上来的机器不丢配置）：{'、'.join(leg)}")
    else:
        _fail(f"老路径没登记对：{leg} —— 老机器升级后配置会凭空消失")


# ══════════════════════════════════════════════
# B. 读哪个（顺序反了会静默读到旧配置）
# ══════════════════════════════════════════════

def _cfg(provider: str) -> str:
    return json.dumps({"provider": provider, "base_url": "http://example.invalid",
                       "model": "m", "enabled": True})


def part_b() -> None:
    print("\n[B] 读哪个：新路径优先，老路径兜底（顺序反了是静默的）")
    real_new, real_leg = vision.CONFIG_PATH, vision.LEGACY_CONFIG_PATHS
    try:
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            new, old = t / "llm.json", t / "data" / "vision.json"
            old.parent.mkdir(parents=True)
            vision.CONFIG_PATH = new
            vision.LEGACY_CONFIG_PATHS = (old,)

            if vision.load_config().get("enabled"):
                _fail("一个文件都没有，enabled 却是开的")
            else:
                _ok("都没配 → enabled=False（不假装能用）")

            old.write_text(_cfg("OLD"), encoding="utf-8")
            if vision.load_config().get("provider") == "OLD":
                _ok("只有老文件 → 读得到（老机器升级不丢配置）")
            else:
                _fail("只有老文件时读不到 —— 老机器升级后配置会凭空消失")

            new.write_text(_cfg("NEW"), encoding="utf-8")
            got = vision.load_config().get("provider")
            if got == "NEW":
                _ok("两个都在 → 读新的（这次修的就是这一点）")
            else:
                _fail(f"两个都在时读到了 {got!r} —— 顺序反了，"
                      "用户在新路径改的配置会被老文件盖住，而且不报错")

            before = old.read_text(encoding="utf-8")
            vision.save_config({"provider": "SAVED"})
            saved = json.loads(new.read_text(encoding="utf-8")).get("provider") \
                if new.exists() else None
            if saved == "SAVED":
                _ok("保存 → 落在新路径（下次发布就跟着上云了）")
            else:
                _fail(f"保存没落到新路径（新文件里是 {saved!r}）")
            if old.read_text(encoding="utf-8") == before:
                _ok("老文件没被动过（只读兼容就该只读）")
            else:
                _fail("保存顺手改了老文件 —— 说好的只读呢")
    finally:
        vision.CONFIG_PATH, vision.LEGACY_CONFIG_PATHS = real_new, real_leg


# ══════════════════════════════════════════════
# C. 两侧的对称约束：发布要带上、分享包要拦住
# ══════════════════════════════════════════════

def part_c() -> None:
    print("\n[C] 发布要带上它，分享包要拦住它 —— 两侧都得断言")
    import make_source_zip as msz  # noqa: PLC0415
    import stage_deploy as sd      # noqa: PLC0415

    hit = [x for x in (*sd.MOVES, *sd.GLOBS)
           if x == "llm.json" or fnmatch.fnmatch("llm.json", x)]
    if hit:
        _fail(f"stage_deploy 会把 llm.json 挪走 {hit} → 云上又没 Key 了")
    else:
        _ok("搬运清单里没有它 → 跟着发布上云")

    if "llm.json" in sd.SANITIZE:
        _fail("llm.json 还在 SANITIZE 里 —— 发布时密钥被摘掉，等于没带")
    else:
        _ok("不在 SANITIZE 里（密钥会跟着上去，这是 2026-10-01 定的）")

    if msz._keep(Path("llm.json")):
        _fail("make_source_zip 会把 llm.json 打进包 —— **你的模型 Key 会跟着源码送出去**")
    else:
        _ok("make_source_zip._keep 拦住了它 → 不进分享包")

    missing = {"gate.json", "llm.json"} - set(msz.SECRET_FILES)
    if missing:
        _fail(f"SECRET_FILES 少了 {sorted(missing)} —— 打完包的点名复验会漏")
    else:
        _ok("SECRET_FILES 里口令和 Key 都在（打完包按它点名复验）")


def part_d() -> None:
    """端到端：真的打一个包（落临时目录），按名字点名看有没有漏。

    前面几条是"读常量"，这一条是"看产物" —— 常量对但产物错是完全可能的
    （比如白名单某天加了通配符）。本项目第 ⑧ 条：关键元素要点名测量。
    """
    print("\n[D] 真打一个包，点名复验（不是读常量，是看产物）")
    import make_source_zip as msz  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.zip"
        argv = sys.argv[:]
        try:
            sys.argv = ["make_source_zip.py", "--out", str(out)]
            rc = msz.main()
        except SystemExit as e:
            rc = e.code or 0
        finally:
            sys.argv = argv

        if rc != 0 or not out.exists():
            _fail(f"打包没成（rc={rc}）—— 后面几条没法验，先去看上面为什么")
            return

        with zipfile.ZipFile(out) as z:
            names = z.namelist()
        leaked = sorted({Path(n).name for n in names} & msz.SECRET_FILES)
        if leaked:
            _fail(f"包里出现了 {leaked} —— 这个包绝对不能发出去")
        else:
            _ok(f"包里 {len(names)} 个文件，没有任何口令 / 模型 Key 文件")
        if any("/data/" in n for n in names):
            _fail("包里带了 data/ —— 你的衣服和照片会被一起送出去")
        else:
            _ok("包内没有 data/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="把输出同时写一份到这个文件")
    args = ap.parse_args()

    buf: list[str] = []
    orig = print

    def tee(*a, **kw):
        s = " ".join(str(x) for x in a)
        buf.append(s)
        orig(*a, **kw)

    globals()["print"] = tee
    tee(f"# test_config · 生成于 {datetime.now():%Y-%m-%d %H:%M:%S}")
    part_a()
    part_b()
    part_c()
    part_d()

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
