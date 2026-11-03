/* 共享工具：API 调用 + 元数据 + 渲染帮手 */

const API = {
  async req(method, path, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(path, opt);
    const txt = await res.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = txt; }
    if (!res.ok) {
      const msg = (data && data.detail) ? data.detail : ('请求失败 ' + res.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  },
  get(p) { return this.req('GET', p); },
  post(p, b) { return this.req('POST', p, b); },
  patch(p, b) { return this.req('PATCH', p, b); },
  del(p) { return this.req('DELETE', p); },
};

let META = null;
async function loadMeta() {
  if (!META) META = await API.get('/api/meta');
  return META;
}

/* ── 渲染帮手 ── */

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function slotName(slot) {
  // 和 catalog.SLOT_NAMES 保持一致。**加槽位时这里也要跟着加** ——
  // 漏了不会报错，只会在界面上原样显示 "L4I"。
  const m = { L1: '内搭', L2: '中间层', L3: '外层', L4I: '内穿下装',
              L4: '下装', L5: '鞋', ACC: '配饰' };
  return m[slot] || slot;
}

function option(list, value, selected) {
  return list.map(v => {
    const val = typeof v === 'object' ? v.value : v;
    const lab = typeof v === 'object' ? v.label : v;
    return `<option value="${esc(val)}"${String(val) === String(selected) ? ' selected' : ''}>${esc(lab)}</option>`;
  }).join('');
}

function colorDot(hex) {
  if (!hex) return '';
  // 方形小色块（2px 圆角），和整体的直角语言一致。
  // 原来画的是 11px 圆点 —— 在搭配卡里挨着衣服名，读起来像单选按钮。
  // 用 inset 阴影当描边而不是 border：border 会让色块实际变成 12px，
  // 深色和浅色两件衣服的色块大小就不一样了。
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;`
    + `background:${esc(hex)};box-shadow:inset 0 0 0 1px rgba(90,80,70,.28);`
    + `vertical-align:-1px;margin-right:5px"></span>`;
}

/* 从名字里抽颜色词，返回调色板颜色名。
   只在「图片测色明确不可信」时使用 —— 上传的是手机截图时，四角取到的是状态栏和
   导航栏，颜色会整个测反（实测 21 张里有 18 张的反的）。模型是看过图之后给的名字，
   这个场景下比一个已知错误的像素测量可靠。颜色词表由 /api/meta 下发，
   和 Python 侧共用一份，避免两边走偏。 */
function colorFromName(name) {
  if (!name || !META || !META.color_words) return null;
  for (const [kw, pal] of META.color_words) {
    if (name.indexOf(kw) >= 0) return pal;
  }
  return null;
}

function itemMeta(it) {
  const bits = [];
  if (it.color_main) bits.push(it.color_main);
  if (it.is_neutral) bits.push('中性色');
  if (it.formality) bits.push('正式度' + it.formality);
  if (it.temp_min != null && it.temp_max != null) bits.push(`${it.temp_min}~${it.temp_max}℃`);
  if (it.thickness) bits.push('厚度' + thickWord(it.thickness));
  return bits.join(' · ');
}

function thumbHtml(it, cls) {
  cls = cls || 'thumb';
  if (it.img_display) return `<img class="${cls}" src="${esc(it.img_display)}" alt="">`;
  return `<div class="${cls} ph">${esc(slotName(it.slot))}</div>`;
}

function tabs(active) {
  // 5 个 tab（套装已降级：入口在今日页「我搭的套装」卡，/sets 直达仍可用）。
  // 图标是内联 SVG（不引图标库）：24×24、stroke 1.6、圆头圆角，
  // 对应 SF Symbols 的 regular 重量 —— 线太粗会显得笨，太细在小尺寸上会糊。
  const items = [
    ['/', '今日', 'today',
     '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2' +
     'M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/>'],
    ['/plan', '计划', 'plan',
     '<rect x="3.2" y="4.8" width="17.6" height="16" rx="3.4"/>' +
     '<path d="M8 2.8v4M16 2.8v4M3.2 10h17.6"/>'],
    ['/wardrobe', '录入', 'wardrobe',
     '<rect x="3.2" y="3.2" width="17.6" height="17.6" rx="4.4"/>' +
     '<path d="M12 8.4v7.2M8.4 12h7.2"/>'],
    ['/prebuy', '新衣试穿', 'prebuy',
     '<path d="M6.2 8h11.6l1 12.2H5.2L6.2 8Z"/>' +
     '<path d="M9.2 8V6.4a2.8 2.8 0 0 1 5.6 0V8"/>'],
    ['/stats', '衣橱体检', 'stats',
     '<path d="M5 20V10.5M12 20V4.5M19 20v-6.5"/>'],
  ];
  // 外层 .tabs 是铺满全宽的毛玻璃背景，内层 .tabs-in 限宽居中 ——
  // 桌面下 5 个 tab 才不会摊到 1440px 的两端去。
  return `<nav class="tabs"><div class="tabs-in">${items.map(([href, label, key, icon]) =>
    `<a href="${href}"${key === active ? ' class="active"' : ''}>` +
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icon}</svg>` +
    `<span>${label}</span></a>`).join('')}</div></nav>`;
}

/* ── 天气 / 城市 ──
   城市搜索是「今日」和「计划」两页共用的，所以放在这里，只写一遍。
   设置一次就落进 data/weather.json，两个页面立刻都知道。 */

const BIAS_LABEL = { am: '早晚', avg: '平均', pm: '午后' };

/* 预报给的是当天最高/最低，"该穿什么"取决于切哪一档。
   三档就是最低 / 平均 / 最高 —— 不搞解释不清的取值。
   注意：档位只决定「按几度挑衣服」；**穿几层始终按当天最低温**
   （后端 plan._warm_of，2026-09-21 按用户反馈"13-22 度也穿短袖"定的）。 */
function biasText(day, bias) {
  if (!day) return '';
  const t = bias === 'am' ? day.temp_am : (bias === 'pm' ? day.temp_pm : day.temp);
  return (t == null ? '' : t + '℃');
}

function tempRangeText(day) {
  if (!day || day.tmin == null || day.tmax == null) return '';
  return `${day.tmin}~${day.tmax}℃`;
}

async function citySearch(q) {
  return API.get('/api/weather/search?q=' + encodeURIComponent(q));
}

async function saveHomeCity(hit) {
  return API.post('/api/weather/config', {
    city: hit.city, admin: hit.admin, country: hit.country,
    lat: hit.lat, lon: hit.lon, timezone: hit.timezone,
  });
}

/* 城市搜索结果列表。点一条要干什么由调用方决定（今日页是"设成我的城市"，
   计划页在旅行模式下是"去这儿"），所以这里只渲染不带行为。 */
function cityHitsHtml(hits, extra) {
  if (!hits || !hits.length) {
    return '<div class="note">没搜到 —— 换个写法试试，比如只写「杭州」。</div>';
  }
  return hits.map((h, i) => `<button class="cityhit" data-city="${i}">${esc(h.label)}</button>`).join('')
    + (extra ? `<div class="note">${esc(extra)}</div>` : '');
}

function toast(msg, ms) {
  // 样式在 app.css 的 .toast —— 深色毛玻璃胶囊，从底部浮上来。
  // 以前这里是内联样式，改配色要翻 JS，收进 CSS 一处就够。
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), ms || 2400);
}

function seasonText(seasons) {
  return (seasons && seasons.length) ? seasons.join(' / ') : '无';
}

/* ── 厚度：数字留给引擎，人话留给用户 ────────────────────────────────
   用户原话："关于衣服的厚度或者温度，我其实没有办法判断。能不能简单地用
   '厚'、'薄'、'中等'这种比较容易理解的词来写，剩下就交给系统来判断，
   或者用一些比较直觉性的提示来告诉大家应该是什么样子的厚度。"

   所以界面上一律说人话（"厚"/"很薄" + 一句"像什么"），1–5 只出现在接口里。
   ⚠️ 词表在这里**只有一份** —— 待入库卡、改字段面板、试衣间三处共用。
   抄第二份必然漂移，最后就是"首页说厚、录入页说很厚"。
   ⚠️ 真正的状态源仍然是那个 `<input type="range" data-?="thickness">`，
   词按钮只是它的**遥控器**：写 value + 派发 `input`，于是提交逻辑一行不用改
   —— 跟今日页那个 hidden `#temp` 是同一招。 */
const THICK_WORDS = ['很薄', '薄', '中等', '厚', '很厚'];
const THICK_HINTS = [
  '像夏天的吊带、雪纺 —— 基本不保暖。',
  '像普通 T 恤、薄衬衫 —— 单穿刚好，起风就想要件外套。',
  '像针织衫、厚衬衫 —— 有点像秋装。',
  '像卫衣、皮衣、厚针织 —— 秋天单穿，或冬天当内层。',
  '像羽绒服、厚大衣 —— 一看就很鼓。',
];
function _thickIdx(v) {
  const n = Math.round(Number(v) || 3);
  return Math.min(5, Math.max(1, n)) - 1;
}
function thickWord(v) { return THICK_WORDS[_thickIdx(v)]; }
function thickHint(v) { return THICK_HINTS[_thickIdx(v)]; }

/** 厚度选择器：一行 5 个词 + 一句直觉提示。`ns` 是属性前缀（f / ef / d）。 */
function thickPicker(ns, v) {
  const i = _thickIdx(v);
  return `<div class="thickbox">
      <label>厚度 <b>${THICK_WORDS[i]}</b></label>
      <input type="range" data-${ns}="thickness" min="1" max="5" value="${i + 1}" class="thickraw">
      <div class="thickpick" data-thickns="${ns}">
        ${THICK_WORDS.map((w, k) =>
          `<button type="button" class="thickbtn${k === i ? ' on' : ''}"`
          + ` data-thick="${k + 1}">${w}</button>`).join('')}
      </div>
      <div class="thickhint">${THICK_HINTS[i]}</div>
    </div>`;
}

/* 遥控器：点词 → 写进 range → 派发 input + change（老逻辑照常跑）→ 再刷新词与提示。
   ⚠️⚠️ **两个事件都要派发，少一个就是静默失败**：
     · `input`   —— 录入页待入库卡（#grid 的 onField）、试穿间（#form）靠它拿到新值；
     · `change`  —— **已入库「改字段」面板靠它落库**（#list 的 `change` → patchField）。
   一开始只派发了 `input`，结果是：词按钮点下去**界面变、数据库不变** ——
   页面一切正常、不报错、刷新就退回去。这正是这个项目栽过十几次的那种坑。
   ⚠️ 顺序不能反：老逻辑会把 label 写成**数字**（`b.textContent = el.value`），
   所以人话必须在**最后**写，否则会被那个数字盖掉。 */
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-thick]');
  if (!b) return;
  const box = b.closest('.thickbox');
  if (!box) return;
  const holder = box.querySelector('[data-thickns]');
  if (!holder) return;
  const raw = box.querySelector(`input[data-${holder.dataset.thickns}="thickness"]`);
  if (!raw) return;
  if (String(raw.value) !== String(b.dataset.thick)) {
    raw.value = b.dataset.thick;
    raw.dispatchEvent(new Event('input', { bubbles: true }));
    raw.dispatchEvent(new Event('change', { bubbles: true }));
  }
  box.querySelectorAll('[data-thick]').forEach(
    (x) => x.classList.toggle('on', x.dataset.thick === String(raw.value)));
  const hint = box.querySelector('.thickhint');
  if (hint) hint.textContent = thickHint(raw.value);
  const lbl = box.querySelector('label b');
  if (lbl) lbl.textContent = thickWord(raw.value);
});

/* 顶栏右上角的设置入口，每个页面自动注入。
   api.js 在 body 末尾加载，此时 .topbar 已经存在。 */
(function injectGear() {
  if (location.pathname === '/settings') return;
  const bar = document.querySelector('.topbar');
  if (bar && !bar.querySelector('.gear')) {
    bar.insertAdjacentHTML('beforeend',
      '<a class="gear" href="/settings" title="接模型 · 设置">设置</a>');
  }
})();