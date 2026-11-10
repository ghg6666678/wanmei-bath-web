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
JSDELIVR_CN = 'https://jsd.onmicrosoft.cn/gh/%s@main/latest.json' % REPO
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
CDNS = [
    'https://cdn.jsdelivr.net/gh/%s@v%s/release/%s',
    'https://gcore.jsdelivr.net/gh/%s@v%s/release/%s',
    'https://jsd.onmicrosoft.cn/gh/%s@v%s/release/%s',
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


def _summarize(body, limit=4):
    """取发布说明里「本次更新」那一节，与原生 Update.summarize 规则一致。"""
    if not body:
        return ''
    lines = body.split('\n')
    start = -1
    for i, l in enumerate(lines):
        t = l.strip()
        if t.startswith('## ') and any(k in t for k in ('更新', '新增', '修复')):
            start = i
            break
    if start < 0:
        for i, l in enumerate(lines):
            if l.strip().startswith('## '):
                start = i
                break
    if start < 0:
        for i, l in enumerate(lines):
            t = l.strip()
            if not t or t.startswith('# '):
                continue
            start = i - 1
            break
    if start < 0:
        return ''

    out = []
    for i in range(start, len(lines)):
        t = lines[i].strip()
        if not t:
            continue
        if i > start and t.startswith('#'):
            break
        if t.startswith('## '):
            t = t[3:].strip()
        if t.startswith('- ') or t.startswith('* '):
            t = t[2:]
        t = t.replace('**', '')
        if len(t) > 70:
            t = t[:70] + '…'
        out.append(t)
        if len(out) >= limit:
            break
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


# ---------------- 各通道的解析 ----------------

def _from_release(d):
    ver = (d.get('tag_name') or '').lstrip('v')
    if not ver:
        raise ValueError('接口没有版本号')
    out = {
        'version': ver,
        'publishedAt': d.get('published_at') or d.get('created_at') or '',
        'notes': _summarize(d.get('body') or ''),
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
        ('jsDelivr 国内节点', JSDELIVR_CN, _from_manifest),
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
    """把 1.10.2 这类版本号拆成可比较的元组（补丁位在前，避免 1.0.9 < 1.0.10）。"""
    try:
        nums = [int(x) for x in __import__('re').findall(r'\d+', str(v or ''))]
    except Exception:
        return ()
    while len(nums) < 3:
        nums.append(0)
    return (nums[2], nums[1], nums[0])


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
