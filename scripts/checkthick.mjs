#!/usr/bin/env node
/* ══════════════════════════════════════════════════════════════════════
   厚度「说人话」自检（scripts/checkthick.mjs）

   为什么单独有这个检查：用户对厚度的原话是
     "关于衣服的厚度或者温度，我其实没有办法判断。能不能简单地用'厚'、'薄'、
      '中等'这种比较容易理解的词来写，剩下就交给系统或者大模型来判断，
      或者用一些比较直觉性的提示来让大家知道应该是什么样子的厚度。"

   于是 1–5 只活在接口里，界面上出现的一律是**词** + 一句"像什么"。
   而它的**失效方式全是静默的**，而且不止一种：

     · 词按钮点了，label 被老代码的 `b.textContent = el.value` 盖回数字；
     · 词按钮点了，界面变了、**数据库没变** —— 因为已入库面板是挂在
       `change` 上的（`patchField`），而遥控器只派发了 `input`。
       这一条**只在真浏览器里点一下、再去接口读一次**才看得见，
       单元测试和字符串断言都测不出来（本地踩过）。
     · 词表被抄成第二份，于是"录入页说厚、试衣间说很厚"。

   所以本文件**不开桩、不开 stub DOM** —— 它打真实服务、开真实浏览器、
   真派发点击，并且点完必须去接口把它读回来。

   本文件钉住的不变量（改了任何一条都会红）：
     A. 「改字段」面板里：5 个词按钮、词序正确、range 仍在场但是**藏起来的**
        （它是唯一状态源，不是给人看的）、初始 label 已经是**词**不是数字
     B. 点「很厚」→ range=5、选中标记只有一个、label 是「很厚」**不是 5**、
        提示换成"羽绒服/厚大衣"那句、摘要行里是"厚度 很厚"而不是"厚度 5"
     C. **B 之后真的落库了**（GET /api/items 读回 thickness === 5）——
        这条是整份文件存在的理由：少派发 `change` 时，只有它会红
     D. 重绘之后仍然是词、仍然选中同一格、摘要行也跟着变（不是"渲染一次就退回数字"）
     E. 点「很薄」→ label 是「很薄」**不是 1**，提示换成"吊带/雪纺"那句，也落库
     F. 点词不会把这一排挤动（整排宽高不变、5 个格子等宽）——
        这个项目为此栽过两次，见 README「点了会换文案的按钮，宽度必须钉死」
     G. 试衣间（prebuy）是**同一套词、同一套提示**，摘要同样是词
     H. 待入库卡（上传队列，用户"录进去时厚度不对"的那一屏）也是同一套词，
        而且撤卡之后**不留残留文件**（快照 diff 必须为空）
     I. 词表全仓库**只有一处定义**（`web/js/api.js` 的 `THICK_WORDS`）
     J. 收尾把动过的厚度**精确还原**并读回确认

   用法：node scripts/checkthick.mjs [--out scripts/checkthick.out.txt]
   前置：服务在 8077 跑着（scripts/serve.py）
   依赖：本机 Playwright 的 chrome-headless-shell（找不到会明确报错，不假装通过）
   ⚠️ 本检查会**真的改一件衣服的厚度**（为了验落库），收尾精确还原原值并读回确认。
   ══════════════════════════════════════════════════════════════════════ */
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir, networkInterfaces } from 'node:os';
import { setTimeout as sleep } from 'node:timers/promises';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = join(HERE, '..', 'web');
const PORT = Number(process.env.CDP_PORT || 9447);
const OUT = (() => {
  const i = process.argv.indexOf('--out');
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : null;
})();
const WORDS = ['很薄', '薄', '中等', '厚', '很厚'];

/* ── 目标地址：必须是"真的在跑本应用"的那个 ──
   ⚠️ 不写死 127.0.0.1：Windows 允许 0.0.0.0:8077（本应用）和
   127.0.0.1:8077（**另一个程序**）同时监听，写死回环会去测别人家的服务。 */
async function pingApp(base) {
  try {
    const r = await fetch(base + '/api/ping', { cache: 'no-store' });
    return (await r.json()).app === 'capsule-wardrobe';
  } catch { return false; }
}
function candidateBases() {
  const out = ['http://127.0.0.1:8077'];
  for (const addrs of Object.values(networkInterfaces())) {
    for (const a of addrs || []) {
      if (a.family !== 'IPv4' || a.internal) continue;
      if (a.address.startsWith('198.18.') || a.address.startsWith('198.19.')) continue;
      if (a.address.startsWith('169.254.')) continue;
      out.push(`http://${a.address}:8077`);
    }
  }
  return out;
}
async function resolveBase() {
  if (process.env.BASE) {
    if (!(await pingApp(process.env.BASE))) {
      throw new Error(`BASE=${process.env.BASE} 上不是本应用（/api/ping 没认出来）`);
    }
    return process.env.BASE;
  }
  const cands = candidateBases();
  for (const b of cands) if (await pingApp(b)) return b;
  throw new Error(
    `8077 上找不到本应用。试过这些地址：\n    ${cands.join('\n    ')}\n` +
    `  · 服务没起？双击 start-lan.bat\n` +
    `  · 回环被别的程序占着？用 BASE=http://<本机IP>:8077 指定一个`);
}

function findShell() {
  if (process.env.CHROME_SHELL && existsSync(process.env.CHROME_SHELL)) return process.env.CHROME_SHELL;
  const bases = [process.env.LOCALAPPDATA, join(process.env.USERPROFILE || '', 'AppData', 'Local')].filter(Boolean);
  for (const b of bases) {
    const root = join(b, 'ms-playwright');
    if (!existsSync(root)) continue;
    let dirs = [];
    try { dirs = readdirSync(root); } catch { continue; }
    for (const d of dirs) {
      if (!d.startsWith('chromium_headless_shell') && !d.startsWith('chromium-')) continue;
      for (const sub of ['chrome-headless-shell-win64', 'chrome-win64', 'chrome-win']) {
        for (const exe of ['chrome-headless-shell.exe', 'chrome.exe']) {
          const p = join(root, d, sub, exe);
          if (existsSync(p)) return p;
        }
      }
    }
  }
  return null;
}

const lines = [];
const say = (...a) => { const s = a.join(' '); lines.push(s); console.log(s); };
let fails = 0;
function check(label, ok, detail = '') {
  say(`  ${ok ? '✓' : '✗'} ${label}${detail ? '   ' + detail : ''}`);
  if (!ok) fails++;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const shell = findShell();
if (!shell) {
  const fatal = ['找不到 chrome-headless-shell。',
    '装一个：npx playwright install chromium',
    '或者用 CHROME_SHELL=<路径> 指定。'];
  console.error(fatal.join('\n'));
  if (OUT) writeFileSync(OUT, `# checkthick · ${new Date().toISOString()}\n\n` + fatal.join('\n') + '\n', 'utf-8');
  process.exit(2);
}

let BASE;
try { BASE = await resolveBase(); }
catch (e) {
  console.error(String(e.message || e));
  if (OUT) writeFileSync(OUT, `# checkthick · ${new Date().toISOString()}\n\n` + String(e.message || e) + '\n', 'utf-8');
  process.exit(2);
}

const profile = join(tmpdir(), `capsule-thick-${Date.now()}`);
mkdirSync(profile, { recursive: true });

// ⚠️ 三条硬规矩（都踩过）：
//   ① 必须给 --user-data-dir，不给调试端口根本不起来
//   ② 启动参数里**不能带目标 URL**，也不能加 --virtual-time-budget ——
//      两者都会让 headless shell 在拿到调试目标之前自己退出
//   ③ 等待用显式 sleep / 轮询
const proc = spawn(shell, [
  '--headless', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--window-size=390,844', 'about:blank',
], { stdio: 'ignore' });

async function findTarget() {
  const tried = ['127.0.0.1', '[::1]'];
  for (let i = 0; i < 50; i++) {
    for (const h of tried) {
      try {
        const r = await fetch(`http://${h}:${PORT}/json/list`);
        // ⚠️ 必须取**页面** target，不能取 /json/version 的浏览器级端点。
        const t = (await r.json()).find((x) => x.type === 'page');
        if (t && t.webSocketDebuggerUrl) return t.webSocketDebuggerUrl;
      } catch { /* 端口还没起来 */ }
    }
    await sleep(300);
  }
  throw new Error(`拿不到 CDP 调试目标（端口 ${PORT}）—— ${tried.join(' 和 ')} 都试过了`);
}

let exitCode = 0;
try {
  const wsUrl = await findTarget();
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  let seq = 0;
  const waiting = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  };
  const send = (method, params = {}) => new Promise((res) => {
    const id = ++seq;
    waiting.set(id, res);
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (waiting.has(id)) { waiting.delete(id); res({ __timeout: 1 }); } }, 20000);
  });

  await send('Network.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Page.enable');
  await send('Runtime.enable');

  async function evalJs(expr) {
    const r = await send('Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.__timeout) throw new Error('evaluate 超时');
    const ex = r.result?.exceptionDetails;
    if (ex) throw new Error(ex.exception?.description || JSON.stringify(ex));
    return r.result?.result?.value;
  }
  async function goto(path, wait = 3500) {
    await send('Page.navigate', { url: BASE + path });
    await sleep(wait);
  }
  async function waitForJs(expr, ms = 20000, step = 300) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await evalJs(expr)) return true;
      await sleep(step);
    }
    return false;
  }
  const qs = (sel) => `document.querySelector(${JSON.stringify(sel)})`;
  const qsa = (sel) => `[...document.querySelectorAll(${JSON.stringify(sel)})]`;
  const txt = (sel) => evalJs(`(${qs(sel)} ? ${qs(sel)}.textContent.trim() : null)`);
  const click = (sel) => evalJs(`(() => { const e = ${qs(sel)}; if (!e) return false; e.click(); return true; })()`);
  const itemThickness = (id) => evalJs(
    `fetch('/api/items?stage=owned', {cache:'no-store'}).then(r => r.json())`
    + `.then(a => { const x = a.find(v => v.id === ${id}); return x ? x.thickness : null; })`);
  const patchThickness = (id, v) => evalJs(
    `fetch('/api/items/${id}', {method:'PATCH', headers:{'Content-Type':'application/json'},`
    + ` body: JSON.stringify({thickness:${v}})}).then(r => r.status)`);

  say(`厚度「说人话」自检  ·  ${new Date().toISOString()}`);
  say(`目标 ${BASE}   ·   浏览器 ${shell.split(/[\\/]/).pop()}`);
  say();

  /* ── 先拿一件真实的已入库单品（要验落库，就必须有一件真的） ──
     ⚠️ 必须先 `goto` 一个页面再 fetch：CDP 目标刚才是 `about:blank`，
     相对路径的 fetch 在它上面直接抛 "Failed to parse URL"。 */
  await goto('/wardrobe');
  const all = JSON.parse(await evalJs(
    `fetch('/api/items?stage=owned', {cache:'no-store'}).then(r => r.text())`));
  if (!all.length) {
    check('衣橱里有已入库单品（没有就没法验"落库"）', false, 'stage=owned 是空的');
    say('\n✗ 没有已入库单品 —— 这一条**不许静默跳过**（跳过就是假绿）。');
    exitCode = 1;
    throw { __done: true };
  }
  const IT = all[0];
  const ID = IT.id;
  const ORIG = IT.thickness;
  say(`拿到的样本：#${ID}「${IT.name}」原厚度 ${ORIG}`);
  say();

  /* ══════════════ A. 已入库「改字段」面板 · 控件形态 ══════════════ */
  say('A · 已入库「改字段」面板里的厚度控件');

  const hasRow = await waitForJs(`${qs('[data-open]')} !== null`, 15000);
  check('录入页列出已入库单品', hasRow);
  if (!hasRow) { exitCode = 1; throw { __done: true }; }

  await click(`[data-open="${ID}"]`);
  const opened = await waitForJs(`${qs('.thickbox')} !== null`, 8000);
  check('「改字段」掀开面板，里面有 .thickbox', opened);
  if (!opened) { exitCode = 1; throw { __done: true }; }

  const words = await evalJs(`${qsa('.thickbox [data-thick]')}.map(x => x.textContent.trim())`);
  check('5 个词按钮，顺序正确', same(words, WORDS), `实际 ${JSON.stringify(words)}`);

  const rawSel = `input[data-ef="thickness"]`;
  const rawInfo = await evalJs(`(() => {
      const e = ${qs(rawSel)};
      if (!e) return null;
      const cs = getComputedStyle(e);
      const r = e.getBoundingClientRect();
      return { type: e.type, value: e.value, min: e.min, max: e.max,
               opacity: cs.opacity, w: Math.round(r.width), h: Math.round(r.height),
               visible: e.offsetParent !== null && cs.display !== 'none' && cs.visibility !== 'hidden' };
    })()`);
  check('状态源（range）仍然在场，还是 1–5', !!rawInfo && rawInfo.type === 'range'
    && rawInfo.min === '1' && rawInfo.max === '5');
  // 藏起来是**故意的**：一个没有刻度的滑杆等于什么都没给（用户原话）。
  // 这条红了不代表坏 —— 代表有人把它"修"回了可见，得先想清楚为什么。
  check('滑杆是藏起来的（它是状态源，不是给人看的）',
    !!rawInfo && rawInfo.opacity === '0' && Number(rawInfo.value) >= 1,
    rawInfo ? `opacity=${rawInfo.opacity} 尺寸=${rawInfo.w}×${rawInfo.h}` : '');

  const v0 = Number(rawInfo.value);
  check('初始 label 就是**词**不是数字',
    (await txt('.thickbox label b')) === WORDS[v0 - 1],
    `range=${v0} → label=${await txt('.thickbox label b')}`);
  const on0 = await evalJs(`${qsa('.thickbox [data-thick].on')}.map(x => x.textContent.trim())`);
  check('初始选中格与 range 一致，且只有一格',
    same(on0, [WORDS[v0 - 1]]), `选中 ${JSON.stringify(on0)}`);
  const hint0 = await txt('.thickbox .thickhint');
  check('有一句"像什么"的直觉提示', !!hint0 && hint0.length >= 8, `"${hint0}"`);
  // 摘要行在**列表那一行**上（`.meta`，由 api.js 的 itemMeta() 拼），不在面板里
  const rowMeta0 = await txt(`#list .item-row[data-id="${ID}"] .meta`);
  check('摘要行里的厚度也是词（"厚度薄"，不是"厚度2"）',
    /厚度[很薄中厚]/.test(rowMeta0 || '') && !/厚度\s*\d/.test(rowMeta0 || ''),
    `"${rowMeta0}"`);
  say();

  /* ══════════════ B/C/D. 点「很厚」→ 界面变 + 落库 + 重绘后仍是词 ══════════════ */
  say('B · 点「很厚」');

  // 点词前后量一次这一排的几何 —— 这个项目栽过"点一下按钮变宽把邻居挤走"
  const geoBefore = await evalJs(`(() => {
      const p = ${qs('.thickpick')};
      const r = p.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height),
               cells: [...p.children].map(c => Math.round(c.getBoundingClientRect().width)) };
    })()`);

  check('点得到「很厚」', await click('.thickbox [data-thick="5"]'));
  await sleep(300);
  check('range 变成 5', (await evalJs(`${qs(rawSel)}.value`)) === '5');
  check('label 显示「很厚」，**不是 5**', (await txt('.thickbox label b')) === '很厚',
    `label="${await txt('.thickbox label b')}"`);
  const on5 = await evalJs(`${qsa('.thickbox [data-thick].on')}.map(x => x.textContent.trim())`);
  check('选中标记移到「很厚」，且仍只有一格', same(on5, ['很厚']), JSON.stringify(on5));
  const hint5 = await txt('.thickbox .thickhint');
  check('提示换成"羽绒服/厚大衣"那句', /羽绒|大衣/.test(hint5 || ''), `"${hint5}"`);

  const geoAfter = await evalJs(`(() => {
      const p = ${qs('.thickpick')};
      const r = p.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height),
               cells: [...p.children].map(c => Math.round(c.getBoundingClientRect().width)) };
    })()`);
  const cellSpread = Math.max(...geoAfter.cells) - Math.min(...geoAfter.cells);
  check('点词没把这一排挤动（整排宽高不变）',
    geoAfter.w === geoBefore.w && geoAfter.h === geoBefore.h,
    `${geoBefore.w}×${geoBefore.h} → ${geoAfter.w}×${geoAfter.h}`);
  check('5 个格子等宽（不是"很薄"比"薄"窄）', cellSpread <= 1,
    `最宽-最窄 = ${cellSpread}px  ${JSON.stringify(geoAfter.cells)}`);

  // ★ 整份文件存在的理由：界面变了，**数据库变了吗**？
  let stored = null;
  for (let i = 0; i < 20; i++) {
    stored = await itemThickness(ID);
    if (Number(stored) === 5) break;
    await sleep(400);
  }
  check('★ 真的落库了（GET /api/items 读回 thickness === 5）',
    Number(stored) === 5, `读回 ${stored}（原值 ${ORIG}）`);
  const toastTxt = await txt('.toast') || await evalJs(
    `(() => { const t = document.querySelector('.toast, #toast');
        return t ? t.textContent.trim() : ''; })()`);
  if (toastTxt) say(`    提示条："${toastTxt}"`);

  // D. 落库后 renderList() 会整块重绘 —— 重绘之后还得是词、还得选中同一格
  await sleep(600);
  const lblAfterRedraw = await txt('.thickbox label b');
  const onAfterRedraw = await evalJs(`${qsa('.thickbox [data-thick].on')}.map(x => x.textContent.trim())`);
  check('重绘之后 label 仍然是词（渲染一次不会退回数字）',
    lblAfterRedraw === '很厚', `"${lblAfterRedraw}"`);
  check('重绘之后仍然选中「很厚」', same(onAfterRedraw, ['很厚']), JSON.stringify(onAfterRedraw));
  const rowMeta5 = await txt(`#list .item-row[data-id="${ID}"] .meta`);
  check('重绘之后摘要行也跟着变成「厚度很厚」', /厚度很厚/.test(rowMeta5 || ''), `"${rowMeta5}"`);
  say();

  /* ══════════════ E. 反向再钉一次：「很薄」不能显示成 1 ══════════════ */
  say('E · 点「很薄」（反向验一次，防"只有 5 那档是特例"）');
  check('点得到「很薄」', await click('.thickbox [data-thick="1"]'));
  await sleep(300);
  check('label 显示「很薄」，**不是 1**', (await txt('.thickbox label b')) === '很薄',
    `label="${await txt('.thickbox label b')}"`);
  const hint1 = await txt('.thickbox .thickhint');
  check('提示换成"吊带/雪纺"那句', /吊带|雪纺/.test(hint1 || ''), `"${hint1}"`);
  stored = null;
  for (let i = 0; i < 20; i++) {
    stored = await itemThickness(ID);
    if (Number(stored) === 1) break;
    await sleep(400);
  }
  check('★ 也真的落库了（读回 thickness === 1）', Number(stored) === 1, `读回 ${stored}`);
  say();

  /* ══════════════ F. 试衣间：同一套词、同一套提示 ══════════════ */
  say('F · 试衣间（prebuy）用的是同一套词');
  const candBefore = JSON.parse(await evalJs(
    `fetch('/api/items?stage=candidate', {cache:'no-store'}).then(r => r.text())`)).length;

  await goto('/prebuy');
  check('有「手动填一件」这个入口', await click('#manualLink'));
  await waitForJs(`${qs('[data-act="more"]')} !== null`, 8000);

  // ⚠️ 本轮新增的回归护栏：厚度控件**必须常驻可见**，不许再退回"藏在「改字段」里"。
  //    藏起来的那版是真出过事的：模型把一件厚外套估成「中等」→ 保暖值算低 →
  //    搭不上现有衣服 → 判「建议不买」，而用户在页面上**找不到任何地方能改回来**。
  check('还没展开「改字段」，.thickbox 就已经在场（常驻可见）',
    await evalJs(`${qs('#form .thickbox')} !== null`));

  check('有「改字段」这个开关', await click('[data-act="more"]'));
  const pbOpen = await waitForJs(`${qs('#form .thickbox')} !== null`, 8000);
  check('展开后出现 .thickbox', pbOpen);
  if (pbOpen) {
    const pbWords = await evalJs(`${qsa('#form .thickbox [data-thick]')}.map(x => x.textContent.trim())`);
    check('试衣间的 5 个词与录入页**完全一致**（没有第二份词表）',
      same(pbWords, WORDS), `实际 ${JSON.stringify(pbWords)}`);
    const pbDefault = await evalJs(`${qs('#form input[data-d="thickness"]')}.value`);
    check('默认档（blank() 给的是 3）显示成「中等」',
      (await txt('#form .thickbox label b')) === '中等', `range=${pbDefault}`);

    check('点得到「厚」', await click('#form .thickbox [data-thick="4"]'));
    await sleep(250);
    check('label 显示「厚」，**不是 4**', (await txt('#form .thickbox label b')) === '厚',
      `label="${await txt('#form .thickbox label b')}"`);
    check('range 变成 4', (await evalJs(`${qs('#form input[data-d="thickness"]')}.value`)) === '4');
    const pbHint = await txt('#form .thickbox .thickhint');
    check('提示是"卫衣/皮衣"那句', /卫衣|皮衣/.test(pbHint || ''), `"${pbHint}"`);
    const pbMeta = await txt('#form .gc-meta');
    check('试衣间摘要里也是词（"厚度 厚"），不是数字',
      /厚度\s*厚/.test(pbMeta || '') && !/厚度\s*4/.test(pbMeta || ''),
      `"${(pbMeta || '').slice(0, 70)}"`);
  }
  const candAfter = JSON.parse(await evalJs(
    `fetch('/api/items?stage=candidate', {cache:'no-store'}).then(r => r.text())`)).length;
  check('只点词、没点保存 → 没有凭空多出候选件', candAfter === candBefore,
    `${candBefore} → ${candAfter}`);
  say();

  /* ══════════════ F2. 试衣间：每个选项选完必须"留住" ══════════════
     用户报的现象：「录新衣的时候有一些选项可以选，选了之后它不会变成那个选项，
     还是会跳回默认选项」。
     ⚠️ 这种事**读代码看不出来**：要看的是"真点一下之后，DOM 里和 `draft` 里
     是不是同一个值"，而且**中间必须触发一次重绘**（改主色会整块重渲染）——
     跳回默认值恰恰只在重绘之后才显形。 */
  say('F2 · 试衣间：每个选项选完要留在原地（含重绘之后）');

  await waitForJs(`${qs('#form select[data-d="collar"]')} !== null`, 8000);

  const SEL_FIELDS = ['collar', 'sleeve', 'length_band', 'fit', 'material', 'pattern', 'is_neutral'];
  const stuck = [];
  const skipped = [];
  for (const f of SEL_FIELDS) {
    const sel = `#form select[data-d="${f}"]`;
    const nOp = await evalJs(`(${qs(sel)} ? ${qs(sel)}.options.length : 0)`);
    if (!nOp) { skipped.push(`${f}(控件不在)`); continue; }
    // 挑一个**和当前不同**的选项，这样"没变"才是真的没变
    const idx = await evalJs(`(() => {
      const s = ${qs(sel)};
      for (let i = 0; i < s.options.length; i++) if (i !== s.selectedIndex) return i;
      return -1; })()`);
    if (idx < 0) { skipped.push(`${f}(只有一项)`); continue; }
    const want = await evalJs(`(() => {
      const s = ${qs(sel)};
      s.selectedIndex = ${idx};
      s.dispatchEvent(new Event('input', { bubbles: true }));
      s.dispatchEvent(new Event('change', { bubbles: true }));
      return s.options[${idx}].value; })()`);
    await sleep(180);
    const domVal = await evalJs(`(${qs(sel)} ? ${qs(sel)}.value : null)`);
    const draftVal = await evalJs(`(typeof draft !== 'undefined' && draft) ? draft[${JSON.stringify(f)}] : '__NO_DRAFT__'`);
    if (String(domVal) !== String(want)) stuck.push(`${f}: 选「${want}」→ DOM 变回「${domVal}」`);
    else if (String(draftVal) !== String(want)) stuck.push(`${f}: 选「${want}」→ DOM 对但 draft 是「${draftVal}」`);
  }
  check(`一个个选过去都留住（${SEL_FIELDS.length - skipped.length} 项）`,
    stuck.length === 0, stuck.length ? stuck.join('；') : (skipped.length ? `跳过 ${skipped.join('/')}` : ''));

  /* 关键一步：**先选一个非默认值 → 再改主色触发整块重绘 → 看它还在不在**。
     单独"选完就读"可能过，重绘才是真正的照妖镜。 */
  const keeper = await evalJs(`(() => {
    const s = ${qs('#form select[data-d="collar"]')};
    if (!s) return null;
    for (let i = 0; i < s.options.length; i++) {
      if (s.options[i].value && i !== s.selectedIndex) {
        s.selectedIndex = i;
        s.dispatchEvent(new Event('input', { bubbles: true }));
        s.dispatchEvent(new Event('change', { bubbles: true }));
        return s.options[i].value;
      }
    }
    return null; })()`);
  if (keeper) {
    // 改主色 → `renderForm()` 整块重渲染（这是最可能把用户选择冲掉的那条路径）
    await evalJs(`(() => {
      const s = ${qs('#form select[data-d="color_main"]')};
      if (!s) return;
      const i = s.selectedIndex === 0 ? 1 : 0;
      s.selectedIndex = i;
      s.dispatchEvent(new Event('input', { bubbles: true }));
      s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
    await sleep(400);
    const after = await evalJs(`(${qs('#form select[data-d="collar"]')} ? ${qs('#form select[data-d="collar"]')}.value : null)`);
    check(`改主色触发重绘之后，领型仍是「${keeper}」（没被冲回默认）`,
      String(after) === String(keeper), `现在 "${after}"`);
  }
  say();

  /* ══════════════ F3. 识别结果落定时，不许冲掉用户已经手选的值 ══════════════
     用户报的现象：「选了之后它不会变成那个选项，就还是会跳回默认选项」。
     真凶不是重绘，是 **`recognize()` 里的 `Object.assign(draft, r, keep)`**：
     模型（拍照后要跑几秒）返回的那一刻，会把用户在这几秒里手选的字段**整片盖掉** ——
     而 `keep` 里只有颜色和图片，没有领型/材质/图案/厚度。

     ⚠️ 这条**必须打桩**：真调模型要联网、要花钱、还要等，而且"谁先谁后"没法稳定复现。
     把 `API.post` 换掉，就能精确复现"用户的动作先、模型的结果后"这个顺序。 */
  say('F3 · 识别结果落定时不许冲掉用户手选的值');

  const staged = await evalJs(`(() => {
    // 造一个"用户已经手选了几项"的草稿
    draft = blank();
    draft.uid = 'probe-uid';
    draft._picked = true; draft._open = true;
    const picks = { collar: 'V领', material: '羊毛', pattern: '条纹' };
    for (const k in picks) draft[k] = picks[k];
    if (draft._touched && draft._touched.add) for (const k in picks) draft._touched.add(k);
    draft.thickness = 5;
    if (draft._touched && draft._touched.add) draft._touched.add('thickness');
    renderForm();
    // 打桩：模型会返回一套**和用户选择不同**的值
    window.__origPost = API.post;
    API.post = async () => ({ enabled: true, results: { c: {
      slot: 'L1', category: 'T恤', name: '模型给的名字',
      formality: 2, thickness: 2, temp_min: 10, temp_max: 30,
      collar: '圆领', sleeve: '短袖', material: '棉', pattern: '纯色',
      length_band: '常规', fit: '合身', }, } });
    return picks; })()`);

  await evalJs(`(async () => { await recognize(); })()`);
  await sleep(400);
  const kept = await evalJs(`({ collar: draft.collar, material: draft.material,
    pattern: draft.pattern, thickness: draft.thickness })`);
  await evalJs(`(() => { API.post = window.__origPost; })()`);

  const clobbered = Object.keys(staged).filter(k => String(kept[k]) !== String(staged[k]))
    .concat(String(kept.thickness) === '5' ? [] : ['thickness']);
  check('模型返回之后，用户手选的 领型/材质/图案/厚度 一个都没被盖掉',
    clobbered.length === 0,
    clobbered.length
      ? clobbered.map(k => `${k}: 手选「${staged[k] ?? 5}」→ 被冲成「${kept[k]}」`).join('；')
      : JSON.stringify(kept));
  // 反向：模型**该**填的（用户没碰过的）仍然要被填上，别修成"识别直接失效"
  check('用户没碰过的字段仍然采纳模型的值（formality 2）',
    String(await evalJs(`draft.formality`)) === '2',
    `formality=${await evalJs(`draft.formality`)}`);
  await evalJs(`(() => { draft = null; renderForm(); })()`);
  say();

  /* ══════════════ I. 待入库卡（上传队列）—— 用户"录进去时厚度不对"那一屏 ══════════════ */
  say('I · 待入库卡（上传队列）里的厚度');
  // 用机器上已有的一张图当"刚拍的照片"：在页面里 fetch 成 Blob → 造 File → 塞进 #file。
  // ⚠️ 不往磁盘塞任何测试素材；这一步会真的走一次识别接口（配置是开的）。
  // ⚠️ 必须**等识别落定再点词**：识别结果是用 `Object.assign(c, r, keep)` 写回去的，
  //    而 thickness **不在** keep/_touched 保护名单里 —— 先点后落定会被冲掉。
  //    这也正是真实用户顺序（先拍、等它认完、再纠错）。
  const IMG_DIR = join(HERE, '..', 'data', 'images');
  const imgBefore = new Set(existsSync(IMG_DIR) ? readdirSync(IMG_DIR) : []);
  let newUid = null;

  await goto('/wardrobe');
  const injected = await evalJs(`(async () => {
      const r = await fetch(${JSON.stringify(IT.img_raw)} + '?cb=' + Date.now(), {cache:'no-store'});
      const b = await r.blob();
      const f = new File([b], 'probe.jpg', {type: 'image/jpeg'});
      const dt = new DataTransfer(); dt.items.add(f);
      const inp = document.querySelector('#file');
      if (!inp) return 'no-input';
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return 'ok';
    })()`);
  check('能把一张图塞进上传入口', injected === 'ok', String(injected));
  const gotCard = await waitForJs(`${qs('.gcard')} !== null`, 30000);
  check('出现待入库卡', gotCard);

  if (gotCard) {
    const settled = await waitForJs(
      `(() => { const g = ${qs('.gcard')}; return !!g && !g.innerHTML.includes('识别中'); })()`,
      120000, 700);
    say(`    （识别${settled ? '已落定' : '**未在 120s 内落定** —— 下面按"手填卡"继续，网络问题不该让本检查变红'}）`);

    check('gcard 里有「改字段」开关', await click('.gcard [data-act="more"]'));
    const ubOpen = await waitForJs(`${qs('.gcard .thickbox')} !== null`, 8000);
    check('展开后出现 .thickbox', ubOpen);
    if (ubOpen) {
      const ubWords = await evalJs(`${qsa('.gcard .thickbox [data-thick]')}.map(x => x.textContent.trim())`);
      check('待入库卡的 5 个词与别处一致',
        same(ubWords, WORDS), `实际 ${JSON.stringify(ubWords)}`);
      const ubV = Number(await evalJs(`${qs('.gcard input[data-f="thickness"]')}.value`));
      check('range 落在 1–5（识别给的或品类默认都给合法值）',
        ubV >= 1 && ubV <= 5, `value=${ubV}`);
      check('label 是词，不是数字',
        (await txt('.gcard .thickbox label b')) === WORDS[ubV - 1],
        `range=${ubV} → label="${await txt('.gcard .thickbox label b')}"`);

      check('点得到「很厚」', await click('.gcard .thickbox [data-thick="5"]'));
      await sleep(250);
      check('待入库卡：range=5', (await evalJs(`${qs('.gcard input[data-f="thickness"]')}.value`)) === '5');
      check('待入库卡：label 显示「很厚」，**不是 5**',
        (await txt('.gcard .thickbox label b')) === '很厚',
        `label="${await txt('.gcard .thickbox label b')}"`);
      const ubMeta = await txt('.gcard .gc-meta');
      check('待入库卡：摘要行是「厚度 很厚」不是数字',
        /厚度\s*很厚/.test(ubMeta || ''), `"${(ubMeta || '').slice(0, 70)}"`);
      // 再点一次「很薄」：两档都验，"只有 5 那档对"是常见的假通过
      await click('.gcard .thickbox [data-thick="1"]');
      await sleep(250);
      check('待入库卡：切到「很薄」也显示词',
        (await txt('.gcard .thickbox label b')) === '很薄');
      const ubMeta1 = await txt('.gcard .gc-meta');
      check('待入库卡：摘要行跟着变「厚度 很薄」',
        /厚度\s*很薄/.test(ubMeta1 || ''), `"${(ubMeta1 || '').slice(0, 70)}"`);

      newUid = await evalJs(`(() => {
          const im = ${qs('.gcard img')};
          const m = im && im.getAttribute('src') && im.getAttribute('src').match(/([0-9a-f]{12})/);
          return m ? m[1] : null;
        })()`);
    }
    // 不点「全部入库」—— 那会把一张测试图变成衣柜里的一件真衣服。
    // 撤掉这张卡，然后把它这次上传落下的三张图删掉。
    await click('.gcard [data-act="del"]');
    await sleep(300);
    check('测试卡已撤掉（没有把测试图变成真衣服）',
      (await evalJs(`${qs('.gcard')} !== null`)) === false);
  }

  // 收尾：这次上传落下的图（快照 diff ∩ uid），精确删三个文件，不动别的
  if (existsSync(IMG_DIR)) {
    const imgAfter = readdirSync(IMG_DIR);
    const fresh = imgAfter.filter((f) => !imgBefore.has(f));
    const mine = newUid ? fresh.filter((f) => f.startsWith(newUid)) : fresh;
    // ⚠️ 不许 `catch {}` 空吞 —— 上一次就是被吞掉原因后只报"删了 3 个、剩 3 个"，
    //    看起来像"没删全"，其实三个都没删掉，真因（EPERM/EBUSY）一个字都没留下。
    //    Windows 上刚被别的进程读过的文件常有一小段占用期 → 退避重试，并把最后一次的错报出来。
    const failed = {};
    for (const f of mine) {
      for (let k = 0; k < 3; k++) {
        try { rmSync(join(IMG_DIR, f)); failed[f] = ''; break; }
        catch (e) { failed[f] = `${e.code || e.name}: ${e.message}`; await sleep(400); }
      }
    }
    const left = readdirSync(IMG_DIR).filter((f) => !imgBefore.has(f));
    const why = left.map((f) => `${f}（${failed[f] || '没尝试删？'}）`).join(' / ');
    check('上传落下的临时图已清干净（快照 diff 为空）', left.length === 0,
      `本次新图 ${mine.length} 个，删不掉的 ${left.length} 个：${why}`);
  }
  say();

  /* ══════════════ G. 词表只有一处定义 ══════════════ */
  say('G · 词表全仓库只有一处定义');
  let defining = [];
  const scan = (dir) => {
    for (const f of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, f.name);
      if (f.isDirectory()) { if (f.name !== 'vendor') scan(p); continue; }
      if (!/\.(js|html)$/.test(f.name)) continue;
      let s = '';
      try { s = readFileSync(p, 'utf8'); } catch { continue; }
      // 找"字面量词表"：把 5 个词按顺序写死在数组里的地方
      if (/['"]很薄['"]\s*,\s*['"]薄['"]/.test(s)) defining.push(p.replace(WEB, 'web').replace(/\\/g, '/'));
    }
  };
  scan(WEB);
  check('只有 web/js/api.js 定义这套词',
    defining.length === 1 && /web\/js\/api\.js$/.test(defining[0]),
    `命中 ${JSON.stringify(defining)}`);

  /* ══════════════ H. 收尾：把厚度精确还原 ══════════════ */
  say();
  say('H · 收尾（把改过的厚度**精确还原**并读回确认）');
  await patchThickness(ID, ORIG);
  let back = null;
  for (let i = 0; i < 20; i++) {
    back = await itemThickness(ID);
    if (Number(back) === Number(ORIG)) break;
    await sleep(300);
  }
  check(`已还原 #${ID} 的厚度（${ORIG}）`, Number(back) === Number(ORIG), `读回 ${back}`);

  say();
  say(fails === 0 ? '✓ 全部通过' : `✗ ${fails} 项未通过`);
  exitCode = fails === 0 ? 0 : 1;

  ws.close();
} catch (e) {
  if (!e || !e.__done) {
    exitCode = 1;
    say(`\n✗ 检查中途出错：${e && e.message ? e.message : e}`);
    if (e && e.stack) say(e.stack.split('\n').slice(0, 6).join('\n'));
  }
} finally {
  try { proc.kill(); } catch { /* 已经退了 */ }
}

const header = `# checkthick  ·  ${new Date().toISOString()}\n`
  + `# （这份输出带生成时间 —— 读旧文件不要当成这一次的结果）\n\n`;
if (OUT) {
  try { writeFileSync(OUT, header + lines.join('\n') + '\n', 'utf-8'); } catch { /* 忽略 */ }
}
process.exit(exitCode);
