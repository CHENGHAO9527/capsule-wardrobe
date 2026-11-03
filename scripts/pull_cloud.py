"""把云上那份 `data/` 拉回来存一份 —— 防止"数据只存在别人的沙箱里"。

用法：
  python scripts/pull_cloud.py                 # 只下载，存到 backups/
  python scripts/pull_cloud.py --unpack        # 下载并**覆盖本机 data/**（先把现役那份改名留档）
  python scripts/pull_cloud.py --url ... --pw ...

⚠️ 为什么要拉：云上的沙箱不归自己管。哪天应用被下线、沙箱被回收，
   **手机上改动的东西就一起没了**。所以"改在云上"必须配"本地留一份"。

⚠️ 默认**不解压**。解压是破坏性动作（会盖掉本机 data/），所以要么显式加 `--unpack`，
   要么自己解压到别处看。加了 `--unpack` 也会先把现役 data/ 改名留档，不直接毁掉。
"""
import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "backups"
# 换部署就设环境变量 `CLOSET_URL`，**别改代码**：这份源码是要分享出去的，
# 里面不该钉死谁的域名。
DEFAULT_URL = os.environ.get("CLOSET_URL") or "https://your-app.example.com"


def read_password() -> str:
    f = ROOT / "gate.json"
    if not f.exists():
        print("⚠️ 没有 gate.json —— 云上那份是免密的吗？那就不用口令。")
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        print("⚠️ gate.json 读不出来")
        return ""
    return str(data.get("password") or "").strip() if isinstance(data, dict) else ""


def login_and_download(base: str, pw: str) -> bytes:
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    if pw:
        body = urllib.parse.urlencode({"password": pw, "next": "/"}).encode()
        req = urllib.request.Request(base + "/login", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with op.open(req, timeout=30) as r:
            r.read()

    with op.open(urllib.request.Request(base + "/api/backup"), timeout=300) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        blob = r.read()
    if "zip" not in ctype:
        raise SystemExit(f"✗ 拿回来的不是 zip（Content-Type={ctype!r}）—— 先看看是不是被门拦了")
    return blob


def inspect(zpath: pathlib.Path) -> None:
    """⚠️ 关键元素点名测量 —— 不打开看一眼，等于没验过。"""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        db_member = next((n for n in names if n.endswith("closet.db")), None)
        imgs = [n for n in names if n.startswith("data/images/") and not n.endswith("/")]
        jsons = sorted(n for n in names if n.startswith("data/") and n.endswith(".json"))
        print(f"  文件数      {len(names)}（其中图片 {len(imgs)} 张）")
        print(f"  附带 json   {', '.join(pathlib.Path(n).name for n in jsons) or '（无）'}")
        if not db_member:
            print("  ✗ 包里没有 closet.db —— 这个备份不完整，别依赖它")
            return
        tmp = BACKUPS / "_peek.db"
        try:
            with z.open(db_member) as src, tmp.open("wb") as dst:
                dst.write(src.read())
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            n_items = conn.execute("select count(*) from items").fetchone()[0]
            n_wardrobe = conn.execute(
                "select count(*) from items where stage='owned' and status='in_wardrobe'"
            ).fetchone()[0]
            n_outfits = conn.execute("select count(*) from outfits").fetchone()[0]
            conn.close()
            print(f"  库里的数据 单品 {n_items} 件（在柜 {n_wardrobe}）｜搭配记录 {n_outfits} 条")
            if n_items == 0:
                print("  ⚠️ 库是空的 —— 云上那份可能是新沙箱建的空库，先别拿它覆盖本机")
        finally:
            tmp.unlink(missing_ok=True)


def unpack(zpath: pathlib.Path) -> int:
    """解压并覆盖本机 data/。现役那份先改名留档，不直接毁掉。"""
    target = ROOT / "data"
    if target.exists():
        keep = ROOT / f"data-before-pull-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(target), str(keep))
        print(f"  现役 data/ 已改名为 {keep.name}（不是删，随时能翻回来）")

    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            rel = pathlib.PurePosixPath(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise SystemExit(f"✗ 包里有可疑路径，拒绝解压：{info.filename}")
        z.extractall(ROOT)
    print(f"  已解压到 {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从云上把 data/ 拉回来")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--pw", default="", help="口令。默认从 gate.json 读")
    ap.add_argument("--unpack", action="store_true",
                    help="下载并解压覆盖本机 data/（先把现役那份改名留档）")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    pw = args.pw or read_password()
    BACKUPS.mkdir(parents=True, exist_ok=True)
    out = BACKUPS / f"closet-data-{time.strftime('%Y%m%d-%H%M%S')}.zip"

    print(f"  来源        {base}")
    try:
        blob = login_and_download(base, pw)
    except urllib.error.HTTPError as e:
        print(f"✗ 下载失败：HTTP {e.code}（401 = 口令不对 / 没登进去）")
        return 1
    except SystemExit as e:
        print(e)
        return 1

    out.write_bytes(blob)
    print(f"  已存        {out}（{len(blob) / 1048576:.1f} MB）")
    inspect(out)

    if args.unpack:
        unpack(out)
    else:
        print("\n  （只下载，没动本机 data/。要拿它覆盖本机就加 --unpack）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
