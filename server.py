"""洗澡助手后端 —— 完美校园「智慧用水·洗澡」精简客户端。

零第三方依赖，只用标准库。启动：
    python server.py [端口]
默认 http://0.0.0.0:8770
"""
import datetime
import json
import os
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vwater

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
CONFIG_PATH = os.path.join(HERE, 'config.json')

_lock = threading.Lock()
_session = None
_config = {}

# 网页登录：sendCode 与 doLogin 必须复用同一个 WxLogin 实例（session 要一致）
_wx_lock = threading.Lock()
_wx = None


def _wx_new():
    import wxlogin
    w = wxlogin.WxLogin(host=wxlogin.HOSTS[0])
    w.exchange_secret()
    return w


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


def get_session(force=False):
    global _session
    with _lock:
        missing = [k for k in ('token', 'customerCode', 'customerid', 'outid') if not _config.get(k)]
        if missing:
            raise NotLoggedIn('还没有登录，请用手机号登录')
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
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, obj)

    # ---------- 网页端登录/退出：nativeCall 的浏览器回落 ----------
    def _native(self, body):
        global _wx, _session
        name = str(body.get('name') or '').strip()
        args = body.get('args') or []

        if name == 'logout':
            with _lock:
                cfg = dict(_config)
                for k in ('token', 'customerCode', 'customerid', 'outid',
                          'name', 'school', 'mobile', 'phone'):
                    cfg.pop(k, None)
                save_config(cfg)
                _session = None
            with _wx_lock:
                _wx = None
            return {'ok': True, 'data': {}}

        if name == 'sendCode':
            phone = str(args[0]) if args else ''
            if len(phone) != 11 or not phone.isdigit():
                return {'ok': False, 'error': '手机号格式不对'}
            try:
                with _wx_lock:
                    if _wx is None:
                        _wx = _wx_new()
                    r = _wx.send_code(phone)
                if str(r.get('code_')) != '0':
                    return {'ok': False, 'error': r.get('message_') or '验证码发送失败'}
                return {'ok': True, 'data': {}}
            except Exception as e:
                with _wx_lock:
                    _wx = None
                return {'ok': False, 'error': '发送失败: %s' % e}

        if name == 'doLogin':
            phone = str(args[0]) if args else ''
            code = str(args[1]) if len(args) > 1 else ''
            if not phone or not code:
                return {'ok': False, 'error': '缺少手机号或验证码'}
            try:
                with _wx_lock:
                    if _wx is None:
                        _wx = _wx_new()
                    user = _wx.login_sms(phone, code)
                    cfg = dict(_config)
                    cfg.update(_login_config(_wx, user, phone))
                    save_config(cfg)
                try:
                    get_session(force=True)
                except Exception:
                    pass          # 票据已写入，下次请求会重建会话
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
        p = os.path.normpath(os.path.join(WEB, path.lstrip('/')))
        if not p.startswith(WEB) or not os.path.isfile(p):
            return self._send(404, 'not found', 'text/plain; charset=utf-8')
        ext = os.path.splitext(p)[1].lower()
        ctype = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
                 '.png': 'image/png', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json'}.get(
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
            return self._json({'ok': True, 'config': _public_config()})

        if p == '/api/version':
            # 检查更新：服务端代查 GitHub，多通道 + 本地清单 + 缓存兜底
            import updater
            return self._json(updater.get_version_info(force=q.get('force') == '1'))

        if p == '/api/time':
            s = get_session()
            now = vwater.server_now(s)
            return self._json({'ok': True, 'serverTime': now.strftime('%Y-%m-%d %H:%M:%S'),
                               'localTime': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

        if p == '/api/state':
            s = get_session()
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
            return self._json({'ok': True, 'config': _public_config(), 'balance': bal,
                               'orders': b['orders'], 'bathrooms': b['list'],
                               'serverTime': b['serverTime'], 'limits': lim,
                               'settings': lim})

        if p == '/api/records':
            s = get_session()
            r = vwater.records(s, month=q.get('month') or None,
                               page=int(q.get('page') or 1))
            return self._json({'ok': True, 'data': r.get('data')})

        if p == '/api/settings':
            s = get_session()
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
            cfg = dict(_config)
            cfg.update({k: v for k, v in body.items() if v})
            save_config(cfg)
            try:
                get_session(force=True)
            except Exception as e:
                return self._json({'ok': False, 'error': '配置已保存，但登录态校验失败: %s' % e}, 200)
            return self._json({'ok': True, 'config': _public_config()})

        if p == '/api/book':
            s = get_session()
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
            s = get_session()
            orderno = body.get('orderno')
            if not orderno:
                return self._json({'ok': False, 'error': '缺少 orderno'}, 400)
            ok = vwater.cancel(s, orderno)
            return self._json({'ok': ok, 'message': '已取消预约' if ok else '取消失败'})

        if p == '/api/settings':
            s = get_session()
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


def _public_config():    return {
        'name': _config.get('name'),
        'phone': _config.get('mobile') or _config.get('phone'),
        'mobile': _config.get('mobile') or _config.get('phone'),
        'outid': _config.get('outid'),
        'school': _config.get('school'),
        'customerCode': _config.get('customerCode'),
        'hasToken': bool(_config.get('token')),
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    load_config()
    if not _config.get('token'):
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
