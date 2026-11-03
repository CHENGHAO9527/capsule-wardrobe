-- 胶囊衣橱 · 数据表结构 v1
-- SQLite 3
-- 设计原则：结构化字段承载"能不能搭"，自由文本只承载"人格信息"

PRAGMA foreign_keys = ON;

-- ═══════════════════════════════════════════
-- 一、单品主表
-- ═══════════════════════════════════════════
CREATE TABLE items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,

  -- ① 品类槽位 —— 决定它在搭配路径里站哪一层
  slot          TEXT NOT NULL CHECK (slot IN ('L1','L2','L3','L4','L5','ACC')),
  -- L1 内搭 / L2 中间层 / L3 外层 / L4 下装 / L5 鞋 / ACC 配饰
  category      TEXT NOT NULL,        -- T恤/衬衫/针织衫/卫衣/西装/大衣/羽绒/牛仔/西裤/短裙/运动鞋/靴/包

  -- ② 主色
  color_main    TEXT,                 -- 主色中文名（藏蓝/米白/焦糖）
  color_hex     TEXT,                 -- 校正后主色 #RRGGBB（必须在 L2 之后提取）
  is_neutral    INTEGER DEFAULT 0,    -- 1 = 中性色
  color_second  TEXT,                 -- 副色，可空
  pattern       TEXT,                 -- 纯色/条纹/格纹/印花/标识

  -- ③ 正式度
  formality     INTEGER CHECK (formality BETWEEN 1 AND 5),
  -- 1 居家运动 / 2 休闲 / 3 日常通勤 / 4 商务 / 5 正式

  -- ④ 温度区间
  temp_min      INTEGER,              -- 适用最低温（摄氏）
  temp_max      INTEGER,              -- 适用最高温

  -- ⑤ 厚度等级
  thickness     INTEGER CHECK (thickness BETWEEN 1 AND 5),
  -- 1 极薄(真丝/雪纺) / 2 薄(棉T) / 3 中(衬衫/薄针织) / 4 厚(毛衣/卫衣) / 5 极厚(大衣/羽绒)

  -- ⑥ 领型
  collar        TEXT,                 -- 圆领/高领/翻领/开领/连帽/V领/无领

  -- 叠穿规则需要的补充字段
  length_band   TEXT,                 -- 短/常规/长/及踝 —— 判"外套下摆 ≥ 内层下摆"
  sleeve        TEXT,                 -- 无袖/短袖/长袖/可卷
  fit           TEXT,                 -- 修身/合身/宽松/廓形 —— 判"宽松叠宽松"
  material      TEXT,                 -- 棉/羊毛/化纤/混纺/皮革/牛仔

  -- ── 归属层：区分"已拥有"与"候选"（买前模拟的隔离基础）──
  stage         TEXT NOT NULL DEFAULT 'owned'
                CHECK (stage IN ('owned','candidate','archived')),
  -- owned      已拥有 → 参与全部统计与推荐
  -- candidate  候选件，还没买 → 不参与任何统计与推荐，只出现在买前模拟里
  -- archived   已淘汰 / 卖掉 → 不参与统计，记录与图片保留

  -- 状态层：决定"今天能穿哪几件"（仅 stage='owned' 时有意义）
  status        TEXT NOT NULL DEFAULT 'in_wardrobe'
                CHECK (status IN ('in_wardrobe','laundry','dry_clean','stored','repair','lent_out','discarded')),
  location      TEXT,                 -- 存放位置

  -- 归档图（四种用途，见 00-图片标准化方案.md）
  img_raw       TEXT,
  img_display   TEXT,
  img_llm       TEXT,
  img_mask      TEXT,
  img_worn      TEXT,                 -- 穿着照，可选，不进标准图管线

  -- 自由文本：只放"人格信息"，不参与硬规则
  notes         TEXT,                 -- 这双鞋走久了脚疼 / 这件领口有点松

  -- ── 候选期专用（stage='candidate' 时填充）──
  intent        TEXT,                 -- 想买它的理由："缺一条通勤裤"
  source_url    TEXT,                 -- 商品链接
  eval_batch_id INTEGER,              -- 评估批次：同一次想买的几件绑成一组横向对比
  decision      TEXT CHECK (decision IN ('bought','rejected','expired')),
  decided_at    TEXT,
  field_conf    TEXT,                 -- JSON 置信度 {color:0.6, thickness:0.3}
                                      -- 候选件通常只有商品图，颜色/厚度是估的，必须标置信度

  -- 经济性
  purchase_price REAL,                -- 候选期填"拟购入价"
  purchase_date  TEXT,
  wear_count     INTEGER DEFAULT 0,   -- 冗余字段，由 wear_log 重算

  is_signature  INTEGER DEFAULT 0,    -- 1 = 本命单品（冷启动用）
  created_at    TEXT DEFAULT (datetime('now','localtime')),
  updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_items_slot   ON items(stage, slot, status);
CREATE INDEX idx_items_temp   ON items(temp_min, temp_max);
CREATE INDEX idx_items_sig    ON items(is_signature);
CREATE INDEX idx_items_batch  ON items(eval_batch_id);

-- ═══════════════════════════════════════════
-- 二、权重表（滑动学习的结果落在这里）
-- ═══════════════════════════════════════════
-- 统一用 Beta(alpha, beta) 表示偏好。
-- 后验均值 = alpha / (alpha + beta)，天然带样本量衰减，不需要手动设阈值。
-- 每次滑动更新 alpha/beta，不重算历史。

-- 2.1 单品权重
CREATE TABLE w_item (
  item_id   INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  alpha     REAL DEFAULT 1.0,   -- 正证据（被接受）
  beta      REAL DEFAULT 1.0,   -- 负证据（被拒）
  last_seen TEXT
);

-- 2.2 属性权重（颜色/正式度/厚度/版型/领型/品类）
CREATE TABLE w_attr (
  attr_type  TEXT NOT NULL,     -- color / formality / thickness / fit / collar / category / pattern
  attr_value TEXT NOT NULL,
  alpha      REAL DEFAULT 1.0,
  beta       REAL DEFAULT 1.0,
  PRIMARY KEY (attr_type, attr_value)
);

-- 2.3 结构权重（组合层面的模式）
CREATE TABLE w_struct (
  pattern_key TEXT PRIMARY KEY,
  -- outer_shorter_than_inner 外套短于内搭
  -- loose_over_loose          宽松叠宽松
  -- layer_count_3             三层以上
  -- color_family_mismatch     跨暖冷色系
  -- formality_spread_2        正式度跨两档
  -- tight_top_loose_bottom    上紧下松
  alpha       REAL DEFAULT 1.0,
  beta        REAL DEFAULT 1.0
);

-- 2.4 成对关系 —— 这张表是滑动学习里最值钱的
-- 单独都接受、联合总被拒 → 不是单品问题，是这两件冲突
CREATE TABLE w_pair (
  item_a_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  item_b_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  alpha     REAL DEFAULT 1.0,
  beta      REAL DEFAULT 1.0,
  PRIMARY KEY (item_a_id, item_b_id),
  CHECK (item_a_id < item_b_id)
);

-- ═══════════════════════════════════════════
-- 三、搭配与穿着记录
-- ═══════════════════════════════════════════
CREATE TABLE outfits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT,                       -- YYYY-MM-DD
  occasion   TEXT,                       -- 通勤/约会/休闲/运动/正式
  temp_actual INTEGER,                   -- 当天实际温度
  source     TEXT CHECK (source IN ('local','llm')),  -- 谁选出来的
  local_score REAL,                      -- 本地打分
  llm_reason TEXT,                       -- 模型给的理由（反哺权重用）
  is_worn    INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE outfit_items (
  outfit_id INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  item_id   INTEGER NOT NULL REFERENCES items(id),
  slot      TEXT NOT NULL,
  PRIMARY KEY (outfit_id, item_id)
);

CREATE TABLE feedback (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  outfit_id  INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
  gesture    TEXT NOT NULL
             CHECK (gesture IN ('accepted','skipped','rejected','auto_worn')),
  -- accepted  下滑：今天就穿这套       → 最强正信号
  -- skipped   上滑：跳过，没兴趣       → 弱负信号
  -- rejected  长按：别再推这套         → 强负信号
  -- auto_worn 自动记录实际穿了         → 最可靠信号
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE wear_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  date      TEXT NOT NULL,
  outfit_id INTEGER REFERENCES outfits(id) ON DELETE SET NULL
);

CREATE INDEX idx_wear_item ON wear_log(item_id, date);

-- ═══════════════════════════════════════════
-- 三·五、买前模拟结果（一次评估一行，支持回溯与横向对比）
-- ═══════════════════════════════════════════
CREATE TABLE prebuy_evals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  batch_id      INTEGER,           -- = items.eval_batch_id，同批横向对比用
  base_n        INTEGER,           -- 模拟前衣橱的有效搭配数
  with_n        INTEGER,           -- 加入候选后的有效搭配数
  delta_n       INTEGER,           -- 新增组合数 ΔN
  revived_count INTEGER DEFAULT 0, -- 救活数：原本 ΔN=0、被候选件激活的孤岛件数
  revival_ids   TEXT,              -- JSON：被救活的单品 id
  partner_ids   TEXT,              -- JSON：搭档及各自贡献 [{id,n}]，画关系图用
  seasons       TEXT,              -- JSON：覆盖的温度带
  replace_rate  INTEGER DEFAULT 0, -- 替代率：能顶掉的同槽位现有件数
  replace_ids   TEXT,              -- JSON：推荐淘汰的 id
  hole_flag     INTEGER DEFAULT 0, -- 1 = 该槽位为空，ΔN 被空洞效应污染，须走横向对比
  verdict       TEXT,              -- buy / compare / skip
  verdict_text  TEXT,              -- 一句人话结论
  created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_prebuy_cand  ON prebuy_evals(candidate_id);
CREATE INDEX idx_prebuy_batch ON prebuy_evals(batch_id);

-- ═══════════════════════════════════════════
-- 四、风格画像与设置
-- ═══════════════════════════════════════════
CREATE TABLE style_profile (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  style_tags      TEXT,   -- JSON 数组：["极简","商务休闲","日系"]
  avoid_tags      TEXT,   -- JSON 数组：明确要避开的风格
  body_params     TEXT,   -- JSON：{height, shoulder_hip_ratio, leg_ratio, conceal:[], show:[]}
  fit_prefs       TEXT,   -- JSON：{top:"合身", bottom:"直筒", avoid:["紧身","落肩"]}
  home_city       TEXT,   -- 天气默认城市
  updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
-- 常用键：llm_provider / llm_model / send_images(0|1) / explore_ratio / temp_offset

-- ═══════════════════════════════════════════
-- 五、派生指标（不存表，实时计算）
-- ═══════════════════════════════════════════
-- N          有效搭配数：通过全部硬规则的合法路径总数
-- 衣橱效率    N / 在库单品数          目标 8–10
-- ΔN(i)       N - N(移除 i 后的 N)    孤岛件 ΔN = 0，枢纽件 ΔN 最高
-- CPW         purchase_price / wear_count
-- 替换率      候选件能顶掉的同槽位现有单品数
-- 救活数      候选件加入后，原本 ΔN=0 的孤岛件中被激活的件数
--
-- 有效搭配数按当天温度 + 场合动态计算，缓存键 = (temp, occasion, 状态过滤后集合哈希)
--
-- ⚠ 统计口径铁律
-- 所有计算（N / 衣橱效率 / CPW / 孤岛枢纽 / 每日推荐）一律只跑：
--     stage = 'owned' AND status = 'in_wardrobe'
-- 候选件（stage='candidate'）永远在衣橱外面。
-- 否则录了 5 件候选还没买，App 会告诉你衣橱效率涨了 —— 口径就脏了。
