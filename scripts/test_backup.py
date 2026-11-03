"""数据备份通道自检 —— `/api/backup` 是这个项目**唯一**能把数据拿回来的口子。

它坏起来的样子很特别：**不报错，只是悄悄少给你几个文件**（或悄悄多给 24MB 旧图）。
所以必须有断言盯着它，而且是"点名测量"，不是"看着差不多"。

    python scripts/test_backup.py
    python scripts/test_backup.py --out scripts/test_backup.out.txt

**自己起一个临时服务**（端口现找、口令走 `CLOSET_PASSWORD`），
不碰你正在跑的那个、也不碰你的 `gate.json`。
跑完比对 `data/` 有没有被动过，并保证自己埋的陷阱已经拆掉。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

PY = sys.executable
PW = "test-backup-pw-4b71"
_FAILS: list[str] = []


class _Abort(Exception):
    """致命错（服务没起来 / 下载失败）→ 跳到收尾，别把后面的断言跑成连锁噪声。"""


def _fail(msg: str) -> None:
    _FAILS.append(msg)
    print(f"  FAIL {msg}")


def _ok(msg: str) -> None:
    print(f"  ok   {msg}")


# ══════════════════════════════════════════════
# 期待值：包里**应该有**哪些条目（与实现独立写一遍）
# ══════════════════════════════════════════════

def _packable(rel: Path) -> bool:
    """按**路径任一段**判断，不是只看文件名。

    ⚠️ 这里的坑是真实发生过的：`data/images.bak-20260923-155338/` 里的文件，
    它的 `.bak-` 在**父目录名**上。只查 `p.name` → 整棵旧备份树漏进包，多打 479 张旧图、
    24MB，而且**不报错、只是包变大**，不看内容根本发现不了。

    `closet.db.*` 一律不要：包里那份是**自洽的快照**，伴生的 `-wal/-shm/.bak-`
    跟着走没有用处，还原时还可能把快照带歪。
    """
    if any(".bak-" in part or part == "__pycache__" for part in rel.parts):
        return False
    if rel.name.startswith("closet.db."):
        return False
    return True


def _expected_names() -> set[str]:
    out = {"data/closet.db"}                      # 库里放的是**一致快照**，不是现役那个文件
    for p in sorted(db.DATA_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(db.DATA_DIR)
        if not _packable(rel):
            continue
        if p.resolve() == db.DB_PATH.resolve():
            continue                              # 用快照替代
        out.add("data/" + rel.as_posix())
    return out


def _tree(data_dir: Path) -> dict[str, int]:
    """目录指纹：路径 → 大小。用来验"这次请求有没有动过 data/"。"""
    out: dict[str, int] = {}
    for p in sorted(data_dir.rglob("*")):
        if p.is_file():
            out[p.relative_to(data_dir).as_posix()] = p.stat().st_size
    return out


# ══════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(opener, url, method="GET", body=None, cookie=None, ctype=None, timeout=90):
    """→ (状态码, 头(小写键), 正文 bytes)。

    头一律归一化成小写：uvicorn 回的是 `set-cookie`，用大写取会拿到 None，
    于是测试报"应用没发 cookie"，其实是测试自己没看见。
    """
    req = urllib.request.Request(url, data=body, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    if ctype:
        req.add_header("Content-Type", ctype)

    def norm(hs):
        return {k.lower(): v for k, v in hs.items()}

    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, norm(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, norm(e.headers), e.read()


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def _body(base: str, opener, trap: Path) -> None:
    # ① 未登录 → 401。**这个口子必须过门**：它就是整份数据。
    code, _, body = _call(opener, f"{base}/api/backup")
    if code == 401:
        _ok("未登录 GET /api/backup → 401（数据口过门）")
    else:
        _fail(f"未登录竟能拿备份：{code} / {body[:80]!r}")

    # ② 对口令拿 cookie
    code, hdrs, _ = _call(opener, f"{base}/login", "POST",
                          body=f"password={PW}".encode(),
                          ctype="application/x-www-form-urlencoded")
    cookie = (hdrs.get("set-cookie") or "").split(";")[0]
    if code == 303 and cookie:
        _ok("登录拿到 cookie")
    else:
        _fail(f"登录失败：{code}")
        raise _Abort()

    # ── 指纹要在**请求前**取（陷阱已经在了）──
    before = _tree(db.DATA_DIR)

    # ③ 真下备份
    t0 = time.time()
    code, hdrs, blob = _call(opener, f"{base}/api/backup", cookie=cookie)
    dt = time.time() - t0
    ctype = hdrs.get("content-type") or ""
    disp = hdrs.get("content-disposition") or ""
    if code == 200 and "application/zip" in ctype:
        _ok(f"下载成功：200 | {ctype} | {dt:.1f}s | {len(blob) / 1048576:.1f} MB")
    else:
        _fail(f"下载失败：{code} / {ctype} / {blob[:80]!r}")
        raise _Abort()

    if "attachment" in disp and ".zip" in disp:
        _ok("带了下载名：" + disp.split("filename=")[-1].strip().strip('"'))
    else:
        _fail(f"Content-Disposition 不对：{disp!r}")

    # ④ 包本身能开
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        _fail(f"下回来的不是能打开的 zip：{e}")
        raise _Abort()
    names = set(z.namelist())
    bad = z.testzip()
    if bad is None:
        _ok(f"包能开且无损坏条目（共 {len(names)} 个）")
    else:
        _fail(f"包里有坏条目：{bad}")

    # ⑤ 【回归】任何条目都不许带 .bak- —— 陷阱就是干这个的
    leaked = sorted(n for n in names if ".bak-" in n)
    if leaked:
        _fail(f"旧备份漏进包了 {len(leaked)} 个，例如 {leaked[0]}")
    else:
        _ok("包内没有任何 .bak- 条目（埋的陷阱被正确排除）")

    # ⑥ 【正面】现役图片一张都不能少
    real_imgs = {p.relative_to(db.DATA_DIR).as_posix()
                 for p in db.IMG_DIR.rglob("*") if p.is_file()}
    zip_imgs = {n[len("data/"):] for n in names if n.startswith("data/images/")}
    missing = sorted(real_imgs - zip_imgs)
    if missing:
        _fail(f"现役图片少了 {len(missing)} 张，例如 {missing[0]}")
    elif len(zip_imgs) == len(real_imgs):
        _ok(f"现役图片 {len(real_imgs)} 张，包里一张不少")
    else:
        _fail(f"包里图片数（{len(zip_imgs)}）与现役（{len(real_imgs)}）对不上")

    # ⑦ 【精确相等】条目集合 == 期望集合。多一个少一个都算错。
    want = _expected_names()
    extra = sorted(names - want)
    lack = sorted(want - names)
    if not extra and not lack:
        _ok(f"条目集合精确匹配（多一个少一个都没有，共 {len(want)} 项）")
    else:
        if extra:
            _fail(f"多打了 {len(extra)} 个，例如 {extra[:3]}")
        if lack:
            _fail(f"少打了 {len(lack)} 个，例如 {lack[:3]}")

    # ⑧ 【点名测量】包里的库要**能开、数据是全的** —— 不能只看"文件在不在"
    if "data/closet.db" not in names:
        _fail("包里没有 data/closet.db")
    else:
        with z.open("data/closet.db") as f:
            raw = f.read()
        tmp_db = db.DATA_DIR / f"_backup_test_{os.getpid()}.db"
        try:
            tmp_db.write_bytes(raw)
            # 这是**包内那份快照的副本**，只用来读几个计数、不写任何东西。
            # 也不能走 db.get_conn()（它指向现役库），所以标注放过（见 test_delete ⑤）。
            # ⚠️ 标注必须跟在**这一行**上：守卫是按行找 `fk-agnostic` 的，写在上一行不算。
            c = sqlite3.connect(tmp_db)   # fk-agnostic
            try:
                c.execute("PRAGMA foreign_keys = ON")
                integ = c.execute("PRAGMA integrity_check").fetchone()[0]
                items = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                owned = c.execute(
                    "SELECT COUNT(*) FROM items WHERE stage='owned' AND status='in_wardrobe'"
                ).fetchone()[0]
                outf = c.execute("SELECT COUNT(*) FROM outfits").fetchone()[0]
            finally:
                c.close()

            # 现役库走**正门**，不绕过 get_conn()。
            live = db.get_conn()
            try:
                live_items = live.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                live.close()

            if integ == "ok":
                _ok(f"包里的库完整性 ok（items={items} | 在柜={owned} | 搭配={outf}）")
            else:
                _fail(f"包里的库完整性检查没过：{integ}")
            if items == live_items:
                _ok(f"包里 items 数与现役一致（{items}）")
            else:
                _fail(f"包里 items={items}，现役={live_items} —— 快照不是当下的库")
            if items > 0 and owned > 0 and outf > 0:
                _ok("三个计数都非零（是真库，不是空壳）")
            else:
                _fail(f"计数里有 0：items={items} owned={owned} outfits={outf}")
        finally:
            tmp_db.unlink(missing_ok=True)

    # ⑨ 【只读】这次请求不许在 data/ 留下任何痕迹（快照必须自己删掉）
    after = _tree(db.DATA_DIR)
    if before == after:
        _ok("请求前后 data/ 完全一致（一致快照已自删，端点是只读的）")
    else:
        added = sorted(set(after) - set(before))
        gone = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        _fail(f"data/ 被动了：新增{added[:3]} 消失{gone[:3]} 变动{changed[:3]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", metavar="PATH", help="把输出也写一份到这个文件（UTF-8）")
    args = ap.parse_args()

    buf: list[str] = []
    orig = print

    def tee(*a, **kw):
        buf.append(" ".join(str(x) for x in a))
        orig(*a, **kw)

    globals()["print"] = tee
    tee(f"# test_backup · 生成于 {datetime.now():%Y-%m-%d %H:%M:%S}")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, CLOSET_PASSWORD=PW, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.pop("CLOSET_GATE", None)          # 临时服务要**开着门**，才能验"未登录被挡"
    log = open(ROOT / "scripts" / "_backup_test_server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    # ── 自己埋陷阱：一个"父目录名带 .bak-"的假旧备份目录 ──
    # 不做这一步，"旧备份不许进包"就是一条**空断言**（干净环境里本来就没有旧备份），
    # 于是它永远绿 —— 这正是本项目第 ⑧ 条说的"防御性跳过会造假绿"。
    trap = db.DATA_DIR / f"images.bak-test{os.getpid()}"
    try:
        trap.mkdir(parents=True)
        (trap / "trap.txt").write_text("绝不能出现在备份包里\n", encoding="utf-8")
        (trap / "sub").mkdir()
        (trap / "sub" / "deep.txt").write_text("父目录带 .bak- 就要整棵排除\n", encoding="utf-8")
        tee(f"\n[A] 夹具：埋了陷阱目录 data/{trap.name}/（2 个文件，父目录名带 .bak-）")

        opener = urllib.request.build_opener(_NoRedirect)
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                code, _, _ = _call(opener, f"{base}/api/ping", timeout=5)
                if code == 200:
                    ready = True
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.4)
        if not ready:
            _fail("临时服务没起来（看 scripts/_backup_test_server.log）")
        else:
            _ok(f"临时服务起来了（:{port}，口令走环境变量）")
            _body(base, opener, trap)
    except _Abort:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if trap.exists():
            shutil.rmtree(trap, ignore_errors=True)
        for junk in db.DATA_DIR.glob(f"_backup_test_{os.getpid()}.db*"):
            junk.unlink(missing_ok=True)

    # ⑩ 收尾要**点名确认**，不能只说"我清理了"
    if trap.exists():
        _fail(f"陷阱目录没拆掉，残留 {trap}")
    else:
        _ok("自己埋的陷阱已拆掉，data/ 回到原样")

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
