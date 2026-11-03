"""发布上云前后的一次性搬运工具。

发布时整个项目目录会被打包上传，所以几类东西要临时挪出项目：
  ① 云上完全用不到的（Windows 安装包、图片/库的旧备份、自检输出）
  ② **`data/` 整个目录 —— 这是默认行为**

⚠️ **模型配置（`llm.json`）不在被挪走之列 —— 它就该跟着上云。**
   2026-10-01 改的：它原来放在 `data/vision.json`，于是每次发布都被甩掉，
   云上那份永远没有 Key → 手机上「录入」页让你去填一个电脑上已经填过的 API。
   （用户原话："感觉手机上逻辑有点不统一啊"。）
   配置和口令一样属于**源码那一半**，不属于会被重写的应用状态 ——
   所以搬到了项目根，和 `gate.json` 并列。详见 `app/vision.py` 顶部。
   ⚠️ 因此 `make_source_zip.py` 必须把它和 gate.json 一起排除（分享包不能带 Key）。

用法：
  python scripts/stage_deploy.py stage              # 挪走（含 data/）—— **日常就用这个**
  python scripts/stage_deploy.py stage --with-data  # 例外：把本机 data 也传上去
  python scripts/stage_deploy.py verify             # 看一眼现在的状态
  python scripts/stage_deploy.py restore            # 原样搬回

⚠️ **没有 `gate.json` 它会拒绝发布**（要发不设门的站点得显式加 `--allow-no-gate`）。
   理由：本机免密是设计（坐在自己电脑前不该被自己的门拦下），
   但同一个"没门"发到公网上 = 谁拿到网址谁能改。这一条**只有发布时拦得住**。

搬走的东西放在**项目旁边的 `closet-stage/`**（原来是写死 `E:/closet-stage`，
本机正好在那儿，别人拿到源码就没法跑了）。要换位置设 `CLOSET_STAGE`。

⚠️ 为什么默认**不带** `data/`：云上有它自己那一份（手机上改的东西全在那儿）。
   带着本机 `data/` 发布 = **拿电脑那份顶掉云上那份**，手机上改的一并没了。
   2026-09-30 实测过：不带 data 发布，**云上那份完整保留**（标记值 23.5 与 27 件都活着）。
   所以"手机改了要留得住"这件事，只靠这一个开关就成立 —— 不需要账号、不需要云数据库。

⚠️ `--with-data` 只在一种场合用：**云上那份丢了/要重置，拿本机备份推回去**。
   用了它，云上手机上改的所有东西都会被本机这份覆盖。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# stage 目录就放在项目**旁边**。原来是写死 `E:/closet-stage` ——
# 本机正好就是这里，但别人拿到源码跑就会去 E: 盘建目录，没有 E: 盘直接崩。
STAGE = Path(os.environ.get("CLOSET_STAGE") or ROOT.parent / "closet-stage")
MANIFEST = STAGE / "_manifest.json"
# ⚠️ 只认**项目根**这一份，不认 `data/gate.json`：`--no-data` 会把整个 `data/`
#    挪走，放在里面的口令跟着走 → 云上等于没门。见 app/gate.py 顶部的论证。
GATE_FILE = ROOT / "gate.json"

# 整个挪走（目录或文件）
MOVES = [
    "tools",                 # Windows 安装包，Linux 沙箱里没有运行价值
    "backups",               # 从云上拉回来的备份包（同级的另一个 25MB），别上传
]
# 按通配挪走
GLOBS = [
    "data/images.bak-*",     # 图片目录旧备份（约 24MB，纯占体积）
    "data/closet.db.bak-*",  # 旧库备份
    "data-before-pull-*",    # pull_cloud.py --unpack 留下的"旧 data 留档"
    "*.out.txt",             # 自检输出
]
# ⚠️ 这一条现在只针对**老路径残留**（2026-10-01 之前配置在 `data/vision.json`）。
#    默认发布（不带 data）时整个 `data/` 被挪走，根本走不到这里；
#    只有 `--with-data` 会路过，那时顺手把老文件里的密钥摘掉、免得它顺手被带上去。
#    新路径 `llm.json` 在项目根，**不摘** —— 它就该带着 Key 上云（见文件头那段）。
# ⚠️ `stage` 默认不带 data ⇒ 云上那份 data 原样保留 ⇒ 手机上的改动留得住。
SANITIZE = ["data/vision.json"]
# `--no-data` 时，这些整块跳过（data 已经整个被挪走了）
DATA_GLOBS = {"data/images.bak-*", "data/closet.db.bak-*"}


def _stage_path(rel: str) -> Path:
    p = STAGE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _strip_key(rel: str) -> None:
    """base_url / model 全留着（用户上云后只需补一个 key），只摘掉密钥并关掉开关。"""
    src = ROOT / rel
    if not src.exists():
        return
    cfg = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        return
    cfg["api_key"] = ""
    cfg["enabled"] = False
    src.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _move(rel: str, done: list[dict]) -> None:
    src = ROOT / rel
    if not src.exists():
        return
    shutil.move(str(src), str(_stage_path(rel)))
    done.append({"rel": rel, "mode": "move"})
    print("· 挪走", rel)


def _gate_ok() -> bool:
    """发布前必须有一道门。

    这是**唯一**能挡住"把衣柜挂到公网上任人读写"的东西。它拦的不是恶意，
    是"忘了" —— 而忘了的时候，本地一路绿灯、发布也成功、页面上什么异常都没有，
    只有"别人也能改"这一件事是静默的。所以这里宁可直接不发。
    """
    if GATE_FILE.exists():
        return True
    print("⛔ 没有 gate.json —— 不发布。")
    print("   云上那个地址是公网：没有口令 = 谁拿到网址谁能看、能改你的衣柜。")
    print("   本机看不出来（本机是免密的），所以这里必须拦。")
    print("   先生成口令：python scripts/set_gate_password.py")
    print("   确实要发一个不设门的站点，再加 --allow-no-gate。")
    return False


def stage(no_data: bool = False, allow_no_gate: bool = False) -> int:
    if MANIFEST.exists():
        print("⚠️ 已经 stage 过一次了，先 restore 再 stage。")
        return 1
    if not allow_no_gate and not _gate_ok():
        return 1
    STAGE.mkdir(parents=True, exist_ok=True)
    done: list[dict] = []

    if no_data:
        # ⚠️ 先挪整个 data/。这是"手机上改的留得住"的唯一实现方式。
        if (ROOT / "data").exists():
            _move("data", done)
            print("  （不带 data 发布：云上保留它自己那份，手机上改的不被顶掉）")
        else:
            print("· 没有 data/ 可挪（云上会用自己那份；首次发布它会自己建库）")

    for rel in MOVES:
        _move(rel, done)

    for pat in GLOBS:
        if no_data and pat in DATA_GLOBS:
            continue          # data 整个已经挪走了
        hits = sorted(ROOT.glob(pat))
        for src in hits:
            _move(src.relative_to(ROOT).as_posix(), done)
        if not hits:
            print("· 跳过（没匹配到）", pat)

    for rel in SANITIZE:
        if no_data:
            continue          # data 整个已经挪走了，密钥自然也不在
        src = ROOT / rel
        if not src.exists():
            print("· 跳过（不存在）", rel)
            continue
        shutil.copy2(str(src), str(_stage_path(rel)))  # 原件留在 stage
        done.append({"rel": rel, "mode": "restore-copy"})
        _strip_key(rel)
        print("· 摘掉密钥后留在原地", rel)

    MANIFEST.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n共处理 {len(done)} 项，清单在 {MANIFEST}")
    return 0


def restore() -> int:
    if not MANIFEST.exists():
        print("⚠️ 没有清单，可能没 stage 过。什么都不动。")
        return 1
    done = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for item in done:
        rel, mode = item["rel"], item["mode"]
        staged = STAGE / rel
        dst = ROOT / rel
        if not staged.exists():
            print("⚠️ stage 里找不到，跳过：", rel)
            continue
        if mode == "move":
            if dst.exists():
                print("⚠️ 目的地已存在，先让开：", rel)
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(staged), str(dst))
            print("· 搬回", rel)
        else:  # restore-copy
            shutil.copy2(str(staged), str(dst))
            print("· 覆盖回原件", rel)
    MANIFEST.unlink()
    print("\n已全部还原。")
    return 0


def verify() -> int:
    print("=== 项目顶层 ===")
    for p in sorted(ROOT.iterdir(), key=lambda x: x.name):
        print(f"  {'DIR ' if p.is_dir() else '    '}{p.name}")
    d = ROOT / "data"
    print("=== data/ ===")
    if d.exists():
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            print(f"  {'DIR ' if p.is_dir() else '    '}{p.name}")
    else:
        print("  （不在 —— 已被 --no-data 挪走）")
    print("=== vision.json 现状（密钥必然是空的） ===")
    vp = d / "vision.json"
    if vp.exists():
        cfg = json.loads(vp.read_text(encoding="utf-8"))
        print("  api_key:", repr(cfg.get("api_key")), "| enabled:", cfg.get("enabled"),
              "| model:", cfg.get("model"))
    else:
        print("  （不在）")
    print("=== stage 里躺着什么（顶层） ===")
    if STAGE.exists():
        for p in sorted(STAGE.iterdir()):
            print("  ", p.name)
    else:
        print("  （还没有 stage 目录）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="胶囊衣橱 发布前后搬运")
    ap.add_argument("cmd", choices=["stage", "restore", "verify"])
    ap.add_argument("--with-data", action="store_true",
                    help="把本机 data/ 也传上去。⚠️ 会顶掉云上那份，只在用它恢复云上时才用")
    ap.add_argument("--allow-no-gate", action="store_true",
                    help="确实要发一个不设口令的站点（默认会被拦住）")
    args = ap.parse_args()
    if args.cmd == "stage":
        # 默认不带 data —— "手机上改的留得住"就靠这一行
        return stage(no_data=not args.with_data, allow_no_gate=args.allow_no_gate)
    if args.cmd == "restore":
        return restore()
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
