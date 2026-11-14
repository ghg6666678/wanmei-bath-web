/**
 * 把官网静态文件从安卓仓库复制到 cloudflare/public/，供 Workers 静态资源直出。
 *
 * 为什么是复制而不是直接指向 ../../android-app/site：
 *   Wrangler 的 assets.directory 必须在项目目录内，跨仓库的相对路径在 CI 上也会失效。
 *   所以保留「android-app/site 是唯一权威副本」这条规矩，构建时复制一份快照，
 *   public/ 本身写进 .gitignore，不入库、不手改。
 *
 * 用法：node build-assets.mjs
 */

import { cp, rm, mkdir, writeFile, readdir, stat } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, '..', '..', 'android-app', 'site');
const out = join(here, 'public');

/* 隐私政策一致性闸门。
 *
 * site/policy.json 是唯一权威，官网 privacy.html 与应用内置文本都由
 * android-app/tools/gen_policy.py 从它生成。忘记重新生成时，页面一切正常，
 * 只是条文是旧的——这种错只有人工比对才能发现，所以在部署前用指纹拦住：
 * 每份产物里都带着「生成时那段正文的 sha256」，这里按同样的算法重算一次。
 *
 * 算法必须与 gen_policy.py 的 fingerprint() 逐字节一致：
 *   sha256([ver, date, applies, html].join('\n')).slice(0, 12)
 */
function policyHash(json) {
  const raw = [json.ver, json.date, json.applies, json.html].map((v) => String(v ?? '')).join('\n');
  return createHash('sha256').update(raw, 'utf8').digest('hex').slice(0, 12);
}

function stampedHash(file) {
  const text = readFileSync(file, 'utf8');
  const m = /<!-- POLICY:BEGIN[^>]*sha256=([0-9a-f]{12}) -->/.exec(text);
  return m ? m[1] : null;
}

try {
  const policy = JSON.parse(readFileSync(join(src, 'policy.json'), 'utf8'));
  const want = policyHash(policy);
  const targets = [
    ['官网页面', join(src, 'privacy.html')],
    ['应用内置文本', join(here, '..', '..', 'android-app', 'assets', 'index.html')]
  ];
  const stale = [];
  for (const [label, file] of targets) {
    const got = stampedHash(file);
    if (got !== want) stale.push(`  ${label}：${got ? `生成于 ${got}` : '没有生成标记'}，应为 ${want}`);
  }
  if (stale.length) {
    console.error('隐私政策不同步（policy.json 已改，生成物没跟上）：');
    console.error(stale.join('\n'));
    console.error('\n修：cd android-app && python tools/gen_policy.py');
    process.exit(1);
  }
  console.log(`隐私政策一致：v${policy.ver}，sha256=${want}`);
} catch (e) {
  console.error(`隐私政策检查失败：${e.message}`);
  console.error('（先跑 android-app/tools/gen_policy.py，再回来部署）');
  process.exit(1);
}

try {
  await stat(join(src, 'index.html'));
} catch {
  console.error(`找不到官网源目录：${src}`);
  console.error('android-app/site 是官网的权威副本，构建前必须存在。');
  process.exit(1);
}

await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
await cp(src, out, { recursive: true });

/* 网页端外壳也一并带过来，由边缘直接返回。
   好处有两个：省掉「边缘 → 上游 → 回」这一趟跨境往返（首字节从 1.16s 降到 200ms 级），
   以及前端改动只需要 wrangler deploy，不必等上游重新部署。
   web-app/web/index.html 仍然是唯一权威副本，这里只是构建期快照。 */
const webSrc = join(here, '..', 'web', 'index.html');
try {
  await stat(webSrc);
} catch {
  console.error(`找不到网页端源文件：${webSrc}`);
  process.exit(1);
}
await cp(webSrc, join(out, 'web-shell.html'));

/* latest.json 决定「有没有新版本」，一旦被边缘缓存，客户端会一直看到旧版本号，
   表现为「发了版但没人收到更新提示」，而且不会报任何错。所以单独禁缓存。
   web-shell.html 同理：外壳里带着版本号和更新日志，缓存住就发不出去。
   policy.json 更严格：它是隐私政策的正文与版本号，客户端按「远程版本更高就
   重新征求同意」处理——一份被缓存的旧文本会让新用户「同意」作废的条款，
   而界面看起来完全正常，属于最难发现的错。 */
await writeFile(
  join(out, '_headers'),
  [
    '/latest.json',
    '  Cache-Control: no-store',
    '',
    '/web-shell.html',
    '  Cache-Control: no-store',
    '',
    '/policy.json',
    '  Cache-Control: no-store',
    '',
    '/*.apk',
    '  Cache-Control: public, max-age=3600',
    ''
  ].join('\n'),
  'utf8'
);

const files = await readdir(out);
let bytes = 0;
for (const f of files) bytes += (await stat(join(out, f))).size;

console.log(`已生成 ${out}`);
console.log(`文件 ${files.length} 个，合计 ${(bytes / 1024).toFixed(0)} KB`);
console.log(files.sort().join('  '));
