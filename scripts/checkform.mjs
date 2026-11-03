/* 前端卡片渲染自检。
 *
 * 为什么需要：`checkjs` 只检查语法，语法全绿的模板照样可以渲染出空卡片
 * （字段名写错、`meta` 还没加载、模板里引用了不存在的变量……全是运行时才炸）。
 * 这里把页面的内联脚本搬进 node，用一个最小的 DOM 桩跑一遍真渲染，
 * 并且 `fetch` 直接打到本地服务上 —— 用的是真数据，不是假造的。
 *
 * 用法：先起服务（scripts/serve.py），然后
 *     node scripts/checkform.mjs [--out checkform.out.txt]
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { networkInterfaces } from 'node:os';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/* ── 目标地址：必须是"真的在跑本应用"的那个 ──
   ⚠️ 不能写死 `127.0.0.1`（2026-09-27 实拍）：Windows 允许 `0.0.0.0:8077`（本应用）
   和 `127.0.0.1:8077`（**另一个程序**）同时监听。当天另一个项目起了
   `python -m http.server` 占着回环，写死回环的检查就会**静默测到别人家的服务**，
   断言全错而且完全指不到原因。逐个候选地址 /api/ping 验明正身。 */
async function pingApp(base) {
  try {
    const r = await fetch(base + '/api/ping', { cache: 'no-store' });
    return (await r.json()).app === 'capsule-wardrobe';
  } catch { return false; }
}
async function resolveBase() {
  if (process.env.BASE) return process.env.BASE;
  const cands = ['http://127.0.0.1:8077'];
  for (const addrs of Object.values(networkInterfaces())) {
    for (const a of addrs || []) {
      if (a.family !== 'IPv4' || a.internal) continue;
      if (/^(198\.1[89]|169\.254)\./.test(a.address)) continue;
      cands.push(`http://${a.address}:8077`);
    }
  }
  for (const b of cands) if (await pingApp(b)) return b;
  throw new Error(`8077 上找不到本应用，试过：\n    ${cands.join('\n    ')}`);
}
const BASE = await resolveBase();

/* `--out <file>`：把控制台输出**同时**落一份 UTF-8 文件。
   ⚠️ 以前两个 .mjs **根本没有 --out**，传了会被**静默忽略** —— 于是 `.out.txt`
   还是四天前那一次留下的内容，读起来"全绿"，其实这轮一条断言都没落进去（踩过）。
   Git Bash / PowerShell 抓子进程 stdout 都不可靠，所以必须由脚本自己写。
   开头写一行生成时间，陈旧文件一眼可见。 */
const OUT = (() => {
  const i = process.argv.indexOf('--out');
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : null;
})();
if (OUT) {
  let buf = '';
  const orig = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, enc, cb) => {
    buf += typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8');
    return orig(chunk, enc, cb);
  };
  const dump = () => {
    try {
      fs.writeFileSync(OUT, `# node scripts/checkform.mjs  ·  ${new Date().toISOString()}\n` + buf, 'utf8');
    } catch (e) { orig.call(process.stdout, `（--out 写不出去：${e && e.message}）\n`); }
  };
  process.on('exit', dump);
  process.on('SIGINT', () => { dump(); process.exit(130); });
}

/* 今日页的"按几度挑衣服"是**天气自动填的**（用户提的头号需求）。
   直接打真实天气，这套断言就会随当天的气温变色：33℃ 的衣橱排不出搭配，
   「主推画出来了」「每一件都有洗了」全部假失败 —— 测试依赖当天的天气
   是最坏的一种不稳定（哪天热哪天红，还看不出是环境还是代码）。
   这里把**页面看到的那个温度**钉成固定值，其余字段照旧用真天气。
   ⚠️ 只钉 `days=1`（只有今日页用它当温度来源）；
   计划页的 days=1 只拿它当"手填温度"的预填默认值，钉了也无副作用。 */
const TODAY_TEMP = 20;

/* ── 最小 DOM 桩 ──
   不只是"能渲染"，还要能**触发点击** —— 用户的 bug 恰恰是"点了没反应"，
   只验渲染是抓不到这一类问题的。 */
function makeEl(sel) {
  const el = {
    _sel: sel, innerHTML: '', textContent: '', value: '', hidden: false,
    disabled: false, dataset: {}, style: {}, files: [], children: [],
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    removeEventListener() {}, appendChild(c) { this.children.push(c); },
    insertAdjacentHTML() {}, scrollIntoView() {}, getContext: () => null,
    closest: () => null, querySelector: () => null, querySelectorAll: () => [],
    // classList 要**真的记事**（和 fakeTarget 同一套，见下）：
    // 页面里 `classList.toggle('one', cond)` 是标准的两参数写法，
    // 桩里缺 toggle 会直接 TypeError，整段测试挂掉 —— 那是桩的锅，不是页面的锅。
    classList: (() => {
      const cls = new Set();
      return {
        add: (c) => { cls.add(c); },
        remove: (c) => { cls.delete(c); },
        contains: (c) => cls.has(c),
        toggle: (c, force) => {
          const want = force === undefined ? !cls.has(c) : !!force;
          if (want) cls.add(c); else cls.delete(c);
          return want;
        },
      };
    })(),
    parentElement: null, remove() {},
  };
  return el;
}

/* 造一个"被点的按钮"。closest 只在选择器对应 dataset 里真有那个键时才命中 ——
   页面里是 `e.target.closest('[data-tagitem]')` 先判、`closest('[data-del]')` 后判，
   如果 closest 一律返回自己，删除按钮会被误判成标签按钮。 */
function fakeTarget(dataset, text = '删') {
  // classList 要**真的记事**：两步确认（点一下变成"再点一次就删"）是靠
  // `classList.contains('armed')` 判的，桩里恒返回 false 的话第二次点会被
  // 当成第一次，于是断言看到"没发请求"—— 那是桩的错，不是页面的错。
  const cls = new Set();
  const t = {
    dataset, textContent: text, disabled: false,
    classList: {
      add: (c) => { cls.add(c); },
      remove: (c) => { cls.delete(c); },
      contains: (c) => cls.has(c),
      toggle: (c, force) => {
        const want = force === undefined ? !cls.has(c) : !!force;
        if (want) cls.add(c); else cls.delete(c);
        return want;
      },
    },
    closest: (sel) => {
      // 两种写法都支持：`[data-del]`（无值）和 `[data-act="wear"]`（带值）。
      // ⚠️ **带值的必须真的比值** —— 真浏览器的 closest 就是这么干的。
      // 只会"有那个键就命中"的话，页面里 `closest('[data-act="unwash-all"]')`
      // 在点到 `data-act="wear"` 时也会命中 = 让测试**假通过**；
      // 而只认无值选择器（旧版）则相反：真浏览器明明认，桩直接返回 null，
      // 于是给出一个**假失败**（写洗衣篮那节时实测踩过 ——「全部恢复」被误判成
      // "点了没反应"，查了半天是桩的锅，不是页面的锅）。
      const m = /\[data-([a-z0-9-]+)(?:="([^"]*)")?\]/i.exec(sel);
      if (!m) return null;
      const key = m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (dataset[key] === undefined) return null;
      if (m[2] !== undefined && String(dataset[key]) !== m[2]) return null;
      return t;
    },
  };
  return t;
}

/* 对元素派发一次事件。注意**返回 Promise**：处理器是 async 的，
   不等它就断言会看到"还没发请求"的假失败。
   `type` 默认 click；分段目的地的「这一段几天」是 change，
   所以这个辅助函数要能派发别的事件类型（否则那一条根本测不到）。 */
async function fire(el, target, type = 'click') {
  const handlers = el._listeners[type] || [];
  if (!handlers.length) throw new Error(`元素 ${el._sel} 上没有 ${type} 监听器`);
  await Promise.all(handlers.map((h) =>
    h({ target, preventDefault() {}, stopPropagation() {} })));
}

function makeDoc(store) {
  const body = makeEl('body');
  body._toasts = [];
  body.appendChild = function (c) {
    this.children.push(c);
    if (c && typeof c.textContent === 'string') this._toasts.push(c.textContent);
  };
  return {
    querySelector(sel) {
      if (!store.has(sel)) store.set(sel, makeEl(sel));
      return store.get(sel);
    },
    querySelectorAll: () => [],
    getElementById: (id) => this ? this.querySelector('#' + id) : null,
    createElement: (t) => makeEl(t),
    body,
    addEventListener() {},
    documentElement: makeEl('html'),
  };
}

/* op 里可以指定 deleteStatus：让 DELETE 返回成功 / 失败，从而验证
   "失败必须看得见" 这条 —— 之前就是因为失败被静默吞掉，用户才说"点了没办法删除"。 */
async function render(page, run, op = {}) {
  const store = new Map();
  const document = makeDoc(store);
  const html = fs.readFileSync(path.join(ROOT, 'web', page), 'utf8');
  const api = fs.readFileSync(path.join(ROOT, 'web', 'js', 'api.js'), 'utf8');

  // 取所有内联 <script>（带 src 的跳过 —— api.js 手动拼在前面）
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]);

  const code = api + '\n' + blocks.join('\n') + `
;globalThis.__probe = {
  get meta() { return META; },
  set meta(v) { META = v; },
  get draft() { return draft; },
  set draft(v) { draft = v; },
  blank: typeof blank === 'function' ? blank : null,
  renderForm: typeof renderForm === 'function' ? renderForm : null,
  openBlank: typeof openBlank === 'function' ? openBlank : null,
  // 录入页「已入库」列表的就地编辑：展开状态存在模块作用域的 Set 里，
  // 桩外面看不到，只能显式暴露出来才验得了"点开会展开"。
  get openIds() { return typeof openIds !== 'undefined' ? openIds : null; },
  renderList: typeof renderList === 'function' ? renderList : null,
  // 待入库：pending / render 都是模块作用域的，桩外面看不见。
  // 「一行一件」改造之后必须能验"待入库也是行式 + 同段提示收成顶一条"。
  get pending() { return typeof pending !== 'undefined' ? pending : null; },
  set pending(v) { if (typeof pending !== 'undefined') pending = v; },
  render: typeof render === 'function' ? render : null,
  newCard: typeof newCard === 'function' ? newCard : null,
  // 设置页：体感舒适温度那套
  maxTopWarmth: typeof maxTopWarmth === 'function' ? maxTopWarmth : null,
  paintComfort: typeof paintComfort === 'function' ? paintComfort : null,
  saveComfort: typeof saveComfort === 'function' ? saveComfort : null,
  // 今日页的洗衣篮：喂一份假数据进去，就能验"那条提示画得对不对"
  renderLaundry: typeof renderLaundry === 'function' ? renderLaundry : null,
  setWashed: typeof setWashed === 'function' ? setWashed : null,
  // 计划页的分段目的地：segs 是模块作用域的，桩外面看不见 ——
  // 不暴露就只能靠"真的搜一次城市"去驱动，那种断言会同时依赖网络和城市库。
  get segs() { return typeof segs !== 'undefined' ? segs : null; },
  set segs(v) { if (typeof segs !== 'undefined') segs = v; },
  renderMode: typeof renderMode === 'function' ? renderMode : null,
  renderSegs: typeof renderSegs === 'function' ? renderSegs : null,
  openPicker: typeof openPicker === 'function' ? openPicker : null,
  segMetaList: typeof segMetaList === 'function' ? segMetaList : null,
};`;

  // 记录 DELETE 请求：**不真的打服务**，免得验证前端时把真实单品删掉
  const deletes = [];
  const setWrites = [];      // 套装页的存/改 —— 同上，只记账不落库
  const planBodies = [];     // /api/plan 的请求体（验分段目的地到底发了什么）
  const realFetch = (url, opt) => fetch(url.startsWith('http') ? url : BASE + url, opt);

  const sandbox = {
    document,
    location: { href: BASE + '/' + page.replace('.html', ''), pathname: '/' + page.replace('.html', ''), search: '' },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: 'node' },
    URLSearchParams,
    setTimeout, clearTimeout, console,
    Math, JSON, Date, Set, Map, Array, Object, String, Number, Boolean, Error, Promise, RegExp,
    fetch: async (url, opt) => {
      /* 天气：见文件头 TODAY_TEMP 的说明 —— 把「今天按几度挑衣服」钉死，
         让今日页的断言与当天的真实气温脱钩（换季不再莫名其妙变红）。 */
      if (/\/api\/weather$/.test(String(url).split('?')[0])
          && String(url).includes('days=1')) {
        const r = await realFetch(url, opt);
        const txt = await r.text();
        let d;
        try { d = JSON.parse(txt); } catch (e) { return { ok: r.ok, status: r.status, text: async () => txt }; }
        if (d && d.day) {
          d.day.temp = TODAY_TEMP;
          if (d.day.temp_am != null) d.day.temp_am = TODAY_TEMP;
          if (d.day.temp_pm != null) d.day.temp_pm = TODAY_TEMP;
        }
        return { ok: r.ok, status: r.status, text: async () => JSON.stringify(d) };
      }

      if (opt && String(opt.method).toUpperCase() === 'DELETE') {
        deletes.push(url);
        const st = op.deleteStatus ?? 200;
        return {
          ok: st >= 200 && st < 300, status: st,
          text: async () => (st >= 300
            ? JSON.stringify({ detail: '模拟的删除失败' }) : '{"ok":true}'),
        };
      }
      /* 计划页把 use_llm 写死成 true。测试不该每次跑都去掏钱调模型，
         也不该因为没配 Key 就挂 —— 在这一层把它按下去。
         页面逻辑、请求、渲染路径一条都没少走，少掉的只是那句总结。 */
      if (opt && String(opt.method).toUpperCase() === 'POST'
          && String(url).includes('/api/plan')) {
        try {
          const b = JSON.parse(opt.body);
          // 分段目的地要验的是"页面到底把什么发出去了"（segments / days），
          // 所以先原样记一份，再改 use_llm。
          planBodies.push(b);
          b.use_llm = false;
          opt = { ...opt, body: JSON.stringify(b) };
        } catch (e) { /* body 不是 JSON 就原样发出去 */ }
      }

      /* 套装页的「存下来」是真写库的。这里只记账、回一个假对象，
         免得验证前端时往用户的表里塞东西。
         注意 `/api/sets/evaluate` 是**只读预演**，要放它过去打真服务 ——
         正则用 `$` 锚住，只拦 `/api/sets` 和 `/api/sets/<id>`。 */
      if (/\/api\/sets(\/\d+)?$/.test(String(url).split('?')[0])
          && ['POST', 'PATCH'].includes(String(opt?.method).toUpperCase())) {
        setWrites.push({ url, method: String(opt.method).toUpperCase(), body: opt.body });
        return {
          ok: true, status: 200,
          text: async () => JSON.stringify(
            { id: 999, name: '假的', n_items: 2, items: [], occasion: null }),
        };
      }

      /* 「今日」每次加载都会落库 3 条搭配（`/api/today` 的 save 参数）——
         那是给用户点反馈用的 outfit_id，本身没错；但**测试每跑一次就多 3 条**
         （实测真实库里攒了 78 条 id 900xxx 的空记录）。
         这里按下去：`save=0` 只算不存，页面渲染路径一点没少走。 */
      /* 设置页的「体感舒适温度」也是真写库的 —— 只记账，别动用户的配置。 */
      if (/\/api\/prefs$/.test(String(url).split('?')[0])
          && String(opt?.method).toUpperCase() === 'POST') {
        setWrites.push({ url, method: 'POST', body: opt.body });
        return {
          ok: true, status: 200,
          text: async () => JSON.stringify(
            { ok: true, prefs: { comfort: JSON.parse(opt.body || '{}').comfort ?? 22 } }),
        };
      }

      /* 洗衣篮：洗了 / 恢复都是**真写** data/laundry.json 的 —— 只记账，
         别把用户正在洗的衣服搅乱。（注意 `/reset` 要一起拦，别漏。） */
      if (/\/api\/laundry(\/reset)?$/.test(String(url).split('?')[0])
          && String(opt?.method).toUpperCase() === 'POST') {
        setWrites.push({ url, method: 'POST', body: opt.body });
        return {
          ok: true, status: 200,
          text: async () => JSON.stringify({ ok: true, washed_ids: [] }),
        };
      }

      if (String(url).includes('/api/today') && !String(url).includes('save=')) {
        const sep = String(url).includes('?') ? '&' : '?';
        url = String(url) + sep + 'save=0';
      }
      return realFetch(url, opt);
    },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const fn = new Function(...Object.keys(sandbox), code);
  fn(...Object.values(sandbox));

  await new Promise((r) => setTimeout(r, 1500));   // 等 boot() 把 meta 拉回来
  return run(sandbox.globalThis.__probe, store, document,
             { deletes, setWrites, toasts: document.body._toasts, fire, fakeTarget,
               planBodies });
}


const fails = [];
function check(ok, label) {
  console.log(`    ${ok ? '✓' : '✗'} ${label}`);
  if (!ok) fails.push(label);
}

/* ══════════════ 新衣试穿间：录一件想买的 ══════════════ */

console.log('\n新衣试穿间 · 被模型填好之后的那张卡');

await render('prebuy.html', (p, store) => {
  console.log(`    meta 已加载：${p.meta ? '是' : '否'}（槽位 ${p.meta ? Object.keys(p.meta.slots).length : 0} 个）`);
  check(!!p.blank && !!p.renderForm, '页面暴露了 blank / renderForm');

  // 模拟"上传完 + 模型识别完"的状态
  const d = p.blank();
  Object.assign(d, {
    uid: 'c5a5908db62d', img_display: '/img/c5a5908db62d_display.webp',
    img_llm: '/img/c5a5908db62d_llm.jpg', img_raw: '/img/c5a5908db62d_raw.jpg',
    name: '藏青直筒裤', slot: 'L4', category: '直筒裤',
    color_main: '藏蓝', color_hex: '#1F2A44', is_neutral: 1,
    formality: 2, thickness: 3, temp_min: 10, temp_max: 26,
    length_band: '长', fit: '合身', material: '棉', pattern: '纯色',
    _source: 'model', _picked: true,
  });
  p.draft = d;
  p.renderForm();
  const h = store.get('#form').innerHTML;

  check(h.includes('藏青直筒裤'), '名称出现在卡片里');
  check(h.includes('/img/c5a5908db62d_display.webp'), '图片用上了裁剪后的展示图');
  check(h.includes('自动识别'), '打了「自动识别」徽标');
  check(h.includes('下装 · 直筒裤'), '摘要行说明了槽位与品类');
  check(h.includes('data-d="purchase_price"'), '价格字段在场（CPW 要用）');
  check(h.includes('data-d="intent"'), '「想买的理由」在场');
  check(h.includes('id="saveCand"'), '保存按钮在场');
  check(!h.includes('gc-slots'), '模型已识别 → 不该再逼用户点槽位');
  check(!h.includes('gc-extra'), '手填字段默认收起（要「改字段」才展开）');
  check(h.includes('data-act="more"'), '有「改字段」开关');

  // 展开手填
  d._open = true;
  p.renderForm();
  const h2 = store.get('#form').innerHTML;
  check(h2.includes('gc-extra'), '展开后出现手填区');
  check(h2.includes('data-d="slot"') && h2.includes('data-d="formality"'), '手填区含槽位与正式度');

  // 没接模型的兜底：必须能选槽位
  d._source = 'local'; d._picked = false; d._open = false;
  d._note = '还没接视觉模型 —— 现在只能手点槽位。';
  p.renderForm();
  const h3 = store.get('#form').innerHTML;
  check(h3.includes('gc-slots'), '没模型时给出槽位按钮');
  check(h3.includes('data-slot="L3"'), '槽位按钮齐（含外层）');
  check(h3.includes('还没接视觉模型'), '说清了为什么只能手填');
});

/* ══════════════ 录入页：待入库卡片 + 去重 ══════════════ */

console.log('\n录入页 · 待入库卡片与疑似重复');

await render('wardrobe.html', async (p, store) => {
  // 疑似重复：它读的是真接口，直接验渲染结果。
  const dups = await (await fetch(BASE + '/api/duplicates')).json();
  const el = store.get('#dupesCard');
  const box = store.get('#dupes');
  console.log(`    接口返回 ${dups.pairs.length} 组可疑重复`);
  if (dups.pairs.length) {
    check(!el.hidden, '去重卡片没有被 hidden');
    check(box.innerHTML.includes('duppair'), '去重卡片里画出了对照块');
    check(box.innerHTML.includes('data-dupdel'), '每组都有「删这件」按钮');
    check((box.innerHTML.match(/data-dupdel/g) || []).length === dups.pairs.length * 2,
      `按钮数 = 组数 × 2（${dups.pairs.length * 2} 个）`);
  } else {
    check(el.hidden, '没有重复时卡片自动隐藏');
  }
});

console.log('\n录入页 · 待入库是「一行一件」+ 同段提示收成顶一条');

await render('wardrobe.html', async (p, store) => {
  // 待入库卡片的形状：一段"26 张卡各印一遍同一句话"的实景。
  // 直接调 render 是可行的 —— 只要先把 pending 填好（newCard 会套品类默认值）。
  if (!p.newCard || !p.render || !p.pending) {
    check(false, '待入库渲染函数没暴露出来（桩该补 __probe 了）');
    return;
  }
  const cards = [];
  for (let i = 0; i < 6; i++) {
    const c = p.newCard({ name: '衣服' + (i + 1), uid: '' });
    c._picked = true;
    // 4 张是手机截图（同一句话），2 张各有各的问题 —— 验"只提重复的、单条不搬"
    c._note = i < 4 ? '这看着是手机截图，不是原图 —— 颜色多半测反了'
                    : (i === 4 ? '这一件的问题只有它有' : '');
    cards.push(c);
  }
  p.pending = cards;
  p.render();
  const grid = String(store.get('#grid').innerHTML);
  const hoist = String(store.get('#hoistNote').innerHTML);
  const hoistHidden = !!store.get('#hoistNote').hidden;

  check(grid.includes('gcard prow'), '待入库也是「一行一件」的行（不是大卡片）');
  check((grid.match(/data-slot="/g) || []).length === 6 * 7, '槽位按钮一件都不少（6 件 × 7 槽位）');
  check((grid.match(/data-act="more"/g) || []).length === 6, '每件都还有「改字段」');
  check((grid.match(/data-act="del"/g) || []).length === 6, '每件都还有「删」');

  check(!hoistHidden, '重复提示被提到了列表顶');
  check(hoist.includes('4 件'), `顶上写清是几件（实际：${hoist.replace(/<[^>]+>/g, '')}）`);
  // 4 张卡上的同一句话收进顶条 → 卡片里只剩 1 条（第 5 件独有的那句）
  check((grid.match(/gc-warn/g) || []).length === 1,
    `重复的那几句不再逐卡印（卡片里剩 ${(grid.match(/gc-warn/g) || []).length} 条，应该只剩 1 条独有的）`);
  check(grid.includes('这一件的问题只有它有'), '只出现一次的那句仍然留在它自己的卡片上（不丢信息）');
});

/* ══════════════ 录入页：已入库的件要能在 App 里改 ══════════════ */

/* 用户原话："有些衣服录进去的时候厚度不对，但好像没办法直接在 app 里改"。
   原因是「改字段」只在上传队列里有（那一串卡片），入完库就只剩一个「删」——
   录入那一刻的错值永远改不掉。这里把入口钉住。 */

console.log('\n录入页 · 已入库单品的字段要能就地改');

await render('wardrobe.html', async (p, store, doc, t) => {
  const list = store.get('#list');
  const h = String(list.innerHTML);
  console.log(`    列表里有 ${(h.match(/data-open="/g) || []).length} 件单品`);

  check(!!p.renderList, '列表渲染函数在场');
  check((h.match(/data-open="/g) || []).length > 0, '每件都有「改字段」入口');
  check(!h.includes('data-ef="thickness"'), '默认收起（不把列表撑成一屏表单）');
  // 一行一件：每行都要是 item-row（不再是大卡片）
  check(h.includes('item-card item-row'), '已入库是「一行一件」的行，不是大卡片');

  const id = (h.match(/data-open="(\d+)"/) || [])[1];
  check(!!id, `取到第一件的 id（${id}）`);
  if (!id) return;

  await t.fire(list, t.fakeTarget({ open: id }, '改字段'));
  const h2 = String(list.innerHTML);
  // 掀面板：必须是 .item-sheet（覆盖式），**且仍画在 #list 里** ——
  // 挪到 body 上会静默失效（这一页所有交互都委托在 #list 上）
  check(h2.includes('item-card item-sheet'), '「改字段」掀出一张覆盖式面板');
  check(h2.includes('data-close="1"'), '面板有遮罩/关闭按钮（不然点不出来）');
  check(h2.includes('data-ef="thickness"'),
    '展开后出现**厚度**（滑杆仍在场，但现在是藏起来的"状态源"，给人看的是 5 个词）');
  check(h2.includes('data-ef="temp_min"') && h2.includes('data-ef="temp_max"'),
    '温度区间也能改');
  check(h2.includes('data-ef="formality"'), '正式度在场');
  check(h2.includes('data-ef="warmth"'),
    '**保暖值**也在场（26℃穿衣法则的那个数，能逐件改）');
  check(h2.includes('data-reset='), '有「按品类重置」（厚度错了多半要整组回到品类默认）');
  check(h2.includes(`data-del="${id}"`), '删除也搬进了面板（列表行里不再有它）');
  // ⚠️ 这一条是给 patchField 兜底的：面板上必须挂 item-card 类，
  //    否则 el.closest('.item-card') 取不到 id → 改了不落库、且不报错
  check(/item-card item-sheet"[^>]*data-id="\d+"/.test(h2.replace(/\s+/g, ' ')),
    '面板带着 data-id 和 item-card 类（patchField 靠 closest 取 id，缺了就静默不落库）');

  // 只画一张面板：同时揭多张会叠成一摞
  check((h2.match(/item-card item-sheet/g) || []).length === 1, '同时只揭一张面板');

  // 点关闭 / 遮罩要收起来
  await t.fire(list, t.fakeTarget({ close: '1' }, '关闭'));
  check(!String(list.innerHTML).includes('item-card item-sheet'), '点关闭收起来');

  // 再点一次也要能重新揭开（不是"点过一次就再也打不开"）
  await t.fire(list, t.fakeTarget({ open: id }, '改字段'));
  check(String(list.innerHTML).includes('data-ef="thickness"'), '能重新揭开');
});

/* ══════════════ 设置页：体感舒适温度（26℃穿衣法则） ══════════════ */

/* 这个数字决定"多少度以上能只穿一件、多少度以下必须有第二层" ——
   它错了整个推荐都错。所以钉三件事：控件在场、前端算的"能穿到几度"和
   口径一致、改它会真的发出去。 */

console.log('\n设置页 · 体感舒适温度（26℃穿衣法则）');

await render('settings.html', async (p, store, doc, t) => {
  const box = store.get('#comfortNote');
  const shown = String(box.textContent || '') + String(box.innerHTML || '');
  console.log(`    「能穿到」那一行：${shown.replace(/\s+/g, ' ').slice(0, 60)}`);
  check(/℃/.test(shown), '设置页算出了"这套衣橱能穿到几度"');

  check(!!p.maxTopWarmth && !!p.paintComfort, '两件关键函数都在场');

  const items = await (await fetch(BASE + '/api/items?stage=owned')).json();
  const top = p.maxTopWarmth(items);
  console.log(`     上装叠满的保暖值（前端算）= ${top}℃`);
  check(top > 0, '前端算出的最大保暖值 > 0（说明 item.warmth 真的下发了）');

  // 口径自洽：能穿到的最低温度 = 舒适线 − 最大保暖值
  const prefs = await (await fetch(BASE + '/api/prefs')).json();
  const limit = prefs.prefs.comfort - top;
  check(shown.includes(limit.toFixed(1)),
    `页面上写的就是 舒适线 ${prefs.prefs.comfort} − 保暖 ${top} = ${limit.toFixed(1)}℃`);

  // 保存必须真的发出去（写操作在桩里被拦，只记账）。
  // ⚠️ 不能用 `querySelectorAll('[data-comfort]')` 派发点击 —— 桩里它恒返回空数组，
  //    预设按钮压根没挂上监听器（那是桩的限制，不是页面的错）。直接调保存函数。
  const box2 = store.get('#comfort');
  box2.value = '26';
  await p.saveComfort(top);
  check(t.setWrites.some((w) => w.url.includes('/api/prefs')),
    `改舒适线发出了 POST /api/prefs（${t.setWrites.length} 次写）`);
  const sent = t.setWrites.find((w) => w.url.includes('/api/prefs'));
  check(sent && String(sent.body).includes('26'),
    `发出去的值是 26（实际 ${sent ? sent.body : '没发'}`);
});

/* ══════════════ 录入页：删除真的能删（用户报的 bug） ══════════════ */

console.log('\n录入页 · 「删这件」点下去要真的删（之前点了没反应）');

await render('wardrobe.html', async (p, store, doc, t) => {
  const dups = await (await fetch(BASE + '/api/duplicates')).json();
  if (!dups.pairs.length) {
    check(true, '当前没有疑似重复件，跳过这条点击测试');
    return;
  }
  const disp = store.get('#dupes');
  const id = dups.pairs[0].a.id;
  const btn = t.fakeTarget({ dupdel: String(id) }, '删这件');

  await t.fire(disp, btn);
  check(t.deletes.length === 0, '第一次点**不发**删除请求（两步确认）');
  check(String(btn.textContent).includes('确认'),
    `按钮进入待确认状态（"${btn.textContent}"）`);

  await t.fire(disp, btn);
  check(t.deletes.length === 1, `第二次点发出了 DELETE（${t.deletes[0] || '没发'}）`);
  check(String(t.deletes[0] || '').includes('/api/items/' + id), '删的正是被点的那一件');
  check(t.toasts.some((x) => x.includes('已删除')),
    `成功有可见提示（toasts=${JSON.stringify(t.toasts)}）`);
});

console.log('\n录入页 · 删除失败必须看得见（不要再静默吞掉）');

await render('wardrobe.html', async (p, store, doc, t) => {
  const dups = await (await fetch(BASE + '/api/duplicates')).json();
  if (!dups.pairs.length) {
    check(true, '当前没有疑似重复件，跳过这条失败路径测试');
    return;
  }
  const disp = store.get('#dupes');
  const btn = t.fakeTarget({ dupdel: String(dups.pairs[0].a.id) }, '删这件');
  await t.fire(disp, btn);
  await t.fire(disp, btn);
  check(t.deletes.length === 1, '失败时也确实发出了请求');
  check(t.toasts.some((x) => x.includes('删除失败')),
    `失败给出明确提示（toasts=${JSON.stringify(t.toasts)}）`);
  check(btn.disabled === false, '失败后按钮恢复可点（可以再试）');
}, { deleteStatus: 500 });

console.log('\n录入页 · 「已入库」列表里的删按钮');

await render('wardrobe.html', async (p, store, doc, t) => {
  const items = await (await fetch(BASE + '/api/items?stage=owned')).json();
  if (!items.length) {
    check(true, '衣橱是空的，跳过');
    return;
  }
  const list = store.get('#list');
  const btn = t.fakeTarget({ del: String(items[0].id) }, '删');
  await t.fire(list, btn);
  await t.fire(list, btn);
  check(t.deletes.length === 1, `列表里的删也能发出 DELETE（${t.deletes[0] || '没发'}）`);
  check(t.toasts.some((x) => x.includes('已删除')), '列表删除也有可见提示');
});

/* 等页面把结果画出来。
   桩里的 `innerHTML`/`textContent` 是两个独立字段（真 DOM 里 setTextContent 会清空
   innerHTML，这里不会），所以 `#msg` 上那句"排算中"永远清不掉 —— 不能用它判断忙闲。
   改看 `#sub`：它在 run() 开头被清空、在 render()/renderPicker() 里才被写上。 */
async function waitSub(store, ms = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (String(store.get('#sub').textContent || '').trim()) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

/* ══════════════ 今日页：温控条 ══════════════
   这是用户提的头号需求：「今日是不是可以直接查温度默认为当日气温」。
   注意天气是**直连 open-meteo**，不经大模型（大模型不联网，问它几度它会编）。

   2026-09-25 改版后，温度不再用滑杆：
     · 整屏**只有一个大数字**（#tempv），就是真正挑衣服用的那个 ——
       上一版同屏并排着天气的 33℃ 和滑杆的 20℃，两个都是衬线大字，没法判断按哪个。
     · 控制收进默认收起的 #panel；档位（#biasseg）和城市搜索（#wxcity）都搬进去。
     · #temp 变成 hidden input，仍是**唯一状态源**。
   断言跟着改：不再查 #wx，改查 #wxsum / #wxbias / #wxcity。 */

console.log('\n今日页 · 温控条：温度默认填当天的天气');

await render('today.html', async (p, store) => {
  const wx = await (await fetch(BASE + '/api/weather?days=1')).json();
  if (wx && wx.day) wx.day.temp = TODAY_TEMP;   // 和桩里对页面做的是同一件事
  const sum = String(store.get('#wxsum').textContent || '');
  const shown = String(store.get('#tempv').textContent || '');
  const sv = String(store.get('#tempsv').textContent || '');
  const seg = String(store.get('#biasseg').innerHTML || '');
  const city = String(store.get('#wxcity').innerHTML || '');
  const slider = store.get('#temp').value;
  const env = String(store.get('#envnote').innerHTML || '');
  const label = String(store.get('#templabel').textContent || '');

  check(Number.isFinite(Number(slider)), '温度有一个状态源（#temp，hidden）');
  check(/℃/.test(shown), `温控条上有一个大数字（"${shown}"）`);
  check(/挑衣服/.test(label), `那个数字是干什么用的，一句话写在下面（"${label}"）`);

  if (wx.ok && wx.day) {
    console.log(`    天气：${wx.label} ${wx.day.tmin}~${wx.day.tmax}℃ → 按 ${wx.day.temp}℃`);
    check(String(shown) === `${wx.day.temp}℃`,
      `温控条上的大数字就是当天温度（"${shown}"）`);
    check(String(sv) === `${wx.day.temp}℃`,
      `步进器里跟的是**同一个数**，不是第二套温度（"${sv}"）`);
    check(Number(slider) === Number(wx.day.temp),
      `状态源被自动填成当天温度（${slider} / 天气 ${wx.day.temp}）`);
    check(sum.includes(wx.label.split(' · ')[0]), '温控条上写出了城市名');
    check(sum.includes('℃'), '温控条上带出了当天的温度区间');
    check(seg.includes('data-bias="am"') && seg.includes('data-bias="pm"'),
      '早/平均/午后三档可切（在面板里）');
    check(store.get('#wxcity').hidden === true, '天气拿到了 → 不再显示设城市');
    check(store.get('#panel').hidden === true, '控制面板默认收起（首屏留给衣服）');
  } else {
    console.log(`    没设城市或取不到（${wx.reason || ''}）`
      + ' → 应当**自动展开面板**并就地给搜索框，不能白屏也不能编一个温度');
    check(city.includes('id="wxq"'), '就地给出设城市的输入框');
    check(store.get('#panel').hidden === false,
      '天气取不到时面板自动展开（否则设城市的入口藏在收起的面板里，用户被永久困住）');
    check(!String(slider).includes('undefined'), '状态源没有被写成 undefined');
  }
  check(env.includes('/plan'), '首页有去「排一周/旅行」的入口');
});

/* ══════════════ 今日页：温控条只有一个大数字、控制默认收起 ══════════════
   这条是这次改版的核心主张 —— "今日页不是控制台，是一份答案"。
   实测证据在 docs/04-界面改造方案.md §2：改前搭配卡顶部 584px / 视口 844px。 */

console.log('\n今日页 · 首屏是衣服，不是控制项');

await render('today.html', async (p, store, doc, t) => {
  // 点「调」要真的展开 —— 这是"点了没反应"最容易发生的一处
  const before = store.get('#panel').hidden;
  await t.fire(store.get('#tuneBtn'), t.fakeTarget({}));
  check(store.get('#panel').hidden !== before,
    `点「调」能展开 / 收起面板（${before} → ${store.get('#panel').hidden}）`);
  check(store.get('#tuneBtn').textContent === (before ? '收起' : '调'),
    `「调」的文案跟着状态走（"${store.get('#tuneBtn').textContent}"）`);

  // 步进器：按下「+」温度必须真的 +1（1℃ 能差出几倍搭配，见 docs/04 §2）
  const t0 = Number(store.get('#temp').value);
  await t.fire(store.get('#tplus'), t.fakeTarget({}, '+'));
  const t1 = Number(store.get('#temp').value);
  check(t1 === t0 + 1, `按一次「+」温度 +1（${t0} → ${t1}）`);
  check(String(store.get('#tempv').textContent) === `${t1}℃`,
    '温控条上的大数字跟着变（不是两个数各说各话）');
  check(String(store.get('#tempsv').textContent) === `${t1}℃`,
    '步进器里的数字也跟着变');

  await t.fire(store.get('#tminus'), t.fakeTarget({}, '−'));
  check(Number(store.get('#temp').value) === t0, `按一次「−」回到原值（${t0}）`);
});


/* ══════════════ 今日页：洗了 / 恢复 ══════════════
   用户原话：「在今日穿搭的衣服里面加一个洗了按钮，按完就不会搭配这个衣服。
   上面或者下面有一个恢复，按了就会把所有洗了的衣服恢复到池子里。」

   "洗了的件真的不出现在推荐里"是**后端**的事（`scripts/test_wash.py` 钉住了）。
   这里只验前端两件事：① 每件衣服上真的有「洗了」② 洗衣篮那条画得对、
   而且「洗了」「全部恢复」都真的发出去了（不是点了没反应）。 */

console.log('\n今日页 · 洗了 / 恢复');

await render('today.html', async (p, store, doc, t) => {
  check(!!p.renderLaundry, '页面暴露了 renderLaundry');

  /* ⚠️ 洗衣篮是**用户真实状态**（data/laundry.json），不是测试造出来的 ——
     断言绝不能假设"现在一件都没洗"，否则用户真洗了一件，测试就变红
     （而且红得莫名其妙）。这里按接口返回的真实件数分支来判。 */
  const today = await (await fetch(
    BASE + `/api/today?temp=${TODAY_TEMP}&n=3&save=0`)).json();
  const nWashed = (today.washed || []).length;

  const hero = store.get('#hero').innerHTML;
  const nItems = (hero.match(/class="oi"/g) || []).length;
  const nWash = (hero.match(/data-wash="/g) || []).length;
  console.log(`    主推 ${nItems} 件，带「洗了」的 ${nWash} 件`
    + `（库里有 ${nWashed} 件正在洗）`);
  check(nItems > 0, '主推画出来了');
  check(nItems > 0 && nWash === nItems,
    `每一件衣服上都有一个「洗了」（${nWash}/${nItems}）`);

  // 洗衣篮的显隐必须跟真实件数**一致**（0 件时就该藏起来）
  check(store.get('#laundry').hidden === (nWashed === 0),
    nWashed === 0
      ? '没洗的时候洗衣篮是隐藏的'
      : `有 ${nWashed} 件正在洗 → 洗衣篮露出来（真实状态，不是测试造的）`);

  // 点一下「洗了」必须真的发出去
  await t.fire(store.get('#hero'), t.fakeTarget({ wash: '98' }, '洗了'));
  const w = t.setWrites.find((x) => x.url.includes('/api/laundry'));
  check(!!w, `点「洗了」发出了 POST /api/laundry（共 ${t.setWrites.length} 次写）`);
  check(!!w && String(w.body).includes('98'),
    `发的正是被点的那一件（body=${w ? w.body : '没发出去'}）`);

  // 洗衣篮长什么样 —— 喂一份假数据给渲染函数
  p.renderLaundry({ washed: [
    { id: 98, name: '深灰棉质T恤', slot: 'L1' },
    { id: 12, name: '藏青连帽卫衣', slot: 'L2' },
  ] });
  const lh = store.get('#laundry').innerHTML;
  check(store.get('#laundry').hidden === false, '有洗了的件时洗衣篮露出来');
  check(lh.includes('2 件') || lh.includes('<b>2</b>'), '写明了件数');
  check(lh.includes('深灰棉质T恤') && lh.includes('藏青连帽卫衣'),
    '把名字都列出来了（不然用户根本不知道洗了哪几件）');
  check(lh.includes('data-unwash="98"'), '每件名字本身可以点（单件放回）');
  check(lh.includes('data-act="unwash-all"'), '有「全部恢复」按钮');
  check(!/\bundefined\b/.test(lh), '洗衣篮里没有漏出 undefined');

  // 「全部恢复」必须真的发出去
  t.setWrites.length = 0;
  await t.fire(store.get('#laundry'),
    t.fakeTarget({ act: 'unwash-all' }, '全部恢复'));
  check(t.setWrites.some((x) => x.url.includes('/api/laundry/reset')),
    '点「全部恢复」发出了 POST /api/laundry/reset');

  // 空了就自己藏回去
  p.renderLaundry({ washed: [] });
  check(store.get('#laundry').hidden === true, '恢复之后洗衣篮自己藏起来');
});

/* ══════════════ 计划页：一周（在家，每天不重样）══════════════ */

console.log('\n计划页 · 一周（真天气 + 真排布）');

await render('plan.html', async (p, store, doc, t) => {
  const settled = await waitSub(store);
  console.log(`    boot 自动排了一轮：${settled ? '完成' : '超时'}`
    + `（#sub="${store.get('#sub').textContent}"）`);
  check(settled, '页面加载后自动排出了结果（不是空白屏）');

  const h = store.get('#result').innerHTML;
  const strips = (h.match(/class="pstrip"/g) || []).length;
  const pack = (h.match(/class="packrow"/g) || []).length;
  console.log(`    ${strips} 个日条 / ${pack} 行打包清单`);
  check(strips >= 5, `一周至少画出 5 天（实际 ${strips}）`);
  check(h.includes('class="pitem"'), '日条里真的有单品块');
  check(pack >= 5, `打包清单画出来了（${pack} 行）`);
  check(!/\bundefined\b/.test(h), '界面上没有漏出 undefined');

  /* 口径变了：跨天"算不算换了穿搭"看**上衣**（换裤子不算）。
     副标题上要看得到这个数，否则用户没法判断这周是不是每天换了上衣。 */
  const sub = store.get('#sub').textContent;
  console.log(`    副标题：${sub}`);
  check(/上衣不重样\s*\d+\s*天/.test(sub), '副标题写出了「上衣不重样 N 天」');
  check(!/undefined/.test(sub), '副标题没有漏出 undefined');

  const marks = (h.match(/class="dempty"/g) || []).length;
  check(strips + marks > 0, '排不出来的那天也要有声（dempty），不能静默少一天');
});

/* ══════════════ 计划页：远期旅行 → 不猜温度，问人要 ══════════════ */

console.log('\n计划页 · 远期（预报只到 16 天内）：不许编温度，要问人要');

await render('plan.html', async (p, store, doc, t) => {
  await waitSub(store);                       // 先等 boot 那一轮排完，免得起跑线不一致
  store.get('#start').value = '2026-12-20';   // 两个多月后 —— 谁都查不到
  store.get('#ndays').value = '5';
  store.get('#sub').textContent = '';         // 复位，好判断这一轮什么时候结束

  await t.fire(store.get('#run'), null);
  const settled = await waitSub(store);
  check(settled, '点「排出来」之后有回应（不是点了没反应）');

  const h = store.get('#result').innerHTML;
  check(h.includes('那几天还没有预报'), '说清了"为什么没有温度"');
  check(h.includes('data-all='), '给了「全按 X℃」的快捷按钮');
  const sliders = (h.match(/type="range"/g) || []).length;
  check(sliders === 5, `按天数给温度滑杆（要 5 根，实际 ${sliders}）`);
  check(h.includes('id="pkgo"'), '有「按这个排」按钮，能把计划排下去');
  console.log(`    #sub="${store.get('#sub').textContent}"`);
});

/* ══════════════ 计划页：旅行「必带」══════════════ */

console.log('\n计划页 · 旅行「必带」：勾了就一定进箱子');

await render('plan.html', async (p, store, doc, t) => {
  await waitSub(store);

  // 切到旅行模式 —— 必带那张卡只在这里出现。
  // ⚠️ 桩里**元素级 querySelector 恒返回 null**，按钮又是事件委托在容器上的，
  // 所以只能"对着容器派发一个带 dataset 的假 target"（跟删除按钮同一套写法）。
  await t.fire(store.get('#modeseg'), t.fakeTarget({ mode: 'trip' }));
  // ⚠️ 必须等这一轮排完 —— 否则下一步点「必带」时上一轮还占着 busy，
  // 那一轮请求会被丢掉（页面里已经有 pending 兜住，但测试该等就等）
  await waitSub(store);
  check(store.get('#mustCard').style.display === '',
        '切到旅行模式后，「必带」那张卡显示出来');

  const pick = store.get('#mustPick');
  const chips = (pick.innerHTML.match(/data-must=/g) || []).length;
  console.log(`    可选单品 ${chips} 件`);
  check(chips >= 5, `把衣橱里的单品列出来当选项（${chips} 件）`);
  check(!/undefined/.test(pick.innerHTML), '选项里没有漏出 undefined');

  const m = /data-must="(\d+)"/.exec(pick.innerHTML);
  if (!m) {
    check(false, '至少有一个可点的「必带」选项');
    return;
  }
  const id = m[1];

  await t.fire(pick, t.fakeTarget({ must: id }, ''));
  const settled = await waitSub(store);
  check(settled, '勾一下就会重排（不是点了没反应）');
  check(store.get('#mustNow').textContent.includes('已选 1 件'),
        '卡片上说明了「已选 1 件」');

  const h = store.get('#result').innerHTML;
  console.log('    清单卡片片段：'
    + (h.match(/装进箱子[^<]*|必带[^<]*|备着/g) || []).join(' | '));
  check(h.includes('必带'), '清单里标出了「必带」');
  check(!/undefined/.test(h), '结果里没有漏出 undefined');

  // 再点一下取消 —— 不是单向开关
  await t.fire(pick, t.fakeTarget({ must: id }, ''));
  await waitSub(store);
  check(!store.get('#mustNow').textContent.includes('已选 1 件'),
        '再点一下能取消（不是单向开关）');

  await t.fire(store.get('#modeseg'), t.fakeTarget({ mode: 'week' }));
  await waitSub(store);
  check(store.get('#mustCard').style.display === 'none',
        '切回一周模式后卡片收起（这个功能是旅行专属）');
});

/* ══════════════ 计划页：旅行去两个地方（分段目的地）══════════════
   用户原话：「旅游那边如果我在两个不同的旅游地呢？」

   要钉住三件事：
     ① **一段都没选时，老路一步都不能变**（还是「排几天」+ 单个城市）
     ② 分段之后「排几天」必须收起来 —— 否则页面上同时有两个能改天数的地方
        （两个状态源必然打架，那是这个项目反复踩过的一类坑）
     ③ 发出去的 `segments` 日期必须**首尾相接**（后端只按日期拼，不猜日期） */

console.log('\n计划页 · 旅行去两个地方：一个箱子，两段天气');

await render('plan.html', async (p, store, doc, t) => {
  await waitSub(store);
  await t.fire(store.get('#modeseg'), t.fakeTarget({ mode: 'trip' }));
  await waitSub(store);

  const dayAfter = (iso, n) => {
    const d = new Date(iso + 'T12:00:00');
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  };

  // ① 一段都没有 —— 老路不许坏
  check(store.get('#ndaysBox').style.display !== 'none',
        '一段都没选时，「排几天」还是输入框（老路不许坏）');
  check(store.get('#cityGo').textContent === '搜', '按钮还是「搜」');
  check(store.get('#cityNow').innerHTML.includes('没选就按你常住城市'),
        '没选目的地时给出说明，不是空白');

  // ② 喂两段进去（**不靠真搜城市** —— 那样的断言会同时依赖网络和城市库）
  p.segs = [
    { label: '京都 · 京都府 · 日本', city: '京都', lat: 35.02107, lon: 135.75385, days: 3 },
    { label: '大阪市 · 大阪府 · 日本', city: '大阪市', lat: 34.69379, lon: 135.50107, days: 3 },
  ];
  p.renderMode();

  const cn = store.get('#cityNow').innerHTML;
  console.log(`    分段行：${(cn.match(/data-segdel=/g) || []).length} 行 · `
    + `共几天：${/共 \d+ 天/.exec(cn) ? /共 \d+ 天/.exec(cn)[0] : '没算出来'}`);
  check((cn.match(/data-segdel=/g) || []).length === 2, '两段各画了一行（带删除）');
  check(cn.includes('共 6 天'), '算出了总天数（3 + 3 = 6）');
  check((cn.match(/data-segdays=/g) || []).length === 2, '每段自己有天数选择器');
  check(cn.includes('10-') || /\d\d–\d\d/.test(cn), '每行写出了起止日期');
  check(!/undefined/.test(cn), '分段行里没有漏出 undefined');

  // ⚠️ 这次改动最容易出错的一条：两个状态源不许并存
  check(store.get('#ndaysBox').style.display === 'none',
        '分段之后「排几天」收起来（天数只归各段管，不许两个地方都能改）');
  check(store.get('#cityGo').textContent === '加一段',
        '搜城市那条变成「加一段」（说清再搜是追加、不是替换）');
  // 这处是 textContent，认不了 markdown —— 写了 `**` 会原样显示出来
  check(!String(store.get('#modehint').textContent).includes('**'),
        '模式说明里没有漏出 ** 星号（这处是 textContent）');

  // ③ 改某一段的天数 → 总天数与日期轴跟着动
  const sel = t.fakeTarget({ segdays: '0' }, '5 天');
  sel.value = '5';
  await t.fire(store.get('#cityNow'), sel, 'change');
  check(p.segs[0].days === 5, `改天数真生效了（第一段 → ${p.segs[0].days} 天）`);
  check(store.get('#cityNow').innerHTML.includes('共 8 天'),
        '总天数跟着变成 8（5 + 3）');

  // ④ 发出去的请求
  store.get('#sub').textContent = '';               // 复位，好判断这一轮什么时候结束
  await t.fire(store.get('#run'), null);
  const settled = await waitSub(store);
  check(settled, '点「排出来」之后有回应（不是点了没反应）');

  const body = (t.planBodies || []).filter(b => b && b.segments).pop();
  check(!!body, '请求里带了 segments（不是还按单个城市发）');
  if (body) {
    console.log('    发出：' + JSON.stringify(body.segments));
    check(body.segments.length === 2, `两段都发出去了（${body.segments.length}）`);
    check(body.n_days === 8, `总天数是两段之和（${body.n_days}）`);
    const [s1, s2] = body.segments;
    check(!!s1.start && !!s2.start, '每段都算好了从哪天开始');
    check(s2.start === dayAfter(s1.start, s1.days),
          `第二段紧接着第一段（${s1.start} + ${s1.days} 天 → ${s2.start}）`);
    check(s1.lat != null && s2.lat != null, '每段都带着坐标（不然后端搜不出城市）');
  }

  // ⑤ 结果页：能看出"哪几天在哪儿"
  const h = store.get('#result').innerHTML;
  check(h.includes('segchain'), '结果页画出了「去过哪几段」');
  check(h.includes('京都') && h.includes('大阪'), '两段的地名都在');
  const nMark = (h.match(/class="dayseg"/g) || []).length;
  console.log(`    段落小标题 ${nMark} 处（每段第一天各一处）`);
  check(nMark === 2, `段落小标题正好两处（每段第一天各一次，实际 ${nMark}）`);
  check(!/undefined/.test(h), '结果里没有漏出 undefined');
  // 后端提示里用 **…** 标重点。直接 esc 会把星号原样显示出来 ——
  // 既有的一处（"穿几层是按当天最冷那会儿算的"）就是这么漏的。
  check(!h.includes('**'), '结果里没有漏出未渲染的 ** 星号');
  check(h.includes('<b>箱子只有一份</b>'),
        '提示里的重点渲染成了粗体（两段共用一个箱子那句）');

  // ⑥ 删掉一段
  await t.fire(store.get('#cityNow'), t.fakeTarget({ segdel: '1' }, '×'));
  check(p.segs.length === 1, `删掉一段后只剩一段（${p.segs.length}）`);
  check((store.get('#cityNow').innerHTML.match(/data-segdel=/g) || []).length === 1,
        '分段行跟着少了一行（不是点了没反应）');

  // ⑦ 预报不全 → 手填温度那一屏：**日期轴必须是后端给的**
  //    （两段之间可能隔着几十天，按"从某天起连续 N 天"推会把第二段算错）
  p.openPicker({
    need_temps: true, n_days: 6, start: '2026-10-01',
    reason: '大阪市 · 大阪府 · 日本 落在预报之外 —— 天气预报最多只到 16 天内。',
    day_axis: ['2026-10-01', '2026-10-02', '2026-10-03',
               '2026-11-20', '2026-11-21', '2026-11-22'],
    segments: [
      { label: '京都 · 京都府 · 日本', days: 3, got: 3, from: '2026-10-01', to: '2026-10-03' },
      { label: '大阪市 · 大阪府 · 日本', days: 3, got: 0, from: '2026-11-20', to: '2026-11-22' },
    ],
    days: [
      { date: '2026-10-01', temp: 22, seg: '京都 · 京都府 · 日本' },
      { date: '2026-10-02', temp: 19, seg: '京都 · 京都府 · 日本' },
      { date: '2026-10-03', temp: 19, seg: '京都 · 京都府 · 日本' },
    ],
  });
  const ph = store.get('#result').innerHTML;
  const nSliders = (ph.match(/type="range"/g) || []).length;
  check(nSliders === 6, `按后端给的日期轴画 6 天（实际 ${nSliders} 根滑杆）`);
  check(ph.includes('11-20'),
        '第二段的日期是 11/20（不是按"连续 6 天"推出来的 10/04）');
  check(!ph.includes('10-04'), '没有出现推错的那个日期 10/04');
  check((ph.match(/class="pkseg"/g) || []).length === 2, '两段各有一个分组标题');
  check(ph.includes('大阪市 那几天还没有预报'), '标题点名说是哪一段没预报');
  check(ph.includes('先替你填好了'), '说清了有几天是查到的、已经先填好');
  check(!ph.includes('pkback'),
        '分段行程没有「只用有预报的那几天」（中间隔着空档，那个按钮没有意义）');
});

/* ══════════════ 套装页：自己搭一套 ══════════════ */

console.log('\n套装页 · 挑几件 → 实时判定 → 存下来');

await render('sets.html', async (p, store, doc, t) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(600);      // 等 loadItems + loadSets

  const chips = (store.get('#picker').innerHTML.match(/data-pick=/g) || []).length;
  console.log(`    可选单品 ${chips} 件`);
  check(chips >= 5, `把衣橱里的单品列出来当选项（${chips} 件）`);
  check(!/undefined/.test(store.get('#picker').innerHTML),
        '选项里没有漏出 undefined');
  check(store.get('#save').disabled === true,
        '一件没挑时「存下来」是禁用的（状态由代码算，不是只写在 HTML 上）');

  // 挑一套**真的能穿**的：内搭 + 下装 + 鞋各一件。
  // 不用"把池子里所有 id 都点上"——那样同一个槽位会重复，判定必然失败，
  // 于是存下来这条路一次都走不到（第一版就是这么写的，白测）。
  const owned = (await (await fetch(BASE + '/api/items?stage=owned')).json())
    .filter((it) => it.status === 'in_wardrobe');
  const wanted = ['L1', 'L4', 'L5']
    .map((s) => owned.find((it) => it.slot === s))
    .filter(Boolean);
  if (wanted.length < 3) {
    check(false, `衣橱里凑不出内搭+下装+鞋（只有 ${wanted.length} 类）`);
    return;
  }
  const ids = wanted.map((it) => it.id);
  console.log(`    挑：${wanted.map((it) => it.name).join(' ＋ ')}`);

  await t.fire(store.get('#picker'), t.fakeTarget({ pick: String(ids[0]) }, ''));
  await sleep(120);
  check(store.get('#seln').textContent.includes('1 件'), '已挑的件数跟着涨');
  check(store.get('#save').disabled === true,
        '只挑一件时仍然禁着（它会在判定里被拦）');

  for (const id of ids.slice(1)) {
    await t.fire(store.get('#picker'), t.fakeTarget({ pick: String(id) }, ''));
  }
  await sleep(1200);     // 220ms 防抖 + 一次真判定
  const vb = store.get('#vb');
  const verdict = vb.innerHTML.replace(/<[^>]+>/g, '').trim();
  console.log(`    判定：${verdict}`);
  check(/能穿|穿不了/.test(vb.innerHTML), '判定结果回来了（不是一直转圈）');

  const usable = store.get('#save').disabled === false;
  check(usable, `这套是合法的 → 「存下来」亮起来（判定说：${verdict}）`);

  // 再点一下取消选择 —— 不是单向开关（**要放在存之前**：
  // 存成功会把选择清空，之后再点就是"重新挑一件"而不是"取消"）
  await t.fire(store.get('#picker'), t.fakeTarget({ pick: String(ids[2]) }, ''));
  await sleep(200);
  check(store.get('#seln').textContent.includes('2 件'), '再点一下能取消（不是单向开关）');
  check(store.get('#save').disabled === true, '取消之后又禁用了');
  await t.fire(store.get('#picker'), t.fakeTarget({ pick: String(ids[2]) }, ''));
  await sleep(1200);
  check(store.get('#save').disabled === false, '点回来又能存了（不是单向开关）');

  if (usable) {
    await t.fire(store.get('#save'), t.fakeTarget({}));
    await sleep(400);
    const w = t.setWrites[0];
    console.log(`    写请求：${w ? w.method + ' ' + w.url : '（没发出去）'}`);
    check(!!w, '点了「存下来」真的发了请求（不是点了没反应）');
    if (w) {
      const body = JSON.parse(w.body);
      check(Array.isArray(body.item_ids) && body.item_ids.length === ids.length,
            `请求里带着挑好的 ${ids.length} 件`);
    }
    check(t.toasts.some((x) => x.includes('存好')), `有可见提示（${JSON.stringify(t.toasts)}）`);
    check(store.get('#save').disabled === true, '存完之后选择被清空，按钮回到禁用');
  }
});

console.log('\n套装页 · 同一个槽位挑两件，要说清为什么不行');

await render('sets.html', async (p, store, doc, t) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(600);
  const owned = (await (await fetch(BASE + '/api/items?stage=owned')).json())
    .filter((it) => it.status === 'in_wardrobe');
  const l1 = owned.filter((it) => it.slot === 'L1');
  const l4 = owned.filter((it) => it.slot === 'L4');
  if (l1.length < 2 || !l4.length) {
    check(true, '内搭不足两件，跳过');
    return;
  }
  for (const it of [l1[0], l1[1], l4[0]]) {
    await t.fire(store.get('#picker'), t.fakeTarget({ pick: String(it.id) }, ''));
  }
  await sleep(1200);
  const txt = store.get('#vb').innerHTML.replace(/<[^>]+>/g, '').trim();
  console.log(`    判定：${txt}`);
  check(/槽位/.test(txt), `说清是同一个槽位重复了，而不是含糊的"不合法"`);
  check(store.get('#save').disabled === true, '这种也存不下来');
});

console.log('\n套装页 · 删一套要两步确认，且失败看得见');

await render('sets.html', async (p, store, doc, t) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(600);
  const list = store.get('#list');
  const hasSet = /data-del="(\d+)"/.exec(list.innerHTML);
  if (!hasSet) {
    console.log('    （现在一套都没有，跳过删除断言）');
    check(true, '没有套装时不渲染删除按钮（不做无意义的断言）');
    return;
  }
  const sid = hasSet[1];
  const btn = t.fakeTarget({ del: sid }, '删除');
  await t.fire(list, btn);
  check(t.deletes.length === 0, '第一次点只是"待确认"，不许真的删');
  check(String(btn.textContent).includes('再点'), `按钮进入待确认（"${btn.textContent}"）`);
  await t.fire(list, btn);
  await sleep(300);
  check(t.deletes.length === 1 && t.deletes[0].includes('/api/sets/' + sid),
        `第二次点才真删：${t.deletes[0] || '（没发）'}`);
  check(t.toasts.length > 0, `删完有可见提示（${JSON.stringify(t.toasts)}）`);
});

console.log();
if (fails.length) {
  console.log(`结论：${fails.length} 项未通过`);
  fails.forEach((f) => console.log(`  ✗ ${f}`));
  process.exit(1);
}
console.log('结论：全部通过');
