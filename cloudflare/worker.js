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

/* ---------- 入口 ---------- */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const host = url.hostname.toLowerCase();

    if (REDIRECT_HOSTS.has(host)) {
      const to = new URL(url.pathname + url.search, 'https://' + [...SITE_HOSTS][0]);
      return Response.redirect(to.toString(), 301);
    }

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
      /* 官网里有 4 处 href="/web/" 的绝对路径（site/index.html:46,51,130,139）。
         域名拆分后这些链接必须落到 web 子域，否则会在主域上渲染一份网页端。
         site/ 是发版时自动生成的快照，改它会被覆盖，所以在边缘做重定向。 */
      if (url.pathname === '/web' || url.pathname.startsWith('/web/')) {
        const rest = url.pathname.slice(4) || '/';
        return Response.redirect(
          new URL(rest + url.search, 'https://' + [...WEB_HOSTS][0]).toString(),
          301
        );
      }

      /* 兜底：官网目前没有任何接口调用，这段是为将来留的。若真需要读后端，
         走反代而不是静态查找，避免「接口路径被当成文件名 → 404」。 */
      if (url.pathname.startsWith('/api/')) return proxy(request, url, false);

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
