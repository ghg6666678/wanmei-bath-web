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
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, '..', '..', 'android-app', 'site');
const out = join(here, 'public');

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
   web-shell.html 同理：外壳里带着版本号和更新日志，缓存住就发不出去。 */
await writeFile(
  join(out, '_headers'),
  [
    '/latest.json',
    '  Cache-Control: no-store',
    '',
    '/web-shell.html',
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
