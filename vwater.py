"""完美校园「智慧用水」协议封装。

来源：逆向 xqh5.17wanxiao.com/water3to1 前端 JS（app.89ec9fbfe4bf5925b116.js）
接口：POST https://vwater.59wanmei.com/usewater/api/use/water/h5/WXInvokFront/
签名：AES-ECB-PKCS7(message = uuid + "-" + timestamp_ms, 16 字节固定密钥 / 标准 AES-128)
     密钥是官方前端 JS 里的明文常量；输出 base64 放在请求头 encrypt，配合 timestamp / uuid
     Java 的 AES/ECB/PKCS5Padding 与前端 PKCS7 填充等价
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE = 'https://vwater.59wanmei.com/usewater'
UA = ('Mozilla/5.0 (Linux; Android 16; PJE110) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/138.0.7204.179 Mobile Safari/537.36 wanxiao/5.8.36')
H5_ORIGIN = 'https://xqh5.17wanxiao.com'
SIGN_KEY = b'wanmeixiaoyuan11'   # 16 字节标准 AES-128，官方 H5 前端 JS 明文常量


class VWaterError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Session:
    """一个账号的 H5 会话：cookie + 当前 uuid + 入口页面地址。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj), _NoRedirect())
        self.uuid = None
        self.page = ''
        self._lock = threading.Lock()
        self._ts = 0

    # ---------- 底层 ----------
    def _call(self, path, data=None, headers=None, referer=None):
        body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
        req = urllib.request.Request(BASE + path, data=body)
        req.add_header('User-Agent', UA)
        req.add_header('Origin', H5_ORIGIN)
        req.add_header('Accept', 'application/json, text/plain, */*')
        if referer:
            req.add_header('Referer', referer)
        if body:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8')
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            r = self.opener.open(req, timeout=25)
            return r.status, dict(r.headers), r.read().decode('utf-8', 'ignore')
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode('utf-8', 'ignore')
        except Exception as e:
            raise VWaterError(f'网络异常: {e}')

    def _sign(self, uuid, ts):
        """AES-ECB-PKCS7(uuid + '-' + ts)，16 字节固定密钥（标准 AES-128），输出 base64。"""
        msg = f'{uuid}-{ts}'.encode('utf-8')
        ct = AES.new(SIGN_KEY, AES.MODE_ECB).encrypt(pad(msg, 16))
        return base64.b64encode(ct).decode('ascii')

    # ---------- 会话 ----------
    def entrance(self):
        q = urllib.parse.urlencode({
            'source': 'wx', 'tVer': '20260918', 'versionnum': '1050836101',
            'customerId': self.cfg['customerCode'], 'systemType': 'Android',
            'UAinfo': 'wanxiao', 'versioncode': '10589101',
            'token': self.cfg['token'],
        })
        code, headers, _ = self._call('/api/use-water/entrance?' + q)
        loc = headers.get('Location') or headers.get('location') or ''
        if not loc:
            raise VWaterError('登录态失效（没有拿到跳转地址），请重新提取 token')
        p = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
        if not p.get('uuid'):
            raise VWaterError('登录态失效（uuid 缺失）')
        # 服务端下发的参数才是权威值
        for k in ('outid', 'customerid', 'customerCode'):
            if p.get(k):
                self.cfg[k] = p[k]
        self.uuid = p['uuid']
        self.page = loc
        self._ts = time.time()
        return p

    def _ensure(self):
        if not self.uuid or time.time() - self._ts > 300:
            self.entrance()

    # ---------- 业务 ----------
    def invoke(self, cmd, extra=None, retry=True):
        self._ensure()
        payload = {'cmd': cmd, 'ver': '2.0', 'source': 'wx'}
        payload.update(extra or {})
        ts = str(int(time.time() * 1000))
        enc = self._sign(self.uuid, ts)
        body = {
            'param': json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
            'source': 'wx',
        }
        code, _, text = self._call('/api/use/water/h5/WXInvokFront/', body,
                                   {'uuid': self.uuid, 'timestamp': ts, 'encrypt': enc},
                                   referer=self.page)
        if code != 200 or not text:
            if retry:
                self.uuid = None
                return self.invoke(cmd, extra, retry=False)
            raise VWaterError(f'接口无响应 (HTTP {code})')
        try:
            outer = json.loads(text)
        except Exception:
            raise VWaterError('返回格式异常: ' + text[:200])
        inner = outer.get('body')
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                pass
        if not isinstance(inner, dict):
            msg = outer.get('message_') or outer.get('msg') or text[:200]
            raise VWaterError(msg)
        return inner

    def raw(self, cmd, extra=None):
        """返回原始 dict，不做 result 校验。"""
        return self.invoke(cmd, extra)


# ---------------- 具体功能 ----------------

def balance(s):
    d = s.invoke('zfbgetcustoddfare', {'outid': s.cfg['outid'],
                                      'timestamp': time.strftime('%Y%m%d%H%M%S')})
    try:
        return float(d.get('oddfare') or 0)
    except Exception:
        return 0.0


def limits(s):
    out = {}
    for lt in (1, 2, 3):
        d = s.invoke('zfbquerylimit', {'outid': s.cfg['outid'], 'limittype': lt,
                                        'timestamp': time.strftime('%Y%m%d%H%M%S')})
        if d.get('data'):
            out[str(lt)] = d['data']
    return out


# limittype: 1 = 预约洗澡(bath/学校浴室)  2 = 打热水(hotwater)  3 = 宿舍淋浴(shower)
LIMIT_BATH = 1


def get_limit(s, limittype=LIMIT_BATH):
    d = s.invoke('zfbquerylimit', {'outid': s.cfg['outid'], 'limittype': limittype,
                                    'timestamp': time.strftime('%Y%m%d%H%M%S')})
    return d.get('data') or {}


def save_limit(s, fare_yuan, time_min, limittype=LIMIT_BATH):
    d = s.invoke('zfbsavelimit', {
        'outid': s.cfg['outid'],
        'timestamp': time.strftime('%Y%m%d%H%M%S'),
        'limittype': limittype,
        'max_fare_limit': int(round(float(fare_yuan) * 100)),
        'max_time_limit': int(time_min),
    })
    return str(d.get('result')) == '0', d.get('message')


def bathrooms(s):
    d = s.invoke('zfbgetbathroomlist', {'outid': s.cfg['outid']})
    data = d.get('data') or {}
    orders = data.get('orderbathroomlist') or []
    now = data.get('nowdate') or (orders[0].get('nowdate') if orders else '') or ''
    return {
        'orders': orders,
        'list': data.get('bathroomlist') or [],
        'oddfare': data.get('oddfare'),
        'serverTime': now,      # HHMMSS，服务端当前时间，用于校时
    }


def cancel(s, orderno):
    d = s.invoke('zfbrevokeorderbathroom', {
        'outid': s.cfg['outid'], 'closetype': 0, 'orderno': orderno,
        'timestamp': time.strftime('%Y%m%d%H%M%S')})
    return d.get('result') == '0'


def book(s, bathroom, order_datetime, verificationtype='2', max_fare=200, max_time=30):
    """提交预约。order_datetime 形如 '2026-11-08 16:05:00'（秒置 0）。"""
    d = s.invoke('zfbskorderbathroom', {
        'orderno': '',
        'bathroomcode': bathroom['bathroomcode'],
        'outid': s.cfg['outid'],
        'orderdatetime': order_datetime,
        'verificationtype': verificationtype,
        'timetype': '0',
        'ecard_customerid': s.cfg['customerid'],
        'verificationtelephone': s.cfg['outid'] if verificationtype == '2' else s.cfg.get('mobile', ''),
        'max_fare_limit': max_fare,
        'max_time_limit': max_time,
        'timestamp': time.strftime('%Y%m%d%H%M%S'),
        'ordertype': 0,
    })
    return d


def records(s, month=None, page=1, rows=20):
    extra = {'outid': s.cfg['outid'], 'page_index': page, 'page_rows': rows,
             'timestamp': time.strftime('%Y%m%d%H%M%S')}
    if month:
        extra['month'] = month
    return s.invoke('getwaterconsume', extra)


def server_now(s):
    """取服务端当前时间（本地时区），返回 datetime。"""
    import datetime
    b = bathrooms(s)
    st = b.get('serverTime') or ''
    if len(st) >= 6:
        try:
            hh, mm, ss = int(st[0:2]), int(st[2:4]), int(st[4:6])
            now = datetime.datetime.now()
            return now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        except Exception:
            pass
    return datetime.datetime.now()
