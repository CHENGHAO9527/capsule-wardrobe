#!/usr/bin/env node
/* ══════════════════════════════════════════════════════════════════
   手机端几何自检 —— 无头浏览器 + CDP，量**真实渲染结果**。

   为什么需要它：CSS 的问题是"看不见的"。字号调小 1px、padding 多加 3px，
   页面上不会有任何报错、不会有任何日志，只有真机上手才知道点不中。
   这个项目已经吃过一次：
     · 把标签的 padding 从 11 加到 13 → 录入页那两个标签悄悄掉到第二行
       （右栏只有 155px：68+68+6=142 放得下，76+76+6=158 就换行）
     · 底部导航 9.5px、每件衣服上的「洗了」只有 24×20 —— 手机上根本点不准
   这两件事都没有任何测试能发现，因为 Python 自检不跑 CSS，
   checkform 的 DOM 桩没有排版引擎。

   钉六件事（每页**浅色 + 深色都跑一遍**）：
     ① **浅色下底色仍是纸感米砂** —— 防止这轮深色改动把浅色带坏
     ② **深色下有层次** —— 不只验"够不够黑"，还要验：卡片浮得起来（卡/底 ≥ 1.20:1）、
        能点的东西框看得见（框/底 ≥ 2.50:1）、底栏文字达 AA（≥ 4.00:1）。
        ⚠️ 旧版深色正是败在这：底 #141210 与卡 #1e1a17 只差 **1.10:1**，
        用户实拍就是"一整片黑、分不清哪些是按钮"。
     ③ **color-scheme 必须含 dark** —— 这是**对抗浏览器强制暗化**的关键：
        声明 `light dark` 浏览器才会跳过本页；写成 `only light` 反而会招来它（踩过）。
     ④ 7 个页面都不能有横向溢出
     ⑤ 可点元素 ≥ 36px（豁免项逐条写在 EXEMPT 里，必须写清理由）
     ⑥ 底部导航文字 ≥ 11px
     ⑦ **录入页点「删」进入待确认时，版式一动不动** ——
        按钮文案会从「删」变长，按钮又在 flex 行里跟着内容走，
        一撑宽就把旁边的介绍文字挤到换行（用户实拍报过"点一下删，
        左边的字就变形了"）。这条 checkform 抓不到：它的 DOM 桩没有排版引擎。

   用法：node scripts/checkmobile.mjs [--out checkmobile.out.txt]
   前置：服务在 8077 跑着（scripts/serve.py）
   依赖：本机 Playwright 的 chrome-headless-shell（找不到会明确报错，不会假装通过）
   ══════════════════════════════════════════════════════════════════ */
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, writeFileSync, rmSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir, networkInterfaces } from 'node:os';
import { setTimeout as sleep } from 'node:timers/promises';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

/* ── 目标地址：**必须是"真的在跑本应用"的那个地址** ──
   ⚠️ 为什么不能写死 `127.0.0.1`（2026-09-27 踩的，代价是整轮自检结论全假）：
   Windows 允许这些**同时**监听，只要后绑的用更具体的地址：
       0.0.0.0:8077    → 本应用
       127.0.0.1:8077  → 另一个程序（当天是另一个项目起的 python -m http.server）
   写死回环的探针于是**测的是别人家的服务**：拿不到任何元素，
   而失败信息只会说"取不到渲染结果"，完全指不到真正的原因 ——
   这正是这个项目最怕的那类静默失败：**测试自己骗自己**。
   所以：逐个候选地址 /api/ping 验明正身，用第一个真的是本应用的。 */
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
      // 排除回环、链路本地、以及 Clash/Mihomo 的 fake-ip 段（198.18/198.19）
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
const BASE = await resolveBase();

const PORT = Number(process.env.CDP_PORT || 9377);
/* 视口默认 iPhone 14/15 的逻辑分辨率。
   可换机型：`VW=360 VH=780 node scripts/checkmobile.mjs`
   —— 窄一点的安卓机最容易暴露"按钮抢宽、把邻居挤变形"这类问题，
   而这类问题在宽视口下会**假装不存在**。 */
const VW = Number(process.env.VW || 390), VH = Number(process.env.VH || 844);
const OUT = (() => {
  const i = process.argv.indexOf('--out');
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : null;
})();

const PAGES = [
  ['/', '今日'],
  ['/plan', '计划'],
  ['/sets', '套装'],
  ['/wardrobe', '录入'],
  ['/prebuy', '新衣试穿'],
  ['/stats', '衣橱体检'],
  ['/settings', '设置'],
];

/* 可点元素小于 36px 的**豁免名单**。
   ⚠️ 每一条都必须写清理由 —— 这是"豁免"不是"免检"，
   想加一条之前先问：这东西在真机上真的点得中吗？ */
const EXEMPT = [
  { tag: 'INPUT', type: 'range', why: '滑杆：轨道 2px + 滑块 15px 是刻意的编排语言，拖动靠滑块' },
  { tag: 'INPUT', type: 'checkbox', why: '在 <label> 里，整行都是热的（实测 label 宽 233px）' },
  { tag: 'INPUT', type: 'radio', why: '同上' },
  { tag: 'A', cls: '', text: 'platform.deepseek.com', why: '行内文字链接，嵌在一段说明里，跟着文字点' },
];

/* ---------- 找无头浏览器 ---------- */
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

/* ---------- 浏览器里跑的探针 ---------- */
const PROBE = (() => {
  const exemptJson = JSON.stringify(EXEMPT);
  return `(() => {
  const EXEMPT = ${exemptJson};
  const de = document.documentElement;
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = {
    viewport: vw + 'x' + vh,
    pageH: de.scrollHeight,
    overflowX: de.scrollWidth - de.clientWidth,
    colorScheme: getComputedStyle(de).colorScheme,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    // 主题「层次」的证据。只测 bodyBg 是不够的 —— 旧版深色的问题恰恰是
    // "底色和卡片一样黑、按钮边框看不见"，而单看 bodyBg 是正常的。
    cardBg: (() => { const e = document.querySelector('.card'); return e ? getComputedStyle(e).backgroundColor : null; })(),
    cardBorder: (() => { const e = document.querySelector('.card'); return e ? getComputedStyle(e).borderTopColor : null; })(),
    // 取**普通的次级按钮**来测。primary/ok 是实心（边框色=底色，对比度天然极高），
    // danger 是红框（本来就比常规框弱一档），拿它们测会让"框到底看不看得见"失真。
    // ⚠️ 一路兜底到顶栏的「设置」入口 —— 体检页一个 .btn 都没有，
    // 只认 .btn 的话这条断言在那里会拿到 null 而变红（页面上明明一切正常）。
    btnBorder: (() => {
      const e = document.querySelector('.btn:not(.primary):not(.ok):not(.danger)')
             || document.querySelector('.btn')
             || document.querySelector('select')
             || document.querySelector('.gear');
      return e ? getComputedStyle(e).borderTopColor : null;
    })(),
    tabsColor: (() => { const e = document.querySelector('.tabs a'); return e ? getComputedStyle(e).color : null; })(),
    offenders: [], smallHits: [], navFs: null,
  };

  // 横向溢出。排除「可滚动容器内部的元素」—— 横滑条（.pstrip）里的 item
  // 天然超出视口，那是设计不是 bug（第一版探针误报过）。
  const inScroller = (el) => {
    let p = el.parentElement;
    while (p && p !== document.body) {
      if (/(auto|scroll)/.test(getComputedStyle(p).overflowX)) return true;
      p = p.parentElement;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if ((r.right > vw + 1 || r.left < -1) && !inScroller(el)) {
      out.offenders.push({
        tag: el.tagName, cls: String(el.className || '').slice(0, 36),
        txt: (el.textContent || '').trim().slice(0, 14),
        left: Math.round(r.left), right: Math.round(r.right),
      });
    }
    if (out.offenders.length > 12) break;
  }

  const isExempt = (el) => EXEMPT.some((e) => {
    if (el.tagName !== e.tag) return false;
    if (e.type && (el.getAttribute('type') || '') !== e.type) return false;
    if (e.text && !(el.textContent || '').includes(e.text)) return false;
    return true;
  });

  for (const el of document.querySelectorAll('button, a, select, input, summary, [data-act], [data-slot], [data-segdel], [role=button]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.bottom < 0 || r.top > de.scrollHeight) continue;
    if (r.height < 36 || r.width < 36) {
      if (isExempt(el)) continue;
      out.smallHits.push({
        tag: el.tagName, cls: String(el.className || '').slice(0, 28),
        txt: (el.textContent || el.value || el.placeholder || '').trim().slice(0, 12),
        w: Math.round(r.width), h: Math.round(r.height),
      });
    }
    if (out.smallHits.length > 24) break;
  }

  const nav = document.querySelector('.tabs a');
  if (nav) out.navFs = parseFloat(getComputedStyle(nav).fontSize);

  return JSON.stringify(out);
})()`;
})();

function rgbLuma(s) {
  const m = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(String(s));
  if (!m) return null;
  return 0.2126 * +m[1] + 0.7152 * +m[2] + 0.0722 * +m[3];
}

/* WCAG 相对亮度 + 对比度。
   ⚠️ 和上面的 rgbLuma 不是一回事：rgbLuma 只是"看着多亮"的粗加权（判浅色 / 深色够用），
   对比度必须走 sRGB 线性化 —— 否则算出来的数字跟"到底能不能看清"对不上。
   两套主题的"层次够不够"全靠它钉：旧版深色就是被它算穿了 ——
   底 #141210 与卡 #1e1a17 只有 1.10:1，人眼在暗处更分不出来。 */
function rgbOf(s) {
  const m = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(String(s));
  return m ? [+m[1], +m[2], +m[3]] : null;
}
function relLum(c) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}
function contrastOf(a, b) {
  const ca = rgbOf(a), cb = rgbOf(b);
  if (!ca || !cb) return null;
  const la = relLum(ca), lb = relLum(cb);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/* ---------- 主流程 ---------- */
const lines = [];
const fails = [];
const say = (s = '') => lines.push(s);
function ok(cond, msg) {
  lines.push(`  ${cond ? '✓' : '✗'} ${msg}`);
  if (!cond) fails.push(msg);
}

const shell = findShell();
if (!shell) {
  const fatal = [
    '找不到无头浏览器 —— 这个自检没法假装通过。',
    '装一个：npx playwright install chromium',
    '或者用 CHROME_SHELL=<路径> 指定。',
  ];
  console.error(fatal.join('\n'));
  if (OUT) writeFileSync(OUT, `# checkmobile · ${new Date().toISOString()}\n\n` + fatal.join('\n') + '\n', 'utf-8');
  process.exit(2);
}

const profile = join(tmpdir(), `capsule-cdp-${Date.now()}`);
mkdirSync(profile, { recursive: true });

// ⚠️ 三条硬规矩（都踩过）：
//   ① 必须给 --user-data-dir，不给调试端口根本不起来
//   ② 启动参数里**不能带目标 URL**，也不能加 --virtual-time-budget ——
//      两者都会让 headless shell 在拿到调试目标之前自己退出
//   ③ 等待用显式 sleep，别指望事件
const proc = spawn(shell, [
  '--headless', '--disable-gpu', '--hide-scrollbars', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${VW},${VH}`, 'about:blank',
], { stdio: 'ignore' });

// ⚠️ 两个 host 都要试：这台机器上 chrome 的调试端口**有时只 bind 到 IPv6 回环**
// （同一端口上 IPv4 bind 会报 0x271D「访问权限不允许」，于是 127.0.0.1 连不上，
// 而浏览器明明是好的）。只试 127.0.0.1 就会误报"拿不到调试目标"。
async function findTarget() {
  const tried = ['127.0.0.1', '[::1]'];
  for (let i = 0; i < 50; i++) {
    for (const h of tried) {
      try {
        const r = await fetch(`http://${h}:${PORT}/json/list`);
        const t = (await r.json()).find((x) => x.type === 'page');
        if (t && t.webSocketDebuggerUrl) return t.webSocketDebuggerUrl;
      } catch { /* 端口还没起来 / 这个 host 没 bind */ }
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
  await send('Emulation.setDeviceMetricsOverride',
    { width: VW, height: VH, deviceScaleFactor: 2, mobile: true });
  // ★ 关键：**两套主题都要测**。
  //   浅色是日常在用的；深色是"跟着系统自动切"的那一套。
  //   用户实拍报过"深色下一整片黑、分不清哪些是按钮" —— 所以深色不能只验
  //   "它够不够黑"，还必须验**层次**：卡片能不能从底上浮起来、按钮有没有看得见的框。
  //   （旧版深色就是败在这里：底 #141210 与卡 #1e1a17 只差 1.10:1，等于没分层。）
  say(`手机端几何自检  ${VW}×${VH}  ·  浅色 + 深色两套主题  ·  ${new Date().toISOString()}`);
  say(`浏览器 ${shell.split(/[\\/]/).pop()}`);
  say();

  const setScheme = (v) => send('Emulation.setEmulatedMedia',
    { features: [{ name: 'prefers-color-scheme', value: v }] });

  for (const [p, name] of PAGES) {
    say(`────── ${name}  (${p}) ──────`);
    const got = {};
    await setScheme('dark');
    await send('Page.navigate', { url: BASE + p + (p.includes('?') ? '&' : '?') + '_=' + Date.now() });
    await sleep(2600);
    // 只加载一次页面，之后靠切换媒体特性让 CSS 重算 —— 省掉一次完整加载。
    for (const scheme of ['dark', 'light']) {
      if (scheme === 'light') { await setScheme('light'); await sleep(600); }
      const r = await send('Runtime.evaluate', { expression: PROBE, returnByValue: true });
      const raw = r && r.result && r.result.result && r.result.result.value;
      if (!raw) { ok(false, `${scheme}：取不到渲染结果`); continue; }
      got[scheme] = JSON.parse(raw);
    }
    const d = got.dark;
    if (!d) { say(); continue; }
    say(`  视口 ${d.viewport}   页高 ${d.pageH}px`);

    // ── 浅色：底色必须仍是纸感米砂（守住原来那套设计不被这轮改动带坏） ──
    if (got.light) {
      const luma = rgbLuma(got.light.bodyBg);
      ok(luma !== null && luma > 200,
         `浅色：底色仍是纸感米砂（${got.light.bodyBg}，亮度 ${luma === null ? '?' : luma.toFixed(0)} > 200）`);
    }
    // ── color-scheme 两套主题都必须声明"支持深色" ──
    //    ⚠️ 这条是**对抗浏览器强制暗化**的关键：声明了 light dark，
    //    Chrome Auto Dark / WebKit 就会跳过本页；写成 only light 反而会招来它。
    for (const [k, v] of Object.entries(got)) {
      ok(/dark/.test(v.colorScheme),
         `${k}：color-scheme 含 dark（实际=${v.colorScheme}）—— 缺这条浏览器会强制暗化`);
    }
    // ── 深色：够暗 + 层次拉开 ──
    const dl = rgbLuma(d.bodyBg);
    ok(dl !== null && dl < 70,
       `深色：底色是深色（${d.bodyBg}，亮度 ${dl === null ? '?' : dl.toFixed(0)} < 70）`);
    const cCard = contrastOf(d.cardBg, d.bodyBg);
    ok(cCard !== null && cCard >= 1.20,
       `深色：卡片浮得起来（卡/底 ${cCard === null ? '?' : cCard.toFixed(2)}:1 ≥ 1.20）卡=${d.cardBg}`);
    if (d.btnBorder) {
      const cBtn = contrastOf(d.btnBorder, d.bodyBg);
      ok(cBtn !== null && cBtn >= 2.50,
         `深色：能点的东西框看得见（框/底 ${cBtn === null ? '?' : cBtn.toFixed(2)}:1 ≥ 2.50）框=${d.btnBorder}`);
    } else {
      // 不静默跳过 —— 真跳了要说明为什么，否则下次会以为它一直在测
      say('      （本页找不到按钮/输入框/顶栏入口，没有"框"可测）');
    }
    const cNav = contrastOf(d.tabsColor, d.bodyBg);
    ok(cNav !== null && cNav >= 4.00,
       `深色：底栏文字达 AA（${cNav === null ? '?' : cNav.toFixed(2)}:1 ≥ 4.00）`);
    // ── 几何（与主题无关，用深色那一轮的数据） ──
    ok(d.overflowX === 0, `没有横向溢出（${d.overflowX}px）`);
    if (d.offenders.length) {
      for (const o of d.offenders) {
        say(`      ⚠️ 溢出：${o.tag}.${o.cls} L${o.left}→R${o.right} "${o.txt}"`);
      }
    }
    ok(d.smallHits.length === 0,
       `可点元素都 ≥ 36px${d.smallHits.length ? '' : '（豁免的只有滑杆/行内链接/label 里的勾选框）'}`);
    for (const s of d.smallHits) {
      say(`      ⚠️ 太小：${s.tag}.${s.cls} ${s.w}×${s.h} "${s.txt}"`);
    }
    if (d.navFs !== null) {
      ok(d.navFs >= 11, `底部导航文字 ${d.navFs}px ≥ 11px`);
    }
    say();
  }

  /* ══ 单页专项：录入页「删」的两级确认**不许改变版式** ══════════════
     用户实拍报过的：点一下「删」，按钮变成待确认，文案从 1 个字变长，
     按钮跟着内容撑宽 —— 它和介绍文字挤在同一条 flex 行里，于是左边的
     文字被挤到换行，整张卡片当场重排。

     ⚠️ 这条**只有这里能抓**：Python 自检不跑 CSS，checkform 的 DOM 桩
     没有排版引擎，宽度变化它根本看不见。 */
  say('────── 专项 · 录入页点「删」进入待确认，版式必须一动不动 ──────');
  await send('Page.navigate', { url: BASE + '/wardrobe?_=' + Date.now() });
  await sleep(3000);

  /* ⚠️ 2026-09-27 「一行一件」之后：删除按钮**搬进了覆盖式面板**，
     列表行里只有「改字段」。所以必须先揭开面板再量 ——
     不揭开的话 `document.querySelector('.item-card [data-del]')` 是 null，
     下面那个 `if (A.none)` 会**静默跳过整节**（判成"衣橱是空的"）＝ 假绿。
     这一节的存在意义恰恰是防假绿，所以这里显式失败而不是跳过。 */
  const openedRaw = await send('Runtime.evaluate', {
    expression: `(() => { const b = document.querySelector('#list [data-open]');
      if (!b) return 'no-item'; b.click(); return 'ok'; })()`,
    returnByValue: true,
  });
  await sleep(420);
  const openState = openedRaw.result.result.value;

  const ARMED_PROBE = `(() => {
    const card = document.querySelector('.item-card.item-sheet');
    if (!card) return JSON.stringify({ none: true });
    const btn = card.querySelector('[data-del]');
    if (!btn) return JSON.stringify({ none: true, why: '面板里没有删按钮' });
    const info = card.querySelector('.delhint') || card.querySelector('.sheetbody');
    const nm = card.querySelector('.sheettitle');
    const sheet = card.querySelector('.sheet');
    const R = el => { const r = el.getBoundingClientRect();
      return { w: +r.width.toFixed(1), h: +r.height.toFixed(1) }; };
    const lines = el => {
      if (!el) return 0;
      const r = document.createRange(); r.selectNodeContents(el);
      const tops = new Set();
      for (const rc of r.getClientRects()) tops.add(Math.round(rc.top));
      return Math.max(1, tops.size);
    };
    return JSON.stringify({
      info: R(info), nmLines: lines(nm), metaLines: lines(card.querySelector('.meta')),
      card: R(sheet), btn: R(btn), text: btn.textContent.trim(),
      armed: btn.classList.contains('armed'),
    });
  })()`;

  const armedSnap = async () => {
    const r = await send('Runtime.evaluate', { expression: ARMED_PROBE, returnByValue: true });
    return JSON.parse(r.result.result.value);
  };

  const A = await armedSnap();
  if (A.none) {
    // 以前这里写的是"这项跳过（不算失败）"。现在它只可能意味着**页面坏了**
    //（衣橱里明明有 27 件），所以必须红。
    ok(false, `揭不开「改字段」面板或面板里没有删按钮（${openState} / ${A.why || '?'}）—— 这一节不能静默跳过`);
  } else {
    say(`  基线　　：「${A.text}」按钮 ${A.btn.w}×${A.btn.h}　info ${A.info.w}×${A.info.h}　名字 ${A.nmLines} 行 · meta ${A.metaLines} 行`);
    // 只点一次：第一次只会进入待确认，**不会真的删**。
    await send('Runtime.evaluate', {
      expression: `document.querySelector('.item-card [data-del]').click(), 1`,
      returnByValue: true,
    });
    await sleep(320);
    const B = await armedSnap();
    say(`  点一下　：「${B.text}」按钮 ${B.btn.w}×${B.btn.h}　info ${B.info.w}×${B.info.h}　名字 ${B.nmLines} 行 · meta ${B.metaLines} 行`);

    ok(B.armed, '点一下确实进了「待确认」（不是点了没反应）');
    ok(Math.abs(B.btn.w - A.btn.w) < 0.6,
       `按钮宽度不变（${A.btn.w} → ${B.btn.w}）—— 按钮一撑宽就会挤走左边的文字`);
    ok(Math.abs(B.info.w - A.info.w) < 0.6 && Math.abs(B.info.h - A.info.h) < 0.6,
       `介绍那一栏没被挤（宽 ${A.info.w} → ${B.info.w}，高 ${A.info.h} → ${B.info.h}）`);
    ok(B.nmLines === A.nmLines && B.metaLines === A.metaLines,
       `文字行数不变（名字 ${A.nmLines}→${B.nmLines} 行 · meta ${A.metaLines}→${B.metaLines} 行）`);
    ok(Math.abs(B.card.h - A.card.h) < 0.6,
       `整张卡片高度不变（${A.card.h} → ${B.card.h}）`);

    // 等自动复原（代码里是 3.2 秒），复原后也不许动
    await sleep(3600);
    const C = await armedSnap();
    say(`  3.6 秒后：「${C.text}」按钮 ${C.btn.w}×${C.btn.h}　info ${C.info.w}×${C.info.h}`);
    ok(C.text === '删' && !C.armed, '3.2 秒后自动复原回「删」（不会卡在待确认）');
    ok(Math.abs(C.btn.w - A.btn.w) < 0.6 && Math.abs(C.info.w - A.info.w) < 0.6,
       `复原之后版式也回到基线（按钮 ${C.btn.w}、info ${C.info.w}）`);
  }
  say();

  /* ══ 专项二：硬定宽必须兜住"手机上还是旧缓存 HTML"这一种情况 ══════════
     上一版只写了 `min-width:46px`，而 min-width **只是下限**：
     用户手机上的 HTML 是旧的（文案还是六个字「再次点击确认删除」），
     按钮被内容撑到 109px，左边文字照样变形 —— 用户看到的就是
     "你说改好了，我刷新了还是一样的"。

     所以这里做**双向对照**：
       ① 正向：新 CSS 下把文案强行改成长文案，必须仍锁 46px、邻居纹丝不动；
       ② 反向：人为还原旧写法，必须**真的把邻居挤坏** —— 否则证明不了
          "改对了原因"，只证明了"现在碰巧没坏"。 */
  say('────── 专项 · 硬定宽必须扛住旧 HTML 的六字长文案（不许靠"文案短"兜底） ──────');
  await send('Page.navigate', { url: BASE + '/wardrobe?_=' + Date.now() });
  await sleep(2800);
  // 同样必须先揭开面板（删除按钮已在面板里）
  const opened2 = await send('Runtime.evaluate', {
    expression: `(() => { const b = document.querySelector('#list [data-open]');
      if (!b) return 'no-item'; b.click(); return 'ok'; })()`,
    returnByValue: true,
  });
  await sleep(420);
  const ok2 = opened2.result.result.value === 'ok';

  const D0 = await armedSnap();          // 干净基线
  if (D0.none || !ok2) {
    ok(false, `揭不开面板（${opened2.result.result.value}）—— 这一节不能静默跳过`);
  } else {
    /* ① 正向：模拟旧缓存 HTML 的六字文案 */
    await send('Runtime.evaluate', {
      expression: "document.querySelector('.item-card.item-sheet [data-del]').textContent = '再次点击确认删除', 1",
      returnByValue: true,
    });
    await sleep(200);
    const D1 = await armedSnap();
    say(`  六字文案　：「${D1.text}」按钮 ${D1.btn.w}×${D1.btn.h}　info ${D1.info.w}　名字 ${D1.nmLines} 行`);
    ok(Math.abs(D1.btn.w - 46) < 0.6,
       `六个字也不许撑宽，仍锁 46px（实际 ${D1.btn.w}）—— 这才是"不依赖文案长度"`);
    ok(Math.abs(D1.info.w - D0.info.w) < 0.6,
       `邻居没被挤（info ${D0.info.w} → ${D1.info.w}）`);
    ok(D1.nmLines === D0.nmLines,
       `名字行数不变（${D0.nmLines} → ${D1.nmLines} 行）`);

    /* ② 反向对照：人为还原旧写法（min-width + 六字文案），必须真的坏 */
    await send('Runtime.evaluate', {
      expression: "(() => { const b = document.querySelector('.item-card.item-sheet [data-del]');"
        + " b.style.cssText = 'flex:none;width:auto;min-width:46px;max-width:9999px;"
        + "padding-left:12px;padding-right:12px;overflow:visible;white-space:normal';"
        + " return 1; })()",
      returnByValue: true,
    });
    await sleep(200);
    const D2 = await armedSnap();
    say(`  旧写法对照：按钮 ${D2.btn.w}×${D2.btn.h}　info ${D2.info.w}　名字 ${D2.nmLines} 行`);
    ok(D2.btn.w > D1.btn.w + 20,
       `旧写法下按钮确实被撑宽（${D1.btn.w} → ${D2.btn.w}）—— 证明这就是当初变形的真正根因`);
    ok(D2.info.w < D1.info.w - 20 || D2.nmLines > D1.nmLines,
       `旧写法下左边文字确实被挤（info ${D1.info.w} → ${D2.info.w} · 名字 ${D1.nmLines} → ${D2.nmLines} 行）`);
  }
  say();

  /* ══ 专项三：prebuy「手动填一件」的热区 ══════════════
     ⚠️ 为什么必须单列一条，不能指望上面那条通用的「可点元素 ≥36px」：
     prebuy 常驻空态，脚本取几何那一刻元素可能还没渲染完，rect 是 0×0 ——
     而收集逻辑把 0×0 当"不可见"**静默跳过**。
     于是"链接太小手指点不中"就变成了**假绿**：
     390 视口下它真的漏过一次，换成 360 才抓到 63×17。
     所以这里点名道姓地量，跳没跳过一目了然。 */
  say('────── 专项 · prebuy「手动填一件」热区（通用检查会假绿，必须点名量） ──────');
  await send('Page.navigate', { url: BASE + '/prebuy?_=' + Date.now() });
  await sleep(2600);
  const MLraw = await send('Runtime.evaluate', {
    expression: `(() => {
      const a = document.querySelector('#manualLink');
      if (!a) return JSON.stringify({ none: true });
      const r = a.getBoundingClientRect();
      return JSON.stringify({ w: +r.width.toFixed(1), h: +r.height.toFixed(1),
                              t: a.textContent.trim() });
    })()`,
    returnByValue: true,
  });
  const ML = JSON.parse(MLraw.result.result.value);
  if (ML.none) {
    say('  页面上没有「手动填一件」这个链接 —— 跳过（不算失败，但要留意）');
  } else {
    say(`  「${ML.t}」实测热区 ${ML.w}×${ML.h}`);
    ok(ML.w >= 36 && ML.h >= 36,
       `热区 ≥36px（实际 ${ML.w}×${ML.h}）—— 它是空态里唯一的录入兜底入口，点不中就等于没有`);
  }
  say();

  /* ══ 专项四（2026-09-27「一行一件」）：录入页的密度必须真的是"行" ══════
     为什么单列一条：**这是唯一能证明"改造真的生效了"的检查**。
     改前实测（同样 390×844）：待入库单卡 **358.6px**、已入库单行 **241.3px**、
     整页 15945px ≈ 19 屏；「改字段」一展开那一行从 241px 涨到 **1065px**。
     改后：单卡 ~166px、单行 ~76px、整页 7664px。

     阈值取"改前 / 改后"的中间偏改后一点：单卡 ≤220、单行 ≤95。
     ⚠️ 不写死绝对值（内容会变），只钉**量级** —— 一旦有人把行改回大卡片，
     这里会立刻红。 */
  say('────── 专项 · 录入页必须是「一行一件」（量级退化就红） ──────');
  await send('Page.navigate', { url: BASE + '/wardrobe?_=' + Date.now() });
  await sleep(2800);
  const DENS = `(() => {
    const rows = [...document.querySelectorAll('#list .item-card.item-row')];
    const hs = rows.map(e => +e.getBoundingClientRect().height.toFixed(1));
    return JSON.stringify({
      n: rows.length,
      max: hs.length ? Math.max(...hs) : null,
      sum: Math.round(hs.reduce((a, b) => a + b, 0)),
      pageH: document.documentElement.scrollHeight,
      overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    });
  })()`;
  const dr = await send('Runtime.evaluate', { expression: DENS, returnByValue: true });
  const D = JSON.parse(dr.result.result.value);
  if (!D.n) {
    ok(false, '录入页「已入库」一行都没有 —— 这项不能静默跳过');
  } else {
    say(`  ${D.n} 行 · 最高 ${D.max}px · 合计 ${D.sum}px · 整页 ${D.pageH}px`);
    ok(D.max <= 95, `单行 ≤95px（实际最高 ${D.max}px；改前是 241px 的大卡片）`);
    ok(D.overflowX === 0, `没有横向溢出（${D.overflowX}px）—— 槽位按钮 nowrap 之后容易出这条`);
  }
  say();

  say('────── 专项 · 每件衣服上的「洗了」必须齐平（品名/信息行数不同也要齐） ──────');

  /* 用户实拍报过的：「有的在上面，有的在下面，就比较丑陋」——
     截图里第二件是「深灰阔腿长裤」，它的「下装 · 阔腿裤」**折成了两行**，
     于是那一格的「洗了」被顶下去，一排参差不齐。

     根因：`.wash` 原来跟着内容流走。修法：`.oi-info` 改 flex 列 + `.wash { margin-top:auto }`
     钉到卡片底部，与行数无关。

     ⚠️ **不能只等"恰好排出一组行数不同的搭配"** —— 衣橱一变，这个前提就没了，
     断言会退化成空真（第一次写就踩了：8 个温度全是 `3/1`，全组一样）。
     所以这里**人为把一格的信息改成两行**，把条件焊死；再**人为把旧写法还原**，
     证明这条断言真的抓得到那个 bug（双向对照，同 §二十二 那次的做法）。 */
  await send('Page.navigate', { url: BASE + '/?save=0&temp=22&_=' + Date.now() });
  await sleep(3000);

  const ALIGN_PROBE = `(() => {
    const cards = [...document.querySelectorAll('.outfit .oi')];
    if (cards.length < 2) return JSON.stringify({ none: true });
    const linesOf = el => {
      if (!el) return 0;
      const r = document.createRange(); r.selectNodeContents(el);
      const tops = new Set();
      for (const rc of r.getClientRects()) tops.add(Math.round(rc.top));
      return Math.max(1, tops.size);
    };
    const rows = cards.map(c => {
      const w = c.querySelector('.wash');
      const d = c.querySelector('.dislike');
      return { y: w ? Math.round(w.getBoundingClientRect().top) : null,
               meta: linesOf(c.querySelector('.oi-meta')),
               // 「不喜欢」在 3 列窄卡里只有 ~50px，容易被裁成「不喜炊」
               dclip: d ? +(d.scrollWidth - d.clientWidth).toFixed(1) : null };
    });
    const ys = rows.map(x => x.y).filter(v => v !== null);
    return JSON.stringify({ ys, metas: rows.map(x => x.meta),
      dclip: rows.map(x => x.dclip),
      spread: ys.length >= 2 ? Math.max(...ys) - Math.min(...ys) : -1 });
  })()`;

  const evalProbe = async () => JSON.parse((await send('Runtime.evaluate',
    { expression: ALIGN_PROBE, returnByValue: true })).result.result.value);

  // ① 先把第二格的信息人为撑成两行（模拟"下装 · 阔腿裤"折行）
  await send('Runtime.evaluate', { expression: `(() => {
    const ms = [...document.querySelectorAll('.outfit .oi .oi-meta')];
    if (ms.length < 2) return false;
    ms[1].textContent = '下装 · 阔腿裤 · 高腰阔腿版型';
    return true; })()`, returnByValue: true });
  await sleep(120);
  const AL_ok = await evalProbe();

  if (AL_ok.ys.length < 2) {
    ok(false, '今日页没有排出 ≥2 件的搭配 —— 这一项不能静默跳过');
  } else {
    say(`  信息行数 ${AL_ok.metas.join('/')} · 「洗了」位置 ${AL_ok.ys.join(' / ')}（差 ${AL_ok.spread}px）`);
    ok(AL_ok.metas.filter(n => n >= 2).length >= 1,
      `确实有一格的信息折成了两行（${AL_ok.metas.join('/')}）—— 否则下面那条是空真`);
    ok(AL_ok.spread <= 1, `行数不同也齐平（实测最大差 ${AL_ok.spread}px）`);
    // 「不喜欢」是三个字，而 3 列窄卡一格只有 ~50px —— 被裁掉一个字就等于没这个功能
    ok(AL_ok.dclip.every(v => v === null || v <= 1),
      `「不喜欢」没有被裁字（scrollWidth−clientWidth = ${AL_ok.dclip.join('/')}）`);

    // ② 反向对照：把修复撤掉（去掉 margin-top:auto），必须真的变歪
    await send('Runtime.evaluate', { expression: `(() => {
      const st = document.createElement('style');
      st.id = '__unalign';
      st.textContent = '.outfit .oi .oi-acts { margin-top: 3px !important; }';
      document.head.appendChild(st);
      return true; })()`, returnByValue: true });
    await sleep(120);
    const AL_bad = await evalProbe();
    await send('Runtime.evaluate', { expression: `(() => {
      const e = document.getElementById('__unalign'); if (e) e.remove(); return true; })()`,
      returnByValue: true });
    say(`  撤掉修复后：${AL_bad.ys.join(' / ')}（差 ${AL_bad.spread}px）`);
    ok(AL_bad.spread > 1, `撤掉修复必须真的变歪（实测差 ${AL_bad.spread}px）—— 证明这条断言抓得到`);
  }
  say();

  say('────── 专项 · `hidden` 必须真的看不见（CSS 不许把它顶掉） ──────');

  /* 用户看得见的：温度条下面一条**空米色带**。
     根因：`#kitbar` 自己声明了 `display:flex`，作者样式优先级高于浏览器的
     `[hidden] { display: none }` —— 于是"隐藏"的元素仍然占一条高度。
     这类坑只有截图看得见（DOM 里 `hidden` 属性确实在，元素也确实"在"）。
     ⚠️ 所以这里量的是 **computed display + 实际高度**，不是 `el.hidden`。 */
  await send('Page.navigate', { url: BASE + '/?save=0&temp=22&_=' + Date.now() });
  await sleep(3000);
  const HID = await send('Runtime.evaluate', {
    expression: `(() => {
      const ids = ['kitbar', 'anchorbar', 'kitsheet', 'avoided', 'laundry', 'mysets', 'timeline'];
      const bad = [];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (!el || !el.hasAttribute('hidden')) continue;   // 只查"声明了隐藏"的
        const cs = getComputedStyle(el);
        const h = +el.getBoundingClientRect().height.toFixed(1);
        if (cs.display !== 'none' || h > 0.5) bad.push(id + ':' + cs.display + '/' + h + 'px');
      }
      return JSON.stringify({ bad });
    })()`, returnByValue: true,
  });
  const H = JSON.parse(HID.result.result.value);
  ok(H.bad.length === 0,
    `声明了 hidden 的元素必须 display:none 且高度 0（违规：${H.bad.join(', ') || '无'}）`);
  say();

  say('════════════════════════════════════════════════════════');
  if (fails.length) {
    say(`✗ ${fails.length} 项没过：`);
    for (const f of fails) say(`   - ${f}`);
    exitCode = 1;
  } else {
    say('✓ 全部通过');
  }
  ws.close();
} catch (e) {
  lines.push(`\n✗ 自检本身出错：${e && e.message ? e.message : e}`);
  exitCode = 1;
} finally {
  try { proc.kill(); } catch { /* 已经退了 */ }
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows 偶尔锁住，无所谓 */ }
}

const text = lines.join('\n') + '\n';
console.log(text);
if (OUT) {
  // 文件头写生成时间 —— 教训：曾经 `--out` 被静默忽略，读到的是四天前的旧文件
  writeFileSync(OUT, `# node scripts/checkmobile.mjs  ·  ${new Date().toISOString()}\n\n` + text, 'utf-8');
  console.log(`（已写入 ${OUT}）`);
}
process.exit(exitCode);
