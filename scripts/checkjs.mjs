// 前端脚本语法检查器。
//
// 这台开发机没有装浏览器自动化（需要额外下载 Chromium），所以用 Node 的
// vm.Script 把三个页面里的内联 <script> 和 web/js/*.js 全部做一遍语法解析。
// 只能抓语法错误，抓不到运行时错误（DOM 相关），但白屏九成是语法错误。
//
//     node scripts/checkjs.mjs [--out checkjs.out.txt]
//
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const WEB = join(ROOT, "web");

/* `--out <file>`：把控制台输出**同时**落一份 UTF-8 文件。
   ⚠️ 以前两个 .mjs **根本没有 --out**，传了会被**静默忽略** —— 于是 `.out.txt`
   还是旧的，读起来"全绿"，其实这轮一条断言都没落进去（踩过）。
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
      writeFileSync(OUT, `# node scripts/checkjs.mjs  ·  ${new Date().toISOString()}\n` + buf, 'utf8');
    } catch (e) { orig.call(process.stdout, `（--out 写不出去：${e && e.message}）\n`); }
  };
  process.on('exit', dump);
  process.on('SIGINT', () => { dump(); process.exit(130); });
}

let files = [];
for (const f of readdirSync(WEB)) {
  if (f.endsWith(".html")) files.push(join(WEB, f));
}
for (const f of readdirSync(join(WEB, "js"))) {
  if (f.endsWith(".js")) files.push(join(WEB, "js", f));
}

let bad = 0;
let checked = 0;

for (const file of files) {
  const src = readFileSync(file, "utf8");
  const blocks = [];
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(src)) !== null) {
    if (/\bsrc\s*=/i.test(m[1])) continue; // 外链，单独检查
    blocks.push(m[2]);
  }
  if (!file.endsWith(".html")) blocks.push(src);

  blocks.forEach((code, i) => {
    checked++;
    const label = `${file.replace(ROOT + "\\", "")}${blocks.length > 1 ? ` [script ${i + 1}]` : ""}`;
    try {
      new vm.Script(code, { filename: label });
      console.log(`  ok    ${label}`);
    } catch (e) {
      bad++;
      console.log(`  FAIL  ${label}`);
      console.log(`        ${e.message}`);
      const lines = code.split("\n");
      const lm = /:(\d+)/.exec(e.stack || "");
      if (lm) {
        const n = Number(lm[1]);
        for (let k = Math.max(0, n - 3); k < Math.min(lines.length, n + 2); k++) {
          console.log(`        ${String(k + 1).padStart(4)} | ${lines[k]}`);
        }
      }
    }
  });
}

console.log(`\n${checked} script block(s), ${bad} with syntax errors`);
process.exit(bad ? 1 : 0);
