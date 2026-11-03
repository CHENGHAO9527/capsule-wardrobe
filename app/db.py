"""SQLite 连接与初始化。数据库文件放在 data/closet.db。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
IMG_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "closet.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 增量列。`CREATE TABLE IF NOT EXISTS` **不会**给已经存在的表补列，
# 老库升级上来必须显式 ALTER，否则新字段永远读不到 ——
# 表现就是"改了属性但搭配没变"，而且不报任何错（这个项目栽过三次同类跟头）。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("items", "tags", "TEXT"),
    # 保暖值（℃）。2026-09-23 随「26℃穿衣法则」加的 —— 忘了这一行，
    # 老库就永远读不到这个字段，表现为"改了保暖值但搭配没变"，而且不报错。
    ("items", "warmth", "INTEGER"),
    # 「不喜欢」按件计数（2026-10-17）。不加这一行，老库永远读到 NULL ——
    # 表现是"点了不喜欢、体检页待淘汰清单永远是空的"，而且不报错。
    ("w_item", "dislike_n", "INTEGER DEFAULT 0"),
]


# ── 外键约束的一次性重建 ──────────────────────────────────────────────
# SQLite **不能 ALTER 约束**，写错的 ON DELETE 只能靠"建新表 → 搬数据 →
# 换名"来修。老库的 outfit_items.item_id 少了 ON DELETE CASCADE，
# 配合 get_conn() 里的 PRAGMA foreign_keys=ON，删任何一件进过搭配的衣服
# 都会 IntegrityError，前端表现是"点了删除什么也没发生"。
_OUTFIT_ITEMS_REBUILD = """
CREATE TABLE outfit_items_new (
  outfit_id INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  slot      TEXT NOT NULL,
  PRIMARY KEY (outfit_id, item_id)
);
INSERT INTO outfit_items_new (outfit_id, item_id, slot)
    SELECT outfit_id, item_id, slot FROM outfit_items;
DROP TABLE outfit_items;
ALTER TABLE outfit_items_new RENAME TO outfit_items;
"""


# ── items 的 CHECK 约束重建（加 L4I 槽位）──────────────────────────────
# 2026-09-21 新增「内穿下装」槽位（秋裤）。`slot` 上有 CHECK 约束，
# **SQLite 改不了约束**，只能建新表 → 搬数据 → 换名。
#
# 为什么把列清单交给 `PRAGMA table_info` 动态取，而不是在这里手写：
# 手写一份列清单 = 多一个会和 schema.sql 漂移的地方，而漂移的后果是
# **悄悄少搬一列数据**（这个项目已经被"字段静默丢失"坑过）。
# 建表语句本身则从 schema.sql 里抓出来（只改表名），保证两边永远一致。
_ITEMS_MARK = "CREATE TABLE IF NOT EXISTS"


def _items_create_sql(new_name: str) -> str:
    """从 schema.sql 抽出 items 的建表语句，换成新表名。

    ⚠️ 锚点里**不能带 `items`** —— 曾经写成 `"CREATE TABLE IF NOT EXISTS items"`，
    拼接 `+ " items"` 之后变成 `"… items items"`，永远匹配不到，
    于是 `replace` 静默什么都没做，建出来的还是 `items`：
    `INSERT INTO items_new` 当场报 `no such table: items_new`。
    （又是"改动没生效但不抛错"那一类 —— 这次好在报错了。）
    """
    src = SCHEMA_PATH.read_text(encoding="utf-8")
    start = src.find(f"{_ITEMS_MARK} items")
    if start < 0:
        raise RuntimeError("schema.sql 里找不到 items 的建表语句")
    end = src.find("\n);", start)
    if end < 0:
        raise RuntimeError("items 建表语句没有正常结尾")
    body = src[start:end + 3]
    out = body.replace("IF NOT EXISTS items", new_name, 1)
    if new_name not in out or out == body:
        raise RuntimeError("items 建表语句换名失败（锚点没匹配上）")
    return out


def _items_slot_ok(conn: sqlite3.Connection) -> bool:
    """现有 items 表的 slot CHECK 是否已经认 L4I。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()
    if not row:
        return True            # 表都还没有，init 会按新 schema 建
    return "L4I" in (row[0] or "")


def _rebuild_items(conn: sqlite3.Connection) -> None:
    """重建 items 表，让 slot 认 L4I（调用方负责备份）。"""
    conn.commit()                      # PRAGMA foreign_keys 在事务里是空操作
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        if not cols:
            return
        col_list = ", ".join(cols)
        conn.execute(_items_create_sql("items_new"))
        conn.execute(f"INSERT INTO items_new ({col_list}) "
                     f"SELECT {col_list} FROM items")
        before = conn.execute("SELECT COUNT(*) FROM items_new").fetchone()[0]
        was = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if before != was:
            raise RuntimeError(f"搬运前后行数不一致：{was} → {before}")
        conn.execute("DROP TABLE items")
        conn.execute("ALTER TABLE items_new RENAME TO items")
        conn.commit()
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"重建 items 后外键检查没过：{[dict(r) for r in bad[:3]]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _fk_needs_cascade(conn: sqlite3.Connection, table: str, ref: str) -> bool:
    """该表指向 ref 的外键，on_delete 不是 CASCADE 就要重建。"""
    for r in conn.execute(f"PRAGMA foreign_key_list({table})"):
        if r["table"] == ref:
            if (r["on_delete"] or "NO ACTION").upper() != "CASCADE":
                return True
    return False


def backup_db(conn: sqlite3.Connection | None = None) -> Path:
    """备份到 data/closet.db.bak-YYYYmmdd-HHMMSS，返回路径。

    用 sqlite3 的在线备份 API，不要在库开着的时候直接拷文件。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dst_path = DATA_DIR / f"closet.db.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    src = conn or get_conn()
    own = conn is None
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        if own:
            src.close()
    return dst_path


def purge_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """清掉"父行已经不存在"的子行，返回 {表.列: 删了几行}。

    孤儿是怎么来的：**`PRAGMA foreign_keys` 默认是 OFF**。只要有人用裸的
    `sqlite3.connect(path)`（而不是本模块的 get_conn()）去 `DELETE FROM outfits`，
    `ON DELETE CASCADE` 就不会触发 —— 删了个寂寞，`outfit_items` 的子行全留下。
    实测真实库里攒了 979 行这种东西（每次跑 apitest.py 都留一批）。
    这是本项目第 6 次栽在"某个默认值在背后起作用、而且不报错"上。

    `ON DELETE SET NULL` 的列按语义置空而不是删行（wear_log 是穿用历史，不能删）。
    """
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    counts: dict[str, int] = {}
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
            child, parent = fk["from"], fk["table"]
            pcol = fk["to"] or "id"
            if child in (None, "") or parent not in tables:
                continue
            missing = (f"SELECT 1 FROM {parent} WHERE {parent}.{pcol} = {t}.{child}")
            if (fk["on_delete"] or "").upper() == "SET NULL":
                cur = conn.execute(
                    f"UPDATE {t} SET {child} = NULL "
                    f"WHERE {child} IS NOT NULL AND NOT EXISTS ({missing})")
            else:
                cur = conn.execute(
                    f"DELETE FROM {t} WHERE {child} IS NOT NULL "
                    f"AND NOT EXISTS ({missing})")
            if cur.rowcount:
                counts[f"{t}.{child}"] = cur.rowcount
    if counts:
        conn.commit()
    return counts


def _count_orphans(conn: sqlite3.Connection) -> int:
    """数一数所有"父行没了"的子行。"""
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    n = 0
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
            child, parent = fk["from"], fk["table"]
            pcol = fk["to"] or "id"
            if not child or parent not in tables:
                continue
            n += conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {child} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {parent} "
                f"WHERE {parent}.{pcol} = {t}.{child})").fetchone()[0]
    return n


def _rebuild_outfit_items(conn: sqlite3.Connection) -> None:
    """重建 outfit_items 补上 ON DELETE CASCADE（调用方负责先备份 + 关外键）。"""
    conn.commit()                   # PRAGMA foreign_keys 在事务里是空操作
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(_OUTFIT_ITEMS_REBUILD)
        conn.commit()
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(
                f"重建 outfit_items 后外键检查没过：{[dict(r) for r in bad[:3]]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _backfill_warmth(conn: sqlite3.Connection) -> None:
    """给还没有保暖值的单品按品类补上（**只补 NULL，不覆盖用户改过的**）。

    保暖值是 2026-09-23 随「26℃穿衣法则」加的字段，老库 ALTER 出来全是 NULL。
    不补的话 `rules.warmth()` 会一路走厚度兜底 —— 能跑，但界面上是空白，
    而且"法则值"和"兜底值"会不一致（同一件衣服两处算出不同的数）。
    幂等：每次启动都跑，但只碰 NULL 行。
    """
    from app import catalog  # 局部 import：catalog 是纯数据模块，不反向依赖 db

    rows = conn.execute(
        "SELECT DISTINCT category FROM items WHERE warmth IS NULL").fetchall()
    cats = [r["category"] for r in rows if r["category"]]
    if not cats:
        return
    updated = 0
    for cat in cats:
        w = catalog.CATEGORY_DEFAULTS.get(cat, {}).get("warmth")
        if w is None:
            continue
        cur = conn.execute(
            "UPDATE items SET warmth = ? WHERE warmth IS NULL AND category = ?", (w, cat))
        updated += cur.rowcount
    if updated:
        print(f"[db] 按品类补上 {updated} 件单品的保暖值（26℃穿衣法则）")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    _backfill_warmth(conn)

    needs_fk = _fk_needs_cascade(conn, "outfit_items", "items")
    needs_slot = not _items_slot_ok(conn)
    n_orphan = _count_orphans(conn)
    if not needs_fk and not n_orphan and not needs_slot:
        return

    # 两种修复都会真正动数据，共用一次备份（这是真实衣橱，不是测试库）
    bak = backup_db(conn)
    print(f"[db] 库结构需要修复，已备份 → {bak.name}")

    if n_orphan:
        purged = purge_orphans(conn)
        detail = "、".join(f"{k} {v} 行" for k, v in purged.items())
        print(f"[db] 清掉 {sum(purged.values())} 行孤儿记录（{detail}）")

    if needs_fk:
        _rebuild_outfit_items(conn)
        print("[db] outfit_items 外键已补上 ON DELETE CASCADE")

    if needs_slot:
        _rebuild_items(conn)
        print("[db] items 表已重建：slot 现在认 L4I（内穿下装 / 秋裤）")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def load_items(conn: sqlite3.Connection, stage: str = "owned",
               only_wearable: bool = True) -> list[dict]:
    """载入单品。

    统计口径铁律：只跑 stage='owned' AND status='in_wardrobe'。
    候选件永远在衣橱外面。
    """
    sql = "SELECT * FROM items WHERE stage = ?"
    params: list = [stage]
    if only_wearable:
        sql += " AND status = 'in_wardrobe'"
    sql += " ORDER BY slot, id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def delete_item(conn: sqlite3.Connection, item_id: int) -> dict:
    """删一件单品，并把指向它的记录一并清干净。**所有删除路径都走这里。**

    绝对不要写成裸的 `DELETE FROM items WHERE id=?` —— 老库的
    outfit_items.item_id 没有 ON DELETE 子句，配上 PRAGMA foreign_keys=ON
    会直接 IntegrityError；配上前端一句裸 await，就变成"点了删除什么也没发生"。
    顺序也不能省：先摘引用，再删本体。
    """
    row = conn.execute("SELECT id, name FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "not_found"}

    # 1) 从历史搭配里摘掉
    n_ref = conn.execute("DELETE FROM outfit_items WHERE item_id = ?",
                         (item_id,)).rowcount
    # 2) 被摘空的搭配留着没意义（还会在历史里显示成"空套"），连同反馈一起清
    n_outfit = conn.execute(
        "DELETE FROM outfits WHERE id NOT IN (SELECT outfit_id FROM outfit_items)"
    ).rowcount
    # 3) 删本体。wear_log / w_pair / prebuy_evals 都带 ON DELETE CASCADE，会跟着走
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return {"ok": True, "id": item_id, "name": row["name"],
            "unlinked": n_ref, "dropped_outfits": n_outfit}
