"""洗澡助手后端 —— 完美校园「智慧用水·洗澡」精简客户端。

除 pycryptodome 外只用标准库（签名与 cookie 加密都靠它）。启动：
    python server.py [端口]
默认 http://0.0.0.0:8770

两种会话模式
------------
单人自用（默认）：票据落在 config.json，进程里维护一份 Session。简单、重启免登录，
                  但只能一个人用——换人登录会覆盖前一个。
无状态多用户：    设 BATH_STATELESS=1，票据加密后放进浏览器 cookie，服务端不保存任何
                  用户数据、每个请求自带凭据，可以安全地给多个使用者用。
                  说明见 docs/STATELESS.md。
"""
import datetime
import json
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import statebox
import vwater

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
# 合并部署：官网静态页的快照目录（由 build 步骤从安卓仓库 site/ 复制而来）。
# 没有这个目录时（本地开发）一律回落到 web/，原行为不变。
SITE = os.path.join(HERE, 'site')
SITE_EXISTS = os.path.isdir(SITE)
CONFIG_PATH = os.path.join(HERE, 'config.json')

_lock = threading.Lock()
_session = None
_config = {}

# 无状态模式：开了才加载（否则不碰密钥文件）
STATELESS = statebox.mode_enabled()
BOX = statebox.box() if STATELESS else None

# 登录握手：sendCode 与 doLogin 必须复用同一个 WxLogin 实例（session 要一致）。
# 原来是一个全局变量，两个人同时登录会互相踩——这里改成按临时标识分开存。
# 它只活在内存里、带 TTL、登录完成即丢，不落盘、不写日志。
_pending_lock = threading.Lock()
_PENDING = {}
_PENDING_TTL = 300


def _wx_new():
    import wxlogin
    w = wxlogin.WxLogin(host=wxlogin.HOSTS[0])
    w.exchange_secret()
    return w


def _pending_put(sid, w):
    now = time.time()
    with _pending_lock:
        for k in [k for k, v in _PENDING.items() if now - v[1] > _PENDING_TTL]:
            _PENDING.pop(k, None)
        _PENDING[sid] = (w, now)


def _pending_get(sid):
    if not sid:
        return None
    with _pending_lock:
        item = _PENDING.get(sid)
    if not item:
        return None
    w, ts = item
    return w if time.time() - ts <= _PENDING_TTL else None


def _pending_pop(sid):
    with _pending_lock:
        return _PENDING.pop(sid, None)


def _pending_take(sid):
    """取走并删除：登录用掉就作废，不能重复使用。"""
    with _pending_lock:
        item = _PENDING.pop(sid, None)
    if not item:
        return None
    w, ts = item
    return w if time.time() - ts <= _PENDING_TTL else None


def _login_config(w, user, phone):
    def pick(*keys):
        for k in keys:
            v = user.get(k)
            if v not in (None, ''):
                return str(v)
        return ''
    return {
        'token': w.session_id,
        'customerCode': w.customer_id or pick('customId'),
        'customerid': w.ecard_customer_id or pick('ecardCustomerid'),
        'outid': pick('authStuNo', 'userName', 'account'),
        'name': pick('name', 'userName'),
        'school': pick('customName_', 'schoolName', 'customerName'),
        'mobile': phone,
    }


def load_config():
    global _config
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8') as f:
            _config = json.load(f)
    else:
        _config = {}
    return _config


def save_config(cfg):
    global _config
    _config = cfg
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class NotLoggedIn(Exception):
    pass


def _check_login_fields(cfg):
    """登录后必填的四个字段。

    缺 token / customerCode 是登录本身没成功；
    缺 customerid（一卡通客户号）或 outid（学号）说明这个手机号在官方那边
    还没绑过校园卡 —— 这是两回事，要分开提示，否则用户不知道该去做什么。
    """
    if not (cfg.get('token') and cfg.get('customerCode')):
        # 登录本身没成功（或压根没登录）——就是「还没登录」
        raise NotLoggedIn('还没有登录，请用手机号登录')
    # 登录成功了，但缺一卡通客户号或学号 = 官方那边这个号还没绑校园卡
    if not (cfg.get('customerid') and cfg.get('outid')):
        raise NotLoggedIn(
            '这个账号还没绑定校园卡。请先用微信小程序「完美校园」绑定校园卡'
            '（微信搜「完美校园」小程序 → 选学校 → 首页点「绑定校园卡」），'
            '或装官方 App 绑定后再回来登录。')


def get_session(handler=None, force=False):
    """取当前请求可用的会话。

    单人模式：进程里一份，落盘在 config.json。
    无状态模式：从请求自带的 cookie 解出来，每次重建——所以绝对不能有全局缓存，
              否则就成了「一份凭据给所有人用」。
    """
    if STATELESS:
        if handler is None:
            raise RuntimeError('无状态模式下必须传 handler，会话从 cookie 取')
        return handler.state_session(force)

    global _session
    with _lock:
        _check_login_fields(_config)
        if _session is None or force:
            _session = vwater.Session(_config)
            _session.entrance()
        return _session


class Handler(BaseHTTPRequestHandler):
    server_version = 'BathHelper/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    # ---------- helpers ----------
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in getattr(self, '_extra_headers', None) or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, obj)

    # ---------- cookie 与会话状态（无状态模式）----------
    def _hdr(self, name, value):
        """追加响应头。懒初始化，避免在没设过 header 的分支里 AttributeError。"""
        if getattr(self, '_extra_headers', None) is None:
            self._extra_headers = []
        self._extra_headers.append((name, value))

    def _cookies(self):
        return statebox.parse_cookies(self.headers.get('Cookie'))

    def _state(self):
        """解出浏览器带来的那坨密文。解不开就当没登录，不要抛 500。"""
        raw = self._cookies().get(statebox.COOKIE)
        if not raw:
            return None
        try:
            return BOX.open(raw)
        except statebox.StateError:
            return None

    def _save_state(self, cfg, uuid=None, page=''):
        """把票据封进 cookie 交还浏览器——服务端不留副本。"""
        blob = BOX.seal({'c': cfg, 'u': uuid, 'p': page or ''})
        self._hdr('Set-Cookie',
                  statebox.build_cookie(statebox.COOKIE, blob, max_age=7 * 24 * 3600))
        self._cfg_cache = dict(cfg)

    def _drop_state(self):
        self._hdr('Set-Cookie', statebox.clear_cookie(statebox.COOKIE))
        self._cfg_cache = {}

    def current_cfg(self):
        """当前请求对应的账号配置：无状态取 cookie，单人模式取全局。"""
        if not STATELESS:
            return _config
        if getattr(self, '_cfg_cache', None) is not None:
            return self._cfg_cache
        st = self._state()
        return dict(st.get('c') or {}) if st else {}

    def state_session(self, force=False):
        cached = getattr(self, '_sess', None)
        if cached is not None and not force:
            return cached
        st = self._state()
        if not st:
            raise NotLoggedIn('还没有登录，请用手机号登录')
        cfg = dict(st.get('c') or {})
        _check_login_fields(cfg)
        s = vwater.Session(cfg)
        s.uuid = st.get('u')
        s.page = st.get('p') or ''
        if force or not s.uuid:
            s.entrance()
            # entrance 会刷掉 uuid/page，也可能回填服务端下发的 id，需要写回 cookie
            self._save_state(s.cfg, s.uuid, s.page)
        self._sess = s
        return s

    # ---------- 网页端登录/退出：nativeCall 的浏览器回落 ----------
    def _native(self, body):
        global _session
        name = str(body.get('name') or '').strip()
        args = body.get('args') or []
        sid = self._cookies().get(statebox.PENDING_COOKIE) or ''

        if name == 'logout':
            if STATELESS:
                self._drop_state()
            else:
                with _lock:
                    cfg = dict(_config)
                    for k in ('token', 'customerCode', 'customerid', 'outid',
                              'name', 'school', 'mobile', 'phone'):
                        cfg.pop(k, None)
                    save_config(cfg)
                    _session = None
            if sid:
                _pending_pop(sid)
            self._hdr('Set-Cookie', statebox.clear_cookie(statebox.PENDING_COOKIE))
            return {'ok': True, 'data': {}}

        if name == 'sendCode':
            phone = str(args[0]) if args else ''
            if len(phone) != 11 or not phone.isdigit():
                return {'ok': False, 'error': '手机号格式不对'}
            # 同一个浏览器重发验证码时复用握手会话，别每次重新换密钥
            w = _pending_get(sid)
            if w is None:
                sid = secrets.token_hex(16)
                try:
                    w = _wx_new()
                except Exception as e:
                    return {'ok': False, 'error': '初始化登录失败: %s' % e}
            try:
                r = w.send_code(phone)
                if str(r.get('code_')) != '0':
                    _pending_pop(sid)
                    return {'ok': False, 'error': r.get('message_') or '验证码发送失败'}
                _pending_put(sid, w)
                self._hdr('Set-Cookie', statebox.build_cookie(
                    statebox.PENDING_COOKIE, sid, max_age=_PENDING_TTL))
                return {'ok': True, 'data': {}}
            except Exception as e:
                _pending_pop(sid)
                return {'ok': False, 'error': '发送失败: %s' % e}

        if name == 'doLogin':
            phone = str(args[0]) if args else ''
            code = str(args[1]) if len(args) > 1 else ''
            if not phone or not code:
                return {'ok': False, 'error': '缺少手机号或验证码'}
            w = _pending_take(sid)      # 验证码是一次性的，用掉就不能再用
            if w is None:
                return {'ok': False, 'error': '登录会话已过期，请重新获取验证码'}
            try:
                user = w.login_sms(phone, code)
                cfg = dict(self.current_cfg())
                cfg.update(_login_config(w, user, phone))
                if STATELESS:
                    self._save_state(cfg)
                else:
                    save_config(cfg)
                    with _lock:
                        _session = None
                    try:
                        get_session(force=True)      # 尽早发现问题，失败不影响已保存的票据
                    except Exception:
                        pass
                self._hdr('Set-Cookie', statebox.clear_cookie(statebox.PENDING_COOKIE))
                return {'ok': True, 'data': {}}
            except Exception as e:
                return {'ok': False, 'error': '登录失败: %s' % e}

        return {'ok': False, 'error': '不支持的调用: %s' % name}

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return dict(urllib.parse.parse_qsl(raw.decode('utf-8')))

    def _static(self, path):
        if path == '/' or not path:
            path = '/index.html'
        if path == '/web':
            path = '/web/'
        # 一个服务同时托管两份页面：/web/ 是网页端，其余是官网（合并部署时）。
        # 没有官网快照时全部走 web/，保持本地开发的原行为。
        if path.startswith('/web/'):
            root, rel = WEB, path[len('/web/'):] or 'index.html'
        elif SITE_EXISTS:
            root, rel = SITE, path.lstrip('/')
        else:
            root, rel = WEB, path.lstrip('/')
        p = os.path.normpath(os.path.join(root, rel))
        if not p.startswith(root) or not os.path.isfile(p):
            return self._send(404, 'not found', 'text/plain; charset=utf-8')
        ext = os.path.splitext(p)[1].lower()
        ctype = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
                 '.png': 'image/png', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json',
                 '.apk': 'application/vnd.android.package-archive'}.get(
            ext, 'application/octet-stream')
        with open(p, 'rb') as f:
            self._send(200, f.read(), ctype)

    # ---------- routes ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p.startswith('/api/'):
                return self.api_get(p, dict(urllib.parse.parse_qsl(u.query)))
            return self._static(p)
        except NotLoggedIn as e:
            return self._json({'ok': False, 'needLogin': True, 'error': str(e)}, 200)
        except vwater.VWaterError as e:
            return self._json({'ok': False, 'error': str(e)}, 502)
        except Exception as e:
            traceback.print_exc()
            return self._json({'ok': False, 'error': '服务异常: %s' % e}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p.startswith('/api/'):
                return self.api_post(p, self._body())
            return self._send(404, 'not found', 'text/plain')
        except NotLoggedIn as e:
            return self._json({'ok': False, 'needLogin': True, 'error': str(e)}, 200)
        except vwater.VWaterError as e:
            return self._json({'ok': False, 'error': str(e)}, 502)
        except Exception as e:
            traceback.print_exc()
            return self._json({'ok': False, 'error': '服务异常: %s' % e}, 500)

    # ---------- API ----------
    def api_get(self, p, q):
        if p == '/api/config':
            return self._json({'ok': True, 'config': _public_config(self.current_cfg())})

        if p == '/api/time':
            s = get_session(self)
            now = vwater.server_now(s)
            return self._json({'ok': True, 'serverTime': now.strftime('%Y-%m-%d %H:%M:%S'),
                               'localTime': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

        if p == '/api/state':
            s = get_session(self)
            b = vwater.bathrooms(s)
            try:
                bal = vwater.balance(s)
            except Exception:
                bal = None
            lim = {}
            try:
                cur = vwater.get_limit(s)
                lim = {'max_fare_limit': cur.get('max_fare_limit'),
                       'max_time_limit': cur.get('max_time_limit')}
            except Exception:
                pass
            return self._json({'ok': True, 'config': _public_config(self.current_cfg()), 'balance': bal,
                               'orders': b['orders'], 'bathrooms': b['list'],
                               'serverTime': b['serverTime'], 'limits': lim,
                               'srvClock': vwater.server_clock(), 'settings': lim})

        if p == '/api/records':
            s = get_session(self)
            r = vwater.records(s, month=q.get('month') or None,
                               page=int(q.get('page') or 1))
            return self._json({'ok': True, 'data': r.get('data')})

        if p == '/api/settings':
            s = get_session(self)
            lim = vwater.get_limit(s)
            return self._json({'ok': True, 'settings': {
                'max_fare_limit': lim.get('max_fare_limit'),
                'max_time_limit': lim.get('max_time_limit'),
            }})

        return self._json({'ok': False, 'error': 'unknown api'}, 404)

    def api_post(self, p, body):
        if p == '/api/native':
            return self._json(self._native(body))

        if p == '/api/config':
            cfg = dict(self.current_cfg())
            cfg.update({k: v for k, v in body.items() if v})
            if STATELESS:
                # 手工填的 token 同样只交给浏览器保管，服务端不留
                self._save_state(cfg)
            else:
                save_config(cfg)
            try:
                get_session(self, force=True)
            except Exception as e:
                return self._json({'ok': False, 'error': '配置已保存，但登录态校验失败: %s' % e}, 200)
            return self._json({'ok': True, 'config': _public_config(self.current_cfg())})

        if p == '/api/book':
            s = get_session(self)
            code = body.get('bathroomcode')
            dt = body.get('datetime')
            if not code or not dt:
                return self._json({'ok': False, 'error': '缺少参数'}, 400)

            # 服务端不校验预约时间是否早于当前（原版的报错其实是它前端自己抛的），
            # 这里替它把关，避免生成过期订单。
            try:
                target = datetime.datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return self._json({'ok': False, 'error': '时间格式不对'}, 400)

            b = vwater.bathrooms(s)
            srv_now = _server_dt(b.get('serverTime'))
            if target < srv_now - datetime.timedelta(minutes=2):
                return self._json({
                    'ok': False,
                    'error': '预约时间早于服务器当前时间（%s），请往后选' % srv_now.strftime('%H:%M'),
                }, 200)
            close = (b.get('list') and next(
                (x.get('businessclosetime') for x in b['list'] if x['bathroomcode'] == code), None)) or '23:59:59'
            if target.strftime('%H:%M:%S') > close:
                return self._json({'ok': False,
                                   'error': '超出营业时间（%s 关门）' % close}, 200)

            room = next((x for x in b['list'] if x['bathroomcode'] == code), None)
            order = next((x for x in b['orders'] if x.get('bathroomcode') == code), None)
            src = order or room
            if not src:
                return self._json({'ok': False, 'error': '找不到该浴室'}, 404)
            # 用用户在「我的」里设置的限时/限额，而不是浴室列表里的默认值
            try:
                lim = vwater.get_limit(s)
                max_fare = int(lim.get('max_fare_limit') or src.get('max_fare_limit') or 200)
                max_time = int(lim.get('max_time_limit') or src.get('max_time_limit') or 30)
            except Exception:
                max_fare = int(src.get('max_fare_limit') or 200)
                max_time = int(src.get('max_time_limit') or 30)
            res = vwater.book(s, src, dt,
                              verificationtype=body.get('verificationtype') or '2',
                              max_fare=max_fare, max_time=max_time)
            ok = str(res.get('result')) == '0'
            data = res.get('data') or {}
            return self._json({'ok': ok,
                               'message': res.get('message') or ('预约成功' if ok else '预约失败'),
                               'data': data,
                               'verificationcode': data.get('verificationcode'),
                               'orderdatetime': data.get('orderdatetime') or dt})

        if p == '/api/cancel':
            s = get_session(self)
            orderno = body.get('orderno')
            if not orderno:
                return self._json({'ok': False, 'error': '缺少 orderno'}, 400)
            ok = vwater.cancel(s, orderno)
            return self._json({'ok': ok, 'message': '已取消预约' if ok else '取消失败'})

        if p == '/api/settings':
            s = get_session(self)
            fare = body.get('fare')
            tmin = body.get('time')
            if fare is None or tmin is None:
                return self._json({'ok': False, 'error': '缺少参数'}, 400)
            try:
                fen = int(round(float(fare) * 100))   # 统一用「分」做整数判断
                tmin = int(tmin)
            except (TypeError, ValueError):
                return self._json({'ok': False, 'error': '参数格式不对'}, 200)
            if not (1 <= tmin <= 120):
                return self._json({'ok': False, 'error': '限时需在 1–120 分钟'}, 200)
            if fen < 100 or (fen > 2000 and fen != 500000):
                return self._json({'ok': False, 'error': '限额需在 1.00–20.00 元，或选择「不限」'}, 200)
            if fen != 500000 and fen % 5 != 0:
                return self._json({'ok': False, 'error': '金额需为 0.05 元的整数倍（如 2.35）'}, 200)
            ok, msg = vwater.save_limit(s, fen / 100.0, tmin)
            return self._json({'ok': ok, 'message': msg or ('已保存' if ok else '保存失败')})

        return self._json({'ok': False, 'error': 'unknown api'}, 404)


def _server_dt(hhmmss):
    """把服务端下发的 HHMMSS 转成本地日期时间的 datetime（日期取本机）。"""
    now = datetime.datetime.now()
    if hhmmss and len(hhmmss) >= 6:
        try:
            return now.replace(hour=int(hhmmss[0:2]), minute=int(hhmmss[2:4]),
                               second=int(hhmmss[4:6]), microsecond=0)
        except Exception:
            pass
    return now


# 官方服务器最近一次响应里的 Date 头（毫秒时间戳）以及记下它时的本机 time.time()。
# Date 是 HTTP 强制响应头，不依赖官方给什么业务字段，是最稳的「服务器当前时刻」。
# 记录动作在 vwater._call 里完成（每次官方请求都会带上这个头），这里只取快照。


def _public_config(cfg=None):
    """下发给前端的账号信息。过滤掉 token，只告诉它「有没有」。"""
    cfg = _config if cfg is None else cfg
    return {
        'name': cfg.get('name'),
        'phone': cfg.get('mobile') or cfg.get('phone'),
        'mobile': cfg.get('mobile') or cfg.get('phone'),
        'outid': cfg.get('outid'),
        'school': cfg.get('school'),
        'customerCode': cfg.get('customerCode'),
        'hasToken': bool(cfg.get('token')),
        # 缺一卡通客户号或学号 = 官方那边还没绑校园卡，前端据此给绑定指引
        'needCard': not (cfg.get('customerid') and cfg.get('outid')),
    }


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('223.5.5.5', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def main():
    # 容器 / WorkBuddy Sites 会注入 PORT；本地仍支持命令行参数与默认 8770
    port = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 8770))
    load_config()
    if STATELESS:
        # 无状态模式下没有「本机登录态」这回事：每个请求自带凭据，启动时无从校验
        print('✔ 无状态模式：票据由浏览器保管，服务端不保存任何账号数据')
        print('  cookie 名称 %s，最大 7 天；换密钥后所有人需重新登录' % statebox.COOKIE)
    elif not _config.get('token'):
        print('!! 未配置 token，请运行 python login.py，或在网页里用手机号登录')
    else:
        try:
            get_session()
            print('✔ 登录态有效：%s · %s' % (_config.get('name'), _config.get('school')))
        except Exception as e:
            print('!! 登录态无效：%s' % e)
            print('   可运行 python login.py 重新登录，或在网页里用手机号登录')
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    ip = _lan_ip()
    print('')
    print('  手机访问（同一 WiFi）: http://%s:%d' % (ip, port))
    print('  本机访问            : http://127.0.0.1:%d' % port)
    print('')
    print('  按 Ctrl+C 停止')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
