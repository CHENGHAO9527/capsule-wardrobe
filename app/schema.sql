-- 胶囊衣橱 · 数据库结构
-- 与 docs/01-数据表结构.sql 保持一致，此处为运行时版本

CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,

  -- 归属层：区分"已拥有"与"候选"（买前模拟的隔离基础）
  stage         TEXT NOT NULL DEFAULT 'owned'
                CHECK (stage IN ('owned','candidate','archived')),

  -- ① 品类槽位
  -- L4I = 内穿下装（秋裤/打底裤），必须被 L4 盖住 —— 2026-09-21 新增。
  -- ⚠️ SQLite 不能 ALTER 约束，老库靠 `db._rebuild_items()` 重建表来补上这个值；
  -- 判断依据是 `sqlite_master` 里有没有 "L4I" 这几个字符，见 `_items_slot_ok()`。
  slot          TEXT NOT NULL
                CHECK (slot IN ('L1','L2','L3','L4I','L4','L5','ACC')),
  category      TEXT NOT NULL,

  -- ② 主色
  color_main    TEXT,
  color_hex     TEXT,
  is_neutral    INTEGER DEFAULT 0,
  color_second  TEXT,
  pattern       TEXT,

  -- ③ 正式度 1-5
  formality     INTEGER,

  -- ④ 温度区间
  temp_min      INTEGER,
  temp_max      INTEGER,

  -- ④b 保暖值（℃）—— 「26℃穿衣法则」里的那个数：气温 + Σ保暖值 = 体感舒适线。
  -- 只有上装（L1/L2/L3）参与加总，下半身恒为 0。默认值按品类给（见 catalog）。
  warmth        INTEGER,

  -- ⑤ 厚度等级 1-5
  thickness     INTEGER,

  -- ⑥ 领型
  collar        TEXT,

  -- 叠穿规则补充
  length_band   TEXT,
  sleeve        TEXT,
  fit           TEXT,
  material      TEXT,

  -- 穿搭标签（JSON 数组）。**只放引擎真的会读的标签**，见 rules.TAGS：
  -- "solo_only" 只能单穿 / "never_outer" 只作内搭
  tags          TEXT,

  -- 状态层（仅 stage='owned' 有意义）
  status        TEXT NOT NULL DEFAULT 'in_wardrobe'
                CHECK (status IN ('in_wardrobe','laundry','dry_clean','stored','repair','lent_out','discarded')),
  location      TEXT,

  -- 图片
  img_raw       TEXT,
  img_display   TEXT,
  img_llm       TEXT,
  img_mask      TEXT,
  img_worn      TEXT,

  notes         TEXT,

  -- 候选期专用
  intent        TEXT,
  source_url    TEXT,
  eval_batch_id INTEGER,
  decision      TEXT CHECK (decision IN ('bought','rejected','expired')),
  decided_at    TEXT,
  field_conf    TEXT,

  -- 经济性
  purchase_price REAL,
  purchase_date  TEXT,
  wear_count     INTEGER DEFAULT 0,

  is_signature  INTEGER DEFAULT 0,
  created_at    TEXT DEFAULT (datetime('now','localtime')),
  updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_items_stage_slot ON items(stage, slot, status);
CREATE INDEX IF NOT EXISTS idx_items_batch      ON items(eval_batch_id);
CREATE INDEX IF NOT EXISTS idx_items_sig        ON items(is_signature);

-- ═══════ 权重表（Beta 分布，滑动学习的结果落在这里）═══════

CREATE TABLE IF NOT EXISTS w_item (
  item_id   INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  alpha     REAL DEFAULT 1.0,
  beta      REAL DEFAULT 1.0,
  -- 「不喜欢」这件衣服的次数。**按件计**，不是按套 —— 不喜欢一套组合
  -- 说明不了其中任何一件有问题，只有"这件本身"被反复否掉才是淘汰线索。
  dislike_n INTEGER DEFAULT 0,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS w_attr (
  attr_type  TEXT NOT NULL,
  attr_value TEXT NOT NULL,
  alpha      REAL DEFAULT 1.0,
  beta       REAL DEFAULT 1.0,
  PRIMARY KEY (attr_type, attr_value)
);

CREATE TABLE IF NOT EXISTS w_struct (
  pattern_key TEXT PRIMARY KEY,
  alpha       REAL DEFAULT 1.0,
  beta        REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS w_pair (
  item_a_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  item_b_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  alpha     REAL DEFAULT 1.0,
  beta      REAL DEFAULT 1.0,
  PRIMARY KEY (item_a_id, item_b_id),
  CHECK (item_a_id < item_b_id)
);

-- ═══════ 搭配与穿着记录 ═══════

CREATE TABLE IF NOT EXISTS outfits (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  date        TEXT,
  occasion    TEXT,
  temp_actual INTEGER,
  source      TEXT CHECK (source IN ('local','llm')),
  local_score REAL,
  llm_reason  TEXT,
  is_worn     INTEGER DEFAULT 0,
  created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS outfit_items (
  outfit_id INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  slot      TEXT NOT NULL,
  PRIMARY KEY (outfit_id, item_id)
);

CREATE TABLE IF NOT EXISTS feedback (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  outfit_id  INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  gesture    TEXT NOT NULL
             CHECK (gesture IN ('accepted','skipped','rejected','auto_worn')),
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS wear_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  date      TEXT NOT NULL,
  outfit_id INTEGER REFERENCES outfits(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_wear_item ON wear_log(item_id, date);

-- ═══════ 自己搭的套装 ═══════
-- 和 outfits 的区别：outfits 是程序算给你的一次性结果（反馈挂在上面），
-- sets 是你自己挑的、长期留着，今日页会优先端出来。
-- **不存温度区间** —— 那会和引擎漂移（改一件单品的温度，这里的区间就错了）。
-- 能不能穿一律现算，见 app/sets.py。
CREATE TABLE IF NOT EXISTS sets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  occasion   TEXT,
  note       TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  used_at    TEXT,
  use_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS set_items (
  set_id  INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  slot    TEXT NOT NULL,
  PRIMARY KEY (set_id, item_id)
);

-- ═══════ 买前模拟结果 ═══════

CREATE TABLE IF NOT EXISTS prebuy_evals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  batch_id      INTEGER,
  base_n        INTEGER,
  with_n        INTEGER,
  delta_n       INTEGER,
  revived_count INTEGER DEFAULT 0,
  revival_ids   TEXT,
  partner_ids   TEXT,
  conflict_ids  TEXT,
  seasons       TEXT,
  replace_rate  INTEGER DEFAULT 0,
  replace_ids   TEXT,
  hole_flag     INTEGER DEFAULT 0,
  verdict       TEXT,
  verdict_text  TEXT,
  created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_prebuy_cand  ON prebuy_evals(candidate_id);
CREATE INDEX IF NOT EXISTS idx_prebuy_batch ON prebuy_evals(batch_id);

-- ═══════ 风格画像与设置 ═══════

CREATE TABLE IF NOT EXISTS style_profile (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  style_tags  TEXT,
  avoid_tags  TEXT,
  body_params TEXT,
  fit_prefs   TEXT,
  home_city   TEXT,
  updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
