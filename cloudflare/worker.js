/**
 * yuezao.org 边缘路由（Cloudflare Worker）
 *
 * 目标：
 *   yuezao.org/*        → 官网，静态文件由 Workers 静态资源直出，不回源
 *   www.yuezao.org/*    → 301 到 yuezao.org
 *   web.yuezao.org/*    → 网页端，反代到 Python 服务的 /web/ 与 /api/
 *
 * 为什么官网直出：
 *   官网是纯静态、无登录态的文件（391KB / 12 个）。以前反代回源，每次请求要多飞一趟
 *   （CF 边缘 → 亚州上游 → 回），实测占掉首字节 1.16s 里的 0.5s。放进静态资源后
 *   这一段归零，顺带得到故障隔离：Python 服务挂了，官网仍然打得开。
 *
 * 为什么网页端仍然反代：
 *   有会话、有登录态、要代理完美校园的接口。Workers 的 Python 运行时（Pyodide）
 *   没有 socket，也不算同一套出口 IP，搬不过去——这条边界是设计，不是配置。
 *
 * 部署（必须用 wrangler，Dashboard 粘贴无法携带静态文件）：
 *   cd web-app/cloudflare
 *   node build-assets.mjs      # 从 android-app/site 复制出 public/
 *   npx wrangler deploy        # 首次需 npx wrangler login
 *
 * 自定义域（yuezao.org / web.yuezao.org / www.yuezao.org）绑定在 Worker 名字上，
 * 用 wrangler 重新部署不会丢；但改 name 就会变成另一个 Worker，务必保持 yuezao-edge。
 */

/* ---------- 配置：换域名/换上游只改这里 ---------- */

const UPSTREAM = 'https://zhishui-site.app.workbuddy.host';
const SHARE_CODE = 'KLDDLXOQRD';

const SITE_HOSTS = new Set(['yuezao.org']);
const WEB_HOSTS = new Set(['web.yuezao.org']);
const REDIRECT_HOSTS = new Set(['www.yuezao.org']);

/* 网页端首页的入口路径。外壳由边缘静态资源直出，这几个路径都接住：
   / 是主入口，/web 和 /web/ 是早期 PWA 的 start_url 和老书签。 */
const SHELL_PATHS = new Set(['/', '/web', '/web/']);

/* ---------- 紧急下架开关（2026-11-15） ----------
   事故：有用户打开网页端，看到的是另一个账号（朋友在自己手机上登录过）的数据。
   这说明上游服务端的会话没有按浏览器隔离——在根因修复并验证之前，网页端整站下线。

   为什么放在最外层而不是停上游：边缘直出外壳，停上游只会让页面「打开是好的、
   一用就报错」，既不干净也不够快；这里直接返回 503，外壳和 /api 一起断，
   不会再有第二个用户从缓存里看到别人。

   连带挡住 yuezao.org/api/*：官网本身不需要接口，这条路径此前也能通到同一套
   会话，属于同一个洞的另一个入口。

   官网静态资源（含 APK 下载）不受影响，安卓端更新链路照旧。 */
const WEB_MAINTENANCE = true;

/* ---------- 入口 ---------- */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const host = url.hostname.toLowerCase();

    /* 明文 HTTP 一律 301 到 HTTPS。实测（check-host 8 节点）裸 http:// 会直接
       200 出页面——CF 的「Always Use HTTPS」默认关闭，而浏览器对明文 HTTP
       一律标「不安全」。在 Worker 里拦比在面板开全局开关更可控：路径与
       query 原样保留，其他 Host 也不受影响。 */
    if (url.protocol === 'http:') {
      return Response.redirect('https://' + url.host + url.pathname + url.search, 301);
    }

    if (REDIRECT_HOSTS.has(host)) {
      const to = new URL(url.pathname + url.search, 'https://' + [...SITE_HOSTS][0]);
      return Response.redirect(to.toString(), 301);
    }

    /* 下架期间：整个 web 子域一律 503，先于任何外壳/反代逻辑 */
    if (WEB_MAINTENANCE && WEB_HOSTS.has(host)) return maintenance();

    if (WEB_HOSTS.has(host)) {
      /* 首页外壳由边缘直出，不再回源。
         它是 114KB 的纯前端单文件、内容不随用户变化，放在边缘等于把
         「边缘 ↔ 亚洲上游」这一跳整段削掉；上游只留 /api/ 的业务请求。
         没有 ASSETS 时自动退回反代，配置漂移也不会整页 404。 */
      if (env && env.ASSETS && SHELL_PATHS.has(url.pathname)) {
        return env.ASSETS.fetch(new URL('/web-shell.html', url).toString());
      }
      /* 其余路径（/api/*、其它静态资源）照旧反代，sharecode 由 proxy() 自动补上，
         用户不需要带码。这里原本有一道门（没有码就返回「这是受限入口」说明页），
         2026-11-13 去掉：域名是给使用者直接打开的，多一道门只会让人以为要买码，
         代价远大于收益。如果将来要重新收紧，判断 request.headers 里的 cookie 即可。 */
      return proxy(request, url, true);
    }

    if (SITE_HOSTS.has(host)) {
      /* 官网入口现在直接写裸链 https://web.yuezao.org（不带斜杠、零跳转）。
         这条规则只为兼容历史链接：早期官网用 href="/web/"，二维码、老书签和
         已经发出去的分享链接里还带着这个路径。若在主域直接渲染网页端，就成了
         「同一个应用两个地址」，所以统一 301 到 web 子域。别删。 */
      if (url.pathname === '/web' || url.pathname.startsWith('/web/')) {
        const rest = url.pathname.slice(4) || '/';
        return Response.redirect(
          new URL(rest + url.search, 'https://' + [...WEB_HOSTS][0]).toString(),
          301
        );
      }

      /* 官网目前没有任何接口调用。下架期间这条路径也要断——它和 web 子域一样
         能通到上游那套未隔离的会话。 */
      if (url.pathname.startsWith('/api/')) {
        if (WEB_MAINTENANCE) return maintenance();
        return proxy(request, url, false);
      }

      /* 静态文件直出，不回源。用 env.ASSETS 是否存在来判断，而不是写死一个开关：
         配置漂移时（比如换了个 Worker 忘了挂 assets）能自动退回反代，
         不会整站变成 404。

         html_handling 关掉了自动后缀处理（否则 /privacy.html 会被 307 到 /privacy），
         代价是根路径不再自动落到 index.html，这里手动补一条。 */
      if (url.pathname === '/' && env && env.ASSETS) {
        return env.ASSETS.fetch(new URL('/index.html', url).toString());
      }
      if (env && env.ASSETS) return env.ASSETS.fetch(request);

      return proxy(request, url, false);
    }

    return new Response('not found', { status: 404 });
  }
};

/* ---------- 逻辑 ---------- */

/** 下架期间的统一响应。不缓存，避免有人从 CDN 拿到旧页面。 */
function maintenance() {
  const html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>网页端维护中</title><style>' +
    'body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;' +
    'background:#f4f6f8;color:#101828;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",' +
    '"PingFang SC","Microsoft YaHei",sans-serif;padding:24px}' +
    '.c{max-width:420px} h1{font-size:20px;margin:0 0 12px} p{margin:0 0 10px;color:#475467}' +
    'small{color:#98a2b3}</style></head><body><div class="c">' +
    '<h1>网页端暂时下线</h1>' +
    '<p>我们发现网页端存在数据串号的问题（可能看到别人的账号），正在修复。</p>' +
    '<p>修复并验证之前不会重新开放。<strong>安卓客户端不受影响</strong>，可以继续使用。</p>' +
    '<small>给你带来麻烦，很抱歉。</small>' +
    '</div></body></html>';
  return new Response(html, {
    status: 503,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store, must-revalidate',
      'retry-after': '3600',
    },
  });
}

/** 公开域名路径 → 上游路径。两端共用 /api/*，静态资源按 host 分流。 */
function upstreamPath(url, isWeb) {
  const p = url.pathname;
  if (p.startsWith('/api/')) return p;
  if (isWeb) {
    /* 网页端页面里有一条写死的绝对路径 /web/manifest.webmanifest，
       不能再加一层 /web 前缀，否则会变成 /web/web/... */
    if (p === '/web' || p.startsWith('/web/')) return p;
    return '/web' + (p === '/' ? '/' : p);
  }
  return p;
}

async function proxy(request, url, isWeb) {
  const target = new URL(upstreamPath(url, isWeb), UPSTREAM);
  url.searchParams.forEach((v, k) => target.searchParams.set(k, v));
  if (!target.searchParams.has('sharecode')) {
    target.searchParams.set('sharecode', SHARE_CODE);
  }

  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('cf-connecting-ip');
  headers.delete('cf-ipcountry');
  headers.delete('cf-ray');
  headers.delete('cf-visitor');
  headers.delete('x-forwarded-proto');
  headers.delete('x-real-ip');

  /** @type {RequestInit} */
  const init = {
    method: request.method,
    headers,
    redirect: 'manual' // 上游的 302 要改写成公开域名，不能直接透传
  };
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = request.body;
  }

  let res;
  try {
    res = await fetch(target.toString(), init);
  } catch (e) {
    return new Response('上游暂时不可达', {
      status: 502,
      headers: { 'content-type': 'text/plain; charset=utf-8' }
    });
  }

  const out = new Headers(res.headers);
  out.delete('content-security-policy'); // 上游若带 CSP，会写死旧域名
  out.delete('content-encoding');        // 交给 Cloudflare 重新压缩
  out.delete('transfer-encoding');

  const loc = out.get('location');
  if (loc) out.set('location', rewriteLocation(loc, url, isWeb));

  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: out });
}

/** 上游 Location 指回自己时，换回公开域名；指外部（如 GitHub）则原样保留。 */
function rewriteLocation(loc, url, isWeb) {
  let abs;
  try {
    abs = new URL(loc, UPSTREAM);
  } catch (e) {
    return loc;
  }
  if (abs.origin !== new URL(UPSTREAM).origin) return abs.toString();

  const host = isWeb ? [...WEB_HOSTS][0] : [...SITE_HOSTS][0];
  let path = abs.pathname;
  if (isWeb && path.startsWith('/web')) path = path.slice(4) || '/';

  const out = new URL(path + abs.search, 'https://' + host);
  return out.toString();
}
