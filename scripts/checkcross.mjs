#!/usr/bin/env node
/* ══════════════════════════════════════════════════════════════════════
   跨页一致性自检（scripts/checkcross.mjs）

   为什么单独有这个检查：这个项目里反复出问题的不是"某个页面写错了"，
   而是**页面之间对不上** —— 同一件事两页各写一套：
     · 今日页温控是步进器、体检页曾经是滑杆
     · 今日页存的「场合」到计划页就丢（计划页硬编码「通勤」）
     · 底栏降级后 /sets 差点没有入口
     · "模型排过序"的标记和"备选卡显示分数"用了两套判断依据
   这类问题**单页截图看不出来**，必须跨页对比才现形。

   本文件钉住的不变量（改了任何一条都会红）：
     A. 底栏 5 格、不含「套装」；套装入口在今日页卡 + /sets 仍可直达
     B. 「它在慢慢懂你」「这些今天穿不上」只在体检页，不在今日页
     C. 今日页存的「场合」计划页读得到（含"明确选了不限"和"记录损坏"两个边界）
     D. 「模型排过序」的标记 ⟺ 备选卡不显示分数（两者必须同源）
     E. 体检页的密度算式三因子乘积 == 它自己写的理论上限

   用法：node scripts/checkcross.mjs [--out .tmp-ui/checkcross.txt]
   前置：服务在 8077 跑着（scripts/serve.py）
   依赖：本机 Playwright 的 chrome-headless-shell（找不到会明确报错，不会假装通过）
   ══════════════════════════════════════════════════════════════════════ */
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir, networkInterfaces } from 'node:os';
import { setTimeout as sleep } from 'node:timers/promises';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const PORT = Number(process.env.CDP_PORT || 9445);
const OUT = (() => {
  const i = process.argv.indexOf('--out');
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : null;
})();

/* ── 目标地址：必须是"真的在跑本应用"的那个 ──
   ⚠️ 不能写死 127.0.0.1：Windows 允许 0.0.0.0:8077（本应用）
   和 127.0.0.1:8077（另一个程序）**同时**监听，写死回环会去测别人家的服务。 */
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

/* ── 起浏览器 ── */
const shell = findShell();
if (!shell) {
  const fatal = ['找不到 chrome-headless-shell。',
    '装一个：npx playwright install chromium',
    '或者用 CHROME_SHELL=<路径> 指定。'];
  console.error(fatal.join('\n'));
  if (OUT) writeFileSync(OUT, `# checkcross · ${new Date().toISOString()}\n\n` + fatal.join('\n') + '\n', 'utf-8');
  process.exit(2);
}

let BASE;
try { BASE = await resolveBase(); }
catch (e) {
  console.error(String(e.message || e));
  if (OUT) writeFileSync(OUT, `# checkcross · ${new Date().toISOString()}\n\n` + String(e.message || e) + '\n', 'utf-8');
  process.exit(2);
}

const profile = join(tmpdir(), `capsule-cross-${Date.now()}`);
mkdirSync(profile, { recursive: true });

// ⚠️ 三条硬规矩（都踩过）：
//   ① 必须给 --user-data-dir，不给调试端口根本不起来
//   ② 启动参数里**不能带目标 URL**，也不能加 --virtual-time-budget ——
//      两者都会让 headless shell 在拿到调试目标之前自己退出
//   ③ 等待用显式 sleep，别指望事件
const proc = spawn(shell, [
  '--headless', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--window-size=390,844', 'about:blank',
], { stdio: 'ignore' });

// 两个 host 都要试：chrome 的调试端口有时只 bind 到 IPv6 回环。
async function findTarget() {
  const tried = ['127.0.0.1', '[::1]'];
  for (let i = 0; i < 50; i++) {
    for (const h of tried) {
      try {
        const r = await fetch(`http://${h}:${PORT}/json/list`);
        // ⚠️ 必须取 /json/list 里的**页面** target，不能取 /json/version 的浏览器级端点 ——
        // 往浏览器级端点发 Page/Runtime 命令会被静默忽略，
        // 表现为"所有 evaluate 都返回 undefined"，极难定位。
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
  // ⚠️ 必须关缓存：这个检查要证明"磁盘上刚改的 HTML 生效了"，
  // 读到缓存就等于在测上一版（曾经因此把结论全判反过）。
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
  async function goto(path) {
    await send('Page.navigate', { url: BASE + path });
    await sleep(3000);
  }
  /* 等一个条件成真（轮询），别靠"猜多少毫秒够用"。
     ⚠️ 为什么必须这么写：这一轮服务刚重启过，`_CORE_CACHE` / `_DOMAIN_CACHE`
     都是冷的，`/api/plan` 第一次要跑几秒 —— 固定 `sleep(3000)` 于是不够，
     报出来的是"计划页画不出搭配（可能崩了）"，而页面其实好好的。
     "固定睡眠"和"断言依赖偶然状态"是同一个毛病：换个更快的机器就绿、
     换个更慢的就红，红的时候指向的原因还是错的。 */
  async function waitForJs(expr, ms = 25000, step = 400) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await evalJs(expr)) return true;
      await sleep(step);
    }
    return false;
  }
  const setEnv = (obj) =>
    evalJs(`localStorage.setItem('capsule.env', ${JSON.stringify(JSON.stringify(obj))})`);

  say(`跨页一致性自检  ·  ${new Date().toISOString()}`);
  say(`目标 ${BASE}   ·   浏览器 ${shell.split(/[\\/]/).pop()}`);
  say();

  /* ══ A. 底栏与套装入口 ══
     套装从底栏降级成今日页的卡。降级最容易出的错是"入口没了"——
     用户会以为功能被删了，而页面上不会有任何报错。 */
  say('A · 底栏 5 格 · 套装入口还在');
  const PAGES = [['/', '今日'], ['/plan', '计划'], ['/wardrobe', '录入'],
                 ['/prebuy', '新衣试穿'], ['/stats', '衣橱体检'], ['/sets', '套装'], ['/settings', '设置']];
  const navOf = `Array.from(document.querySelectorAll('.tabs a')).map(a => a.textContent.trim())`;
  const navs = {};
  for (const [p, name] of PAGES) {
    await goto(p);
    navs[name] = await evalJs(navOf);
  }
  const badNav = Object.entries(navs).filter(([, v]) => v.length !== 5);
  check('7 个页面的底栏都是 5 格', badNav.length === 0,
    badNav.length ? badNav.map(([n, v]) => `${n}=${v.length}`).join(' ') : `格数 5`);
  const badSet = Object.entries(navs).filter(([, v]) => v.includes('套装'));
  check('底栏里没有「套装」', badSet.length === 0,
    badSet.length ? badSet.map(([n]) => n).join(' ') : '');
  const want = ['今日', '计划', '录入', '新衣试穿', '衣橱体检'];
  const badItems = Object.entries(navs).filter(([, v]) => want.some(w => !v.includes(w)));
  check('底栏五个入口齐全', badItems.length === 0,
    badItems.length ? badItems.map(([n, v]) => `${n}=[${v}]`).join(' ') : want.join(' / '));

  await goto('/');
  const mysets = await evalJs(`(() => {
    const el = document.querySelector('#mysets');
    return el ? { hidden: el.hidden, txt: el.textContent.replace(/\\s+/g, ' ').trim() } : null;
  })()`);
  check('今日页有「我搭的套装」卡（0 套时也要在）',
    !!mysets && !mysets.hidden && mysets.txt.includes('我搭的套装'),
    mysets ? `hidden=${mysets.hidden}` : '缺 #mysets');
  check('0 套时给的是「去搭第一套」入口',
    !!mysets && mysets.txt.includes('去搭第一套'),
    mysets && mysets.txt.includes('去搭第一套') ? '' : (mysets ? mysets.txt.slice(0, 60) : ''));

  await goto('/sets');
  check('/sets 仍可直达且页面正常',
    await evalJs(`document.body.innerHTML.length > 2000 && !/404|Not Found/.test(document.title)`),
    await evalJs(`document.title`));

  /* ══ B. 搬走的两块只在体检页 ══
     从挑衣服的屏上搬走，是"第一眼不该是这些"的决定；
     搬走后如果两页都还在（或都不在），就是白改。 */
  say();
  say('B · 「慢慢懂你」「今天穿不上」只在体检页');
  await goto('/');
  const todayHas = await evalJs(`(() => {
    const t = document.body.innerText;
    return { learn: t.includes('它在慢慢懂你'), inact: t.includes('这些今天穿不上') };
  })()`);
  check('今日页没有「它在慢慢懂你」', !todayHas.learn);
  check('今日页没有「这些今天穿不上」', !todayHas.inact);

  await goto('/stats');
  const statsHas = await evalJs(`(() => {
    const t = document.body.innerText;
    return { learn: t.includes('它在慢慢懂你'), inact: t.includes('换个温度就能穿') };
  })()`);
  check('体检页有「它在慢慢懂你」', statsHas.learn);
  check('体检页有「换个温度就能穿」', statsHas.inact);

  /* ══ C. 场合跨页继承 ══
     今天页选的场合必须带到计划页。四个边界都要过，
     尤其是"明确选了不限"（空串）—— 不能当成"没记录"而退回「通勤」。 */
  say();
  say('C · 今日页选的「场合」计划页读得到');
  const GET_OCC = `document.querySelector('#occ').value`;

  await goto('/');
  await evalJs(`localStorage.removeItem('capsule.env')`);
  await goto('/plan');
  check('没记录 → 计划页用本页原默认「通勤」', await evalJs(GET_OCC) === '通勤', await evalJs(GET_OCC));

  await goto('/');
  await setEnv({ temp: 20, occ: '休闲' });
  await goto('/plan');
  check('今日页选「休闲」→ 计划页也是「休闲」', await evalJs(GET_OCC) === '休闲', await evalJs(GET_OCC));

  await goto('/');
  await setEnv({ temp: 20, occ: '' });
  await goto('/plan');
  check('今日页选「不限」→ 计划页也是「不限」（空串不能当没记录）',
    await evalJs(GET_OCC) === '', JSON.stringify(await evalJs(GET_OCC)));

  await goto('/');
  await evalJs(`localStorage.setItem('capsule.env', '{"occ":')`);   // 写坏的 JSON
  await goto('/plan');
  check('记录损坏 → 退回默认「通勤」', await evalJs(GET_OCC) === '通勤', await evalJs(GET_OCC));
  check('记录损坏 → 计划页仍画得出搭配（不能整页崩）',
    await waitForJs(`(() => { const r = document.querySelector('#result'); return !!r && r.innerHTML.length > 50; })()`));

  /* ══ D. 排序标记 ⟺ 不显示分数 ══
     两处判断必须同源。上一版标记只看 llm_used、分数还要 llm_error 为空，
     于是出现过"写着模型排过序、下面却按分数从低到高排"。 */
  say();
  say('D · 「模型排过序」与「备选卡不显示分数」同源');
  await goto('/?save=0');
  const sortInfo = await evalJs(`(() => {
    const tag = Array.from(document.querySelectorAll('.tag'))
      .map(e => e.textContent.trim())
      .find(t => t === '模型排过序' || t === '本地排序' || t === '模型没回应') || null;
    const h = Array.from(document.querySelectorAll('.alt-h')).map(e => e.textContent.trim());
    return { tag, heads: h, hasScore: h.some(x => /\\d+ 分/.test(x)) };
  })()`);
  const ordered = sortInfo.tag === '模型排过序';
  check('标记与分数显示一致（模型排序 ⟺ 不给分数）',
    ordered === !sortInfo.hasScore,
    `标记=${JSON.stringify(sortInfo.tag)} 显示分数=${sortInfo.hasScore}`);

  /* ══ E. 体检页的密度算式自洽 ══ */
  say();
  say('E · 体检页「理论上限怎么来的」算式自洽');
  await goto('/stats');
  const calc = await evalJs(`(() => {
    const t = document.body.innerText;
    const m = t.match(/(\\d+)\\s*件上衣\\s*×\\s*(\\d+)\\s*条下装\\s*×\\s*(\\d+)\\s*双鞋\\s*=\\s*(\\d+)\\s*种可能/);
    if (!m) return null;
    return { a: +m[1], b: +m[2], c: +m[3], said: +m[4] };
  })()`);
  if (!calc) {
    check('找得到密度算式', false, '页面上没有「N 件上衣 × N 条下装 × N 双鞋 = N 种可能」');
  } else {
    check('算式三因子乘积等于它写的总可能数',
      calc.a * calc.b * calc.c === calc.said,
      `${calc.a}×${calc.b}×${calc.c}=${calc.a * calc.b * calc.c}，页面写 ${calc.said}`);
  }

  /* ══ F. 时间轴（今日页从"一天"变成"一条时间轴"）══
     ⚠️ 这一节**绝不能点「就穿这套」** —— 那是真实穿着记录。
     只验它"在不在"，不验它"点了会怎样"。 */
  say();
  say('F · 时间轴：往后几天 · 点一天就切过去');
  await goto('/?save=0');
  const tl = await evalJs(`(() => {
    const rows = Array.from(document.querySelectorAll('.tlrow'));
    return {
      n: rows.length,
      onCount: rows.filter(r => r.classList.contains('on')).length,
      labels: rows.map(r => r.querySelector('.tlday') ? r.querySelector('.tlday').childNodes[0].textContent.trim() : ''),
      minH: rows.length ? Math.min(...rows.map(r => r.getBoundingClientRect().height)) : 0,
      hidden: document.querySelector('#timeline') ? document.querySelector('#timeline').hidden : null,
      overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  })()`);
  check('时间轴在且没被隐藏', tl.hidden === false);
  check('有 7 行（今天 + 未来 6 天）', tl.n === 7, `实际 ${tl.n} 行`);
  check('第一行是「今天」', tl.labels[0] === '今天', `实际 ${JSON.stringify(tl.labels[0])}`);
  check('「今天」「明天」都有可见字样',
    tl.labels.includes('今天') && tl.labels.includes('明天'), tl.labels.join('/'));
  check('恰好一行处于选中态', tl.onCount === 1, `实际 ${tl.onCount}`);
  check('每行可点高度 ≥36px（手机下限）', tl.minH >= 36, `最矮 ${Math.round(tl.minH)}px`);
  check('时间轴没撑出横向滚动', tl.overflowX <= 0, `溢出 ${tl.overflowX}px`);

  // 点「明天」：必须切过去，且**不能出现「就穿这套」**（那是穿完才点的动作，
  // 摆在明天的搭配下面会让人误记成今天穿了 —— 穿着记录一脏，后面全错）。
  const clicked = await evalJs(`(() => {
    const rows = Array.from(document.querySelectorAll('.tlrow'));
    const tmr = rows.find(r => (r.querySelector('.tlday') || {}).textContent
      && r.querySelector('.tlday').textContent.includes('明天'));
    if (!tmr) return 'no-tomorrow-row';
    window.__tlWant = tmr.querySelector('.tln').textContent.trim();
    window.__tlDate = tmr.dataset.tlDate || '';
    tmr.click();
    return 'ok';
  })()`);
  check('找得到「明天」那一行并点得动', clicked === 'ok', clicked);
  // 等 hero 真的换掉（不是等一个猜出来的毫秒数）
  await waitForJs(`(() => document.querySelector('#hero [data-act="today-view"]') !== null)()`);
  const afterTomorrow = await evalJs(`(() => {
    const h = document.querySelector('#hero').innerText;
    return {
      heroHasWearBtn: !!document.querySelector('#hero [data-act="wear"]'),
      heroHasBackBtn: !!document.querySelector('#hero [data-act="today-view"]'),
      heroHead: h.split('\\n')[0] || '',
      tlWant: window.__tlWant || '',
      selIsTomorrow: (() => {
        const on = document.querySelector('.tlrow.on');
        return on && on.querySelector('.tlday') ? on.querySelector('.tlday').textContent.includes('明天') : false;
      })(),
      sub: (document.querySelector('#sub') || {}).textContent || '',
      tlDate: window.__tlDate || '',
    };
  })()`);
  check('点「明天」后时间轴选中项跟着走', afterTomorrow.selIsTomorrow === true);
  check('看明天时**没有**「就穿这套」', afterTomorrow.heroHasWearBtn === false,
    `hero 标题=${JSON.stringify(afterTomorrow.heroHead)}`);
  check('看明天时给了「回到今天」的出路', afterTomorrow.heroHasBackBtn === true);
  /* ⚠️ 这一条**绝不许写死日期**。上一版写的是 `sub.includes('9月28日')` ——
     那是写测试那天（09-27 晚上）的"明天"。跨过零点之后这个断言立刻变红，
     而页面完全正确：它报的是"副标题没跟着换成 9月28日"，
     实际副标题是"9月29日 · 周二"，因为**今天**已经是 09-28 了。
     这正是"断言不许依赖跑测机器上的偶然状态"那一条：
     把期望值从「那一天的行」上**读出来**，而不是从写测试的人的日历上抄。 */
  const wantMD = (() => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(afterTomorrow.tlDate || '');
    return m ? `${Number(m[2])}月${Number(m[3])}日` : '';
  })();
  check(`顶栏副标题跟着换成那天（期望含「${wantMD}」）`,
    !!wantMD && afterTomorrow.sub.includes(wantMD),
    `副标题=${JSON.stringify(afterTomorrow.sub)}`);

  /* ★ 时间轴上写的种数，必须等于点进去后副标题里的搭配数。
     这一条是**实测踩出来的**：时间轴不排除洗衣篮、/api/today 排除，
     于是同一温度下 252 和 224 两个数同时出现在屏幕上，用户只会以为坏了。
     洗衣篮只管"今天" —— 洗了的衣服到那天早干了。 */
  const tlNum = (afterTomorrow.tlWant.match(/(\d+)\s*种/) || [])[1];
  const subNum = (afterTomorrow.sub.match(/(\d+)\s*种搭配/) || [])[1];
  check('时间轴的数字 == 点进去的数字（口径必须同一个）',
    tlNum != null && tlNum === subNum,
    `时间轴=${tlNum ?? afterTomorrow.tlWant}  点进去=${subNum ?? '?'}（副标题 ${JSON.stringify(afterTomorrow.sub)}）`);

  // 点「回到今天」：必须回到有「就穿这套」的状态
  await evalJs(`(() => {
    const b = document.querySelector('#hero [data-act="today-view"]');
    if (b) b.click();
    return true;
  })()`);
  await waitForJs(`(() => document.querySelector('#hero [data-act="wear"]') !== null)()`);
  const backToday = await evalJs(`(() => ({
    heroHasWearBtn: !!document.querySelector('#hero [data-act="wear"]'),
    selIsToday: (() => {
      const on = document.querySelector('.tlrow.on');
      return on && on.querySelector('.tlday') ? on.querySelector('.tlday').textContent.includes('今天') : false;
    })(),
  }))()`);
  check('「回到今天」把「就穿这套」还回来了', backToday.heroHasWearBtn === true);
  check('「回到今天」后选中项回到今天那一行', backToday.selIsToday === true);

  // 「在看哪天」绝不能持久化：否则明天打开会停在昨天点的那天上（静默错位）
  await goto('/?save=0');
  const freshSel = await evalJs(`(() => {
    const on = document.querySelector('.tlrow.on');
    return on && on.querySelector('.tlday') ? on.querySelector('.tlday').textContent.trim() : '';
  })()`);
  check('重新加载后回到「今天」（所选日期不持久化）',
    freshSel.includes('今天'), JSON.stringify(freshSel));

  /* ══ G. 计划页两个模式各自的场合默认值 ══
     在家跟着「今日」页、出门默认「休闲」。
     而且**各记各的**：出差把旅行场合改成通勤，切一圈回来不能丢。 */
  say();
  say('G · 计划页：一周跟着今日页 · 旅行默认「休闲」');
  await goto('/');
  await setEnv({ temp: 20, occ: '居家' });
  await goto('/plan');
  const occWeek = await evalJs(`document.querySelector('#occ').value`);
  check('一周模式继承今日页的「居家」', occWeek === '居家', JSON.stringify(occWeek));

  const clickMode = (m) => evalJs(`(() => {
    const b = document.querySelector('#modeseg [data-mode="${m}"]');
    if (!b) return 'no-btn';
    b.click();
    return 'ok';
  })()`);
  check('切得到「旅行」模式', (await clickMode('trip')) === 'ok');
  await sleep(2600);
  const occTrip = await evalJs(`document.querySelector('#occ').value`);
  check('旅行模式默认「休闲」（不跟随一周的「居家」）', occTrip === '休闲', JSON.stringify(occTrip));

  // 出差场景：把旅行场合改成通勤，切回一周再切回来，不能丢
  await evalJs(`(() => {
    const s = document.querySelector('#occ');
    s.value = '通勤';
    s.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`);
  await sleep(1200);
  await clickMode('week');
  await sleep(2600);
  const weekBack = await evalJs(`document.querySelector('#occ').value`);
  check('切回一周时，一周那份还是「居家」', weekBack === '居家', JSON.stringify(weekBack));
  await clickMode('trip');
  await sleep(2600);
  const tripBack = await evalJs(`document.querySelector('#occ').value`);
  check('再切回旅行时，出差改的「通勤」还在（各模式各记一份）',
    tripBack === '通勤', JSON.stringify(tripBack));

  say();
  say(fails === 0 ? '✓ 全部通过' : `✗ ${fails} 项失败`);
  exitCode = fails === 0 ? 0 : 1;
} catch (e) {
  say('✗ 检查没能跑完');
  say('  ' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n  ') : String(e)));
  exitCode = 2;
} finally {
  proc.kill();
}

const text = lines.join('\n') + '\n';
if (OUT) {
  // 文件头写生成时间 —— 教训：曾经 --out 被静默忽略，读到的是四天前的旧文件
  writeFileSync(OUT, `# node scripts/checkcross.mjs  ·  ${new Date().toISOString()}\n\n` + text, 'utf-8');
  console.log(`（已写入 ${OUT}）`);
}
process.exit(exitCode);
