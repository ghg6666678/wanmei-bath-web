# -*- coding: utf-8 -*-
"""检查更新（服务端版）。

GitHub 在国内时通时不通，所以服务端也不把「能访问 GitHub」当前提，而是多通道依次尝试：

    1. GitHub 官方 Release 接口   —— 最权威，带更新日志、APK 直链与 sha256
    2. jsDelivr CDN 读 latest.json —— 国内 CDN，稳定性好
    3. raw 直连读 latest.json

> 曾经还有一条 ghproxy.cn 的代理通道。2026-11 实测发现该域名在移动网络下会把下载
> 跳转到色情站点，已彻底移除。**免费代理域名一律不进代码**——它们随时可能被抢注或
> 劫持，把用户导向哪里完全不受我们控制。宁可少一条通道。

全部失败时依次退回：

    5. 随包发布的 web/latest.json —— 至少知道最新版本号和下载地址
    6. 上次成功的缓存（version_cache.json）

无论走到哪一步，都会把「已尝试的通道」如实返回给前端；
检测失败只影响更新提示，不影响任何业务功能。
"""
import json
import os
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

REPO = 'ghg6666678/wanmei-bath-android'
GITHUB_API = 'https://api.github.com/repos/%s/releases/latest' % REPO
JSDELIVR = 'https://cdn.jsdelivr.net/gh/%s@main/latest.json' % REPO
# jsDelivr 主节点（Fastly）对分支文件缓存 12 小时，刚发版后仍返回旧版本；
# 备用节点与 raw 都是即时生效的，一起探测并取版本号最高的。
JSDELIVR_GCORE = 'https://gcore.jsdelivr.net/gh/%s@main/latest.json' % REPO
RAW = 'https://raw.githubusercontent.com/%s/main/latest.json' % REPO

LOCAL_MANIFEST = os.path.join(HERE, 'web', 'latest.json')
CACHE_FILE = os.path.join(HERE, 'version_cache.json')

# 网页端自身版本（刷新页面即为最新，不需要安装）
WEB_VERSION = '1.0.3'

# 单个通道超时 / 整体缓存时长
TIMEOUT = 6
CACHE_TTL = 1800          # 30 分钟
STALE_CACHE_TTL = 300     # 降级结果只缓存 5 分钟，网络恢复后尽快重探
STALE_TTL = 7 * 24 * 3600  # 磁盘缓存里超过 7 天就不再用

# 下载加速：只走 jsDelivr 这类有企业背书的 CDN。APK 同时提交到仓库 release/ 目录，
# 并用版本 tag 固定路径，这样拿到的就是这一版，不受分支缓存影响。
# 用版本 tag 定位，而不是 @main：jsDelivr 对分支快照有 12 小时解析缓存，
# 刚发版时 @main 可能还指向旧提交（404 或返回上一版文件）；tag 是全新引用，立刻可用。
# 因此发版顺序必须是「提交 APK → 打 tag → 发 Release」。
# 只有这两个节点是「直接服务内容」的（响应头 Server: cloudflare）。
# 曾用的 jsd.onmicrosoft.cn 实测是 301 跳 raw.githubusercontent.com，不是真 CDN，已移除。
CDNS = [
    'https://cdn.jsdelivr.net/gh/%s@v%s/release/%s',
    'https://gcore.jsdelivr.net/gh/%s@v%s/release/%s',
]

_lock = threading.Lock()
_cache = None          # {'latest':..., 'source':..., 'at':...}
_cache_at = 0.0


# ---------------- 工具 ----------------

def _http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'BathHelper/1.0',
        'Accept': 'application/json, text/plain, */*',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8')


NOTE_MAX = 3000          # 更新日志摘要的字符上限：够放下要点，再长就让弹层滚动
NOTE_MD_MAX = 20000      # 完整 Markdown 原文的上限


def _is_table_sep(line):
    """Markdown 表格的分隔行，如 |---|:--:|。

    用字符集合判断而不是正则：`\\-` 这类转义写在多层字符串里很容易被吃掉，
    这里只需要知道「这一行除了竖线、冒号、减号、空格没别的」。
    """
    return bool(line) and all(c in '|:- ' for c in line)


def _summarize(body):
    """把 Release 正文抽成前端能直接显示的纯文本。

    与原生 Update.summarize 规则保持一致：跳过代码块与一级标题；
    二/三级标题当小标题；列表换「·」；表格转「· 左列 → 右列」；
    去掉 **加粗** 与 `代码` 记号。

    上限按字符数而不是行数 —— 之前限 4 行，遇到「短行多」的说明会截得只剩标题，
    1.1.1 的日志就只显示出两行。
    """
    if not body:
        return ''
    ls = body.split('\n')
    out = []
    in_code = False
    pending_head = None      # 小标题先存着，等它下面真有内容再输出

    for i, raw in enumerate(ls):
        t = raw.strip()
        if t.startswith('```'):
            in_code = not in_code
            continue
        if in_code or not t:
            continue
        if t.startswith('# '):
            continue

        if t.startswith('|'):
            nxt = ls[i + 1].strip() if i + 1 < len(ls) else ''
            if _is_table_sep(t) or _is_table_sep(nxt):
                continue                       # 分隔行 / 表头
            cells = [c.strip() for c in t.strip('|').split('|') if c.strip()]
            t = '· ' + ' → '.join(cells)
        elif t.startswith('### '):
            pending_head = t[4:].strip()
            continue
        elif t.startswith('## '):
            pending_head = t[3:].strip()
            continue
        elif t.startswith('- ') or t.startswith('* '):
            t = '· ' + t[2:]
        elif t.startswith('> '):
            t = t[2:]

        if pending_head is not None:
            if out:
                out.append('')                 # 小标题前空一行
            out.append(pending_head)
            pending_head = None

        t = t.replace('**', '').replace('`', '')
        out.append(t)
        if sum(len(x) + 1 for x in out) >= NOTE_MAX:
            text = '\n'.join(out)[:NOTE_MAX] + '…（已截断，完整说明见下方链接）'
            return text

    return '\n'.join(out)


def _with_download(latest):
    """补齐下载地址、镜像地址。"""
    ver = latest.get('version') or ''
    apk = latest.get('apkUrl') or ''
    if not apk and ver:
        name = latest.get('apkName') or ('zhishui-%s.apk' % ver)
        latest['apkName'] = name
        apk = 'https://github.com/%s/releases/download/v%s/%s' % (REPO, ver, name)
        latest['apkUrl'] = apk
    if not latest.get('releaseUrl'):
        latest['releaseUrl'] = ('https://github.com/%s/releases/tag/v%s' % (REPO, ver)
                                if ver else 'https://github.com/%s/releases/latest' % REPO)
    if not latest.get('mirrors'):
        name = latest.get('apkName') or ''
        # 只有「仓库 release/ 目录里确实有这个文件」时才能拼 CDN 地址
        if ver and name:
            latest['mirrors'] = [t % (REPO, ver, name) for t in CDNS]
    return latest


# ---------------- 下载地址实测 ----------------

def _probe_one(url, timeout=2.5):
    """取 1 个字节验证地址可用性；200/206 都算通。"""
    try:
        req = urllib.request.Request(url, method='GET')
        req.add_header('Range', 'bytes=0-0')
        req.add_header('User-Agent', 'BathHelper/1.0 (Server)')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except Exception:
        return False


def _attach_downloads(latest):
    """逐个试下载地址，把结果写进 downloads（url / ok / ms）。

    为什么要做：地址能不能打开完全取决于用户所在网络，列一排让用户自己试，
    常常是点了才发现下不动。先替他把这一步做掉。
    """
    urls = []
    if latest.get('apkUrl'):
        urls.append(latest['apkUrl'])
    for m in (latest.get('mirrors') or []):
        if m and m not in urls:
            urls.append(m)
    if not urls:
        return latest

    got = {}

    def work(u):
        t0 = time.time()
        got[u] = (_probe_one(u), int((time.time() - t0) * 1000))

    threads = [threading.Thread(target=work, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.2)

    out = []
    for i, u in enumerate(urls):
        ok, ms = got.get(u, (False, 0))
        out.append({'url': u, 'ok': ok, 'ms': ms,
                    'kind': 'github' if (i == 0 and latest.get('apkUrl')) else 'cdn'})
    latest['downloads'] = out
    return latest


# ---------------- 各通道的解析 ----------------

def _from_release(d):
    ver = (d.get('tag_name') or '').lstrip('v')
    if not ver:
        raise ValueError('接口没有版本号')
    out = {
        'version': ver,
        'publishedAt': d.get('published_at') or d.get('created_at') or '',
        'notes': _summarize(d.get('body') or ''),
        # 完整 Markdown 原文也带上，前端直接渲染成 HTML。
        # notes 是给旧客户端的纯文本摘要，两份并存谁都不吃亏。
        'notesMd': (d.get('body') or '')[:NOTE_MD_MAX],
    }
    for a in d.get('assets') or []:
        if (a.get('name') or '').lower().endswith('.apk'):
            out['apkName'] = a.get('name')
            out['apkUrl'] = a.get('browser_download_url')
            digest = (a.get('digest') or '').replace('sha256:', '').strip()
            if digest:
                out['sha256'] = digest
            if a.get('size'):
                out['apkSize'] = a['size']
            break
    return out


def _from_manifest(d):
    ver = (d.get('version') or '').strip()
    if not ver:
        raise ValueError('清单没有版本号')
    out = {
        'version': ver,
        'versionCode': d.get('versionCode', 0),
        'publishedAt': d.get('publishedAt') or '',
        'notes': d.get('notes') or '',
    }
    if d.get('apkName'):
        out['apkName'] = d['apkName']
    if d.get('apkUrl'):
        out['apkUrl'] = d['apkUrl']
    if d.get('sha256'):
        out['sha256'] = d['sha256']
    return out


# ---------------- 缓存 ----------------

def _read_local_manifest():
    try:
        with open(LOCAL_MANIFEST, encoding='utf-8') as f:
            return _from_manifest(json.load(f))
    except Exception:
        return None


def _read_cache_file():
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            c = json.load(f)
        if time.time() - float(c.get('at') or 0) > STALE_TTL:
            return None
        return c
    except Exception:
        return None


def _write_cache_file(latest, source):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'latest': latest, 'source': source, 'at': time.time()},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 主流程 ----------------

def _probe():
    """并行探测各通道，返回 (latest, source, tried)。"""
    from concurrent.futures import ThreadPoolExecutor

    sources = [
        ('GitHub 官方接口', GITHUB_API, _from_release),
        ('jsDelivr CDN', JSDELIVR, _from_manifest),
        ('jsDelivr 备用节点', JSDELIVR_GCORE, _from_manifest),
        ('raw 直连', RAW, _from_manifest),
    ]
    tried = []
    latest = None
    source = ''

    def run(item):
        name, url, parser = item
        try:
            d = json.loads(_http_get(url))
            return name, parser(d), None
        except Exception as e:
            return name, None, str(e)

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        for name, got, err in pool.map(run, sources):
            if got is not None:
                tried.append('%s 成功' % name)
                if latest is None or _ver_num(got.get('version')) > _ver_num(latest.get('version')):
                    latest = got
                    source = name
            else:
                tried.append('%s 失败' % name)
    return latest, source, tried


def _ver_num(v):
    """把 1.10.2 这类版本号拆成可比较的元组，按「主.次.补」的顺序。

    曾经这里写成了 (nums[2], nums[1], nums[0])——把补丁位当成主版本比，
    结果 1.1.0 被判成比 1.0.5 小，检查更新永远选不到新版本。
    1.0.x 之间因为主次版本相同，倒序后恰好仍是对的，所以这个 bug 一直没暴露。
    """
    try:
        nums = [int(x) for x in __import__('re').findall(r'\d+', str(v or ''))]
    except Exception:
        return ()
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def get_version_info(force=False):
    """给前端的版本信息。

    返回结构与安卓原生保持一致：
      {ok, current:{version,versionCode}, latest:{...}, source, stale, error, tried}
    """
    global _cache, _cache_at

    now = time.time()
    with _lock:
        # 降级结果（stale）只缓存 5 分钟：网络恢复了要尽快重新探测
        ttl = STALE_CACHE_TTL if _cache and _cache.get('stale') else CACHE_TTL
        if not force and _cache and now - _cache_at < ttl:
            c = dict(_cache)
            c['current'] = {'version': WEB_VERSION, 'versionCode': 0}
            return c

    latest, source, tried = _probe()
    stale = False
    error = ''

    if latest is None:
        local = _read_local_manifest()
        if local is not None:
            latest = local
            source = '本地清单'
            stale = True
            error = '连不上 GitHub'
        else:
            cached = _read_cache_file()
            if cached:
                latest = cached.get('latest') or {}
                source = '上次检测结果（缓存）'
                stale = True
                error = '连不上 GitHub'
            else:
                return {
                    'ok': False,
                    'current': {'version': WEB_VERSION, 'versionCode': 0},
                    'latest': {},
                    'source': '',
                    'stale': True,
                    'error': '连不上 GitHub，且没有可用的本地清单。请到项目主页手动下载。',
                    'tried': tried,
                }

    latest = _with_download(latest)
    # 实测下载地址：结果跟着版本信息一起缓存，30 分钟内不重复探测
    latest = _attach_downloads(latest)
    result = {
        'ok': True,
        'current': {'version': WEB_VERSION, 'versionCode': 0},
        'latest': latest,
        'source': source,
        'stale': stale,
        'error': error,
        'tried': tried,
    }
    with _lock:
        _cache = result
        _cache_at = now
    _write_cache_file(latest, source)
    return dict(result)
