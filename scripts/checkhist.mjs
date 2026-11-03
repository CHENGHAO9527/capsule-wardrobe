#!/usr/bin/env node
/* ══════════════════════════════════════════════════════════════════════
   计划页「常用」自检（scripts/checkhist.mjs）

   为什么单独有这个检查：用户对旅行页的原话是
     "我每次只要有一点变动，再回到旅行界面，就要重新输入城市、重新输入时间，
      我觉得这个就很折磨人。"
   于是加了「常用」这一块：搜过的城市 + 存过的行程，点一下就走。
   而它的**失效方式全是静默的** —— localStorage 写不进去、点了没反应、
   模式切走还留着 —— 页面上一个错都不会报。所以必须点名测量。

   本文件钉住的不变量（改了任何一条都会红）：
     A. 「常用」只在旅行模式出现（一周模式不该看见一行"去旅行"的东西）
     B. 预置的搜过城市 → 按钮数量、名字都对
     C. 预置的存过的行程 → 行在、天数算对（4+6 = 10 天，不是"6 天"）
     D. 点城市按钮 = **加一段**（不是替换、不是没反应）
     E. 点存过的行程 = **整趟换回来**（2 段），不是"再加两段"
     F. 「×」删得掉，而且 localStorage 同步少了（不是只删了 DOM）
     G. 一条都没有时给的是**说明**，不是空白卡
     H. `rememberTrip()` 真的落库，且同一趟重复排不会存两份

   用法：node scripts/checkhist.mjs [--out scripts/checkhist.out.txt]
   前置：服务在 8077 跑着（scripts/serve.py）
   依赖：本机 Playwright 的 chrome-headless-shell（找不到会明确报错，不假装通过）
   ══════════════════════════════════════════════════════════════════════ */
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir, networkInterfaces } from 'node:os';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = Number(process.env.CDP_PORT || 9446);
const OUT = (() => {
  const i = process.argv.indexOf('--out');
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : null;
})();

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

const shell = findShell();
if (!shell) {
  const fatal = ['找不到 chrome-headless-shell。',
    '装一个：npx playwright install chromium',
    '或者用 CHROME_SHELL=<路径> 指定。'];
  console.error(fatal.join('\n'));
  if (OUT) writeFileSync(OUT, `# checkhist · ${new Date().toISOString()}\n\n` + fatal.join('\n') + '\n', 'utf-8');
  process.exit(2);
}

let BASE;
try { BASE = await resolveBase(); }
catch (e) {
  console.error(String(e.message || e));
  if (OUT) writeFileSync(OUT, `# checkhist · ${new Date().toISOString()}\n\n` + String(e.message || e) + '\n', 'utf-8');
  process.exit(2);
}

const profile = join(tmpdir(), `capsule-hist-${Date.now()}`);
mkdirSync(profile, { recursive: true });

// ⚠️ 三条硬规矩（都踩过）：
//   ① 必须给 --user-data-dir，不给调试端口根本不起来
//   ② 启动参数里**不能带目标 URL**，也不能加 --virtual-time-budget ——
//      两者都会让 headless shell 在拿到调试目标之前自己退出
//   ③ 等待用显式 sleep / 轮询，别指望事件
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
        // ⚠️ 必须取**页面** target，不能取 /json/version 的浏览器级端点 ——
        // 往浏览器级端点发 Page/Runtime 会被静默忽略（表现为所有 evaluate 都是 undefined）。
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
  async function goto(path, wait = 3000) {
    await send('Page.navigate', { url: BASE + path });
    await sleep(wait);
  }
  async function waitForJs(expr, ms = 20000, step = 400) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await evalJs(expr)) return true;
      await sleep(step);
    }
    return false;
  }
  const seed = (key, obj) => evalJs(
    `localStorage.setItem(${JSON.stringify(key)}, ${JSON.stringify(JSON.stringify(obj))})`);
  const read = (key) => evalJs(
    `JSON.parse(localStorage.getItem(${JSON.stringify(key)}) || 'null')`);
  const pickMode = (m) => evalJs(
    `(() => { const b = document.querySelector('#modeseg [data-mode="${m}"]');
       if (b) b.click(); return !!b; })()`);

  say(`计划页「常用」自检  ·  ${new Date().toISOString()}`);
  say(`目标 ${BASE}   ·   浏览器 ${shell.split(/[\\/]/).pop()}`);
  say();

  const NICE = { label: 'Nice · 法国', city: 'Nice', lat: 43.7102, lon: 7.262, days: 4 };
  const PARIS = { label: 'Paris · 法国', city: 'Paris', lat: 48.8566, lon: 2.3522, days: 6 };
  const strip = (o) => ({ label: o.label, city: o.city, lat: o.lat, lon: o.lon });
  const TRIP_NAME = 'Nice×4 › Paris×6';

  /* ── A. 一周模式下「常用」不出现 ── */
  say('A · 「常用」只在旅行模式出现');
  await goto('/plan');
  await seed('capsule.cities', [strip(NICE), strip(PARIS)]);
  await seed('capsule.trips', [{ key: TRIP_NAME, segs: [NICE, PARIS] }]);
  await goto('/plan', 3500);
  await waitForJs(`!!document.querySelector('#modeseg [data-mode="week"]')`);

  const visOf = `(() => { const c = document.querySelector('#histCard');
    if (!c) return 'MISSING';
    return getComputedStyle(c).display === 'none' ? 'none' : 'shown'; })()`;
  check('一周模式下不显示', (await evalJs(visOf)) === 'none',
    `实际 ${await evalJs(visOf)}`);

  check('切到旅行模式成功', await pickMode('trip'), '');
  await sleep(900);
  check('旅行模式下显示出来', (await evalJs(visOf)) === 'shown',
    `实际 ${await evalJs(visOf)}`);

  /* ── B. 搜过的城市 ── */
  say();
  say('B · 搜过的城市成了按钮');
  const chips = await evalJs(
    `Array.from(document.querySelectorAll('#cityQuick .histchip')).map(b => b.textContent.trim())`);
  check('两个城市都在，顺序=最近在用在前', JSON.stringify(chips) === JSON.stringify(['Nice', 'Paris']),
    JSON.stringify(chips));

  /* ── C. 存过的行程 ── */
  say();
  say('C · 存过的行程（尼斯 4 天 → 巴黎 6 天）');
  const rows = await evalJs(`Array.from(document.querySelectorAll('#tripSaved .triprow')).map(r => ({
    nm: (r.querySelector('.tnm') || {}).textContent || '',
    nd: (r.querySelector('.tnd') || {}).textContent || '',
    del: !!r.querySelector('.tripdel'),
  }))`);
  check('行程行画出来了', rows.length === 1, `行数 ${rows.length}`);
  check('名字是「Nice → Paris」', rows[0] && rows[0].nm.trim() === 'Nice → Paris',
    rows[0] ? rows[0].nm : '');
  // 天数必须按每段之和算（4+6=10）。写成"只取最后一段"就会显示 6 —— 那正是
  // 分段行程最容易算错的地方，所以这里点名卡住。
  check('天数是两段之和 = 10 天', rows[0] && rows[0].nd.replace(/\s/g, '') === '10天',
    rows[0] ? rows[0].nd : '');
  check('带删除键', !!(rows[0] && rows[0].del), '');

  /* ── D. 点城市按钮 = 加一段 ── */
  say();
  say('D · 点一下城市按钮 -> 加一段（不用再搜一遍）');
  const segRows = `document.querySelectorAll('#cityNow .segrow').length`;
  check('点之前一段都没有', (await evalJs(segRows)) === 0, `实际 ${await evalJs(segRows)}`);
  await evalJs(`document.querySelector('#cityQuick .histchip').click()`);
  await waitForJs(`${segRows} === 1`, 12000);
  check('点了一下就多了一段', (await evalJs(segRows)) === 1, `实际 ${await evalJs(segRows)}`);
  const nm1 = await evalJs(
    `(document.querySelector('#cityNow .segnm') || {}).textContent || ''`);
  check('那一段是刚点的 Nice', String(nm1).includes('Nice'), nm1);
  // 第一段的天数沿用「排几天」（默认 7）—— 不是硬编码 3
  const d1 = await evalJs(
    `(() => { const s = document.querySelector('#cityNow .segdays');
       return s ? s.value : ''; })()`);
  check('第一段天数沿用「排几天」（7）', String(d1) === '7', `实际 ${d1}`);
  // 排成功之后这一趟自己也进「常用」了（走的是"点城市按钮"这条真实路径，
  // 不是直接调函数）。⚠️ 后面 E 段必须**认得出**这条，否则会以为"列表里
  // 只有巴黎那趟"而点错行 —— 实测就是这么红的。
  // ⚠️ 等的是**那一趟**（key='Nice'），不是"列表非空" —— 列表一开始就有一条
  // 预置的，写成 `length >= 1` 会立刻返回，然后断言跑在一次还没落库的排算前面。
  await waitForJs(
    `(JSON.parse(localStorage.getItem('capsule.trips')||'[]')||[]).some(t => t.key === 'Nice')`,
    25000);
  const afterD = await read('capsule.trips');
  check('排成功的那趟自动进了「常用」',
    Array.isArray(afterD) && afterD.some(t => t.key === 'Nice'),
    JSON.stringify((afterD || []).map(t => t.key)));

  /* ── E. 点存过的行程 = 整趟换回来 ── */
  say();
  say('E · 点一下存过的行程 -> 整趟换回来（不是再追加两段）');
  await waitForJs(`typeof busy === 'undefined' || !busy`, 20000);
  // ⚠️ **按名字选行**，不能按下标 `[data-trip]`。D 段刚把「Nice」（单段）存进
  // 常用并排到了最前面，按下标点到的就是它 —— 实测正是因此红过一次：
  // 功能完全正常，红的却是这条断言在依赖"列表里恰好只有一趟"。
  const clicked = await evalJs(`(() => {
    const b = Array.from(document.querySelectorAll('#tripSaved .tripgo'))
      .find(x => x.textContent.includes('Paris'));
    if (!b) return false;
    b.click();
    return true;
  })()`);
  check('找得到「尼斯 → 巴黎」那行', clicked, '');
  await waitForJs(`document.querySelectorAll('#cityNow .segrow').length === 2`, 12000);
  check('变成 2 段（是"换"不是"加"）', (await evalJs(segRows)) === 2,
    `实际 ${await evalJs(segRows)}`);
  const names = await evalJs(
    `Array.from(document.querySelectorAll('#cityNow .segnm')).map(x => x.textContent.trim())`);
  check('两段就是 Nice / Paris', JSON.stringify(names) === JSON.stringify(['Nice', 'Paris']),
    JSON.stringify(names));
  const days2 = await evalJs(
    `Array.from(document.querySelectorAll('#cityNow .segdays')).map(s => s.value)`);
  check('每段天数也回来了（4 / 6）', JSON.stringify(days2) === JSON.stringify(['4', '6']),
    JSON.stringify(days2));

  /* ── F. 「×」删得掉，而且 localStorage 同步 ── */
  say();
  say('F · 「×」只删那一趟（删错行 = 用户丢数据），localStorage 要同步');
  const removed = await evalJs(`(() => {
    const row = Array.from(document.querySelectorAll('#tripSaved .triprow'))
      .find(r => r.textContent.includes('Paris'));
    if (!row) return false;
    row.querySelector('.tripdel').click();
    return true;
  })()`);
  check('找得到要删的那行', removed, '');
  await waitForJs(`!Array.from(document.querySelectorAll('#tripSaved .tnm'))
      .some(x => x.textContent.includes('Paris'))`, 8000);
  const leftKeys = await evalJs(
    `Array.from(document.querySelectorAll('#tripSaved .tnm')).map(x => x.textContent.trim())`);
  check('巴黎那行没了', !leftKeys.some(t => String(t).includes('Paris')), JSON.stringify(leftKeys));
  // 删的是**那一行**，别的行程一件都不能少 —— 比"删空了"是更强的断言。
  check('另一趟（Nice）还在', leftKeys.some(t => String(t).includes('Nice')),
    JSON.stringify(leftKeys));
  const after = await read('capsule.trips');
  check('localStorage 同步少了一条（不是只删 DOM）',
    Array.isArray(after) && after.length === 1 && after[0].key === 'Nice',
    JSON.stringify((after || []).map(t => t.key)));

  /* ── G. 空状态要说人话 ── */
  say();
  say('G · 一条都没有时给的是说明，不是空白卡');
  await seed('capsule.cities', []);
  await seed('capsule.trips', []);
  await evalJs(`(function(){ if (typeof renderHist === 'function') renderHist(); })()`);
  await sleep(300);
  const emptyTxt = await evalJs(
    `(document.querySelector('#cityQuick').textContent + '|' +
      document.querySelector('#tripSaved').textContent).replace(/\\s+/g,' ')`);
  check('城市那栏说明了怎么才会有', String(emptyTxt).includes('搜过'), String(emptyTxt).slice(0, 60));
  check('行程那栏也说明了怎么才会有', String(emptyTxt).includes('排过一次'), '');
  check('两块都没被留成空白',
    !/\|\s*$/.test(String(emptyTxt)) && String(emptyTxt).split('|')[1].trim().length > 6,
    '');

  /* ── H. rememberTrip 真的落库，且同一趟不存两份 ── */
  say();
  say('H · rememberTrip 落库 + 同一趟不重复记');
  const saved = await evalJs(`(function(){
    mode = 'trip';
    segs = [${JSON.stringify(NICE)}, ${JSON.stringify(PARIS)}];
    rememberTrip(); rememberTrip();          // 同一趟排两次
    segs = [${JSON.stringify(NICE)}];
    rememberTrip();                          // 另一趟
    return JSON.parse(localStorage.getItem('capsule.trips') || '[]');
  })()`);
  check('记了 2 趟（同趟没重复）', Array.isArray(saved) && saved.length === 2,
    `实际 ${Array.isArray(saved) ? saved.length : 'null'}`);
  check('最近用的排在最前', !!(saved && saved[0] && saved[0].key === 'Nice'),
    saved && saved[0] ? saved[0].key : '');
  check('存的是字段不是整份结果（有 segs.lat）',
    !!(saved && saved[0] && saved[0].segs && saved[0].segs[0] && saved[0].segs[0].lat),
    '');
  // 同一个行程（尼斯 → 巴黎）改了天数应该**覆盖**那一条，而不是占掉两个格子 ——
  // 否则排两次就把整栏填满了，而它们其实是同一趟旅行。
  const replaced = await evalJs(`(function(){
    mode = 'trip';
    segs = [Object.assign({}, ${JSON.stringify(NICE)}, { days: 9 }),
            ${JSON.stringify(PARIS)}];
    rememberTrip();
    return JSON.parse(localStorage.getItem('capsule.trips') || '[]');
  })()`);
  check('同一趟改了天数 -> 覆盖而不是新增', Array.isArray(replaced) && replaced.length === 2,
    `实际 ${Array.isArray(replaced) ? replaced.length : 'null'}`);
  const over = (replaced || []).find(t => t.key === 'Nice › Paris');
  check('覆盖后留住的是新的天数（9 天）',
    !!(over && over.segs && over.segs[0] && over.segs[0].days === 9),
    over && over.segs && over.segs[0] ? `days=${over.segs[0].days}` : '找不到那趟');

  /* 收尾：把这一轮播下的种子清掉，别留在浏览器 profile 里
     （虽然 profile 是临时目录，但"测完不留痕"这条规矩对任何环境都成立）。 */
  await evalJs(`localStorage.removeItem('capsule.cities'); localStorage.removeItem('capsule.trips');`);

  say();
  if (fails) {
    say(`✗ ${fails} 项没过`);
    exitCode = 1;
  } else {
    say('✓ 全部通过');
  }
} catch (e) {
  say();
  say(`✗ 检查本身出错：${e && e.message ? e.message : e}`);
  exitCode = 1;
} finally {
  try { proc.kill(); } catch { /* 已经退出 */ }
}

if (OUT) {
  writeFileSync(OUT, `# checkhist.mjs  ·  ${new Date().toISOString()}\n\n`
    + lines.join('\n') + '\n', 'utf-8');
}
process.exit(exitCode);
