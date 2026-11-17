"""2026-11-15 事故验收：数据隔离（两个独立 cookie jar + 一个未登录访客）。

事故路径是「没有 cookie 的访客直接 GET /api/state 就拿到了别人的姓名、
完整手机号、学号、余额、用水记录」。因此这里不测「能不能登录」，只测隔离：

  A —— 独立 cookie jar，票据属于甲
  B —— 独立 cookie jar，票据属于乙
  C —— 独立 cookie jar，什么都没有（模拟舍友/路人第一次打开链接）

不依赖真实账号与短信验证码：两份票据用 statebox.seal() 预置，等价于
登录成功后服务端 Set-Cookie 的结果。全是本地请求，不打完美校园。

关键：**不设任何会话模式环境变量**，用来验证「默认就是无状态」——
事故的根因正是默认值曾经站在单人模式那一侧。

用法：python tests/test_isolation_2026_11_15.py
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP)

import statebox  # noqa: E402

PORT = 8802
KEY = '22' * 32
BASE = 'http://127.0.0.1:%d' % PORT

A = {
    'token': 'A' * 36, 'customerCode': 'CUS-A', 'customerid': 'EMP-A',
    'outid': '20230001', 'name': '甲同学', 'school': '测试大学',
    'mobile': '13800138000',
}
B = {
    'token': 'B' * 36, 'customerCode': 'CUS-B', 'customerid': 'EMP-B',
    'outid': '20230002', 'name': '乙同学', 'school': '测试大学',
    'mobile': '13900139000',
}

ok = 0
bad = 0


def check(name, cond, extra=''):
    global ok, bad
    if cond:
        ok += 1
        print('  OK   %s' % name)
    else:
        bad += 1
        print('  BAD  %s %s' % (name, extra))


class Client:
    """一个独立的浏览器：自己的 cookie jar，自己的 opener。"""

    def __init__(self, label, state=None):
        self.label = label
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.cookie = None
        if state is not None:
            box = statebox.StateBox(bytes.fromhex(KEY))
            self.cookie = statebox.COOKIE + '=' + box.seal(state)

    def get(self, path, timeout=10):
        req = urllib.request.Request(BASE + path)
        if self.cookie:
            req.add_header('Cookie', self.cookie)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {'raw': body[:200]}


def wait_up(timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + '/api/config', timeout=3):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    env = dict(os.environ)
    # 关键：两个模式变量都不设，验证默认走无状态。
    env.pop('BATH_SINGLE', None)
    env.pop('BATH_STATELESS', None)
    env.pop('BATH_COOKIE_SECURE', None)
    env['BATH_STATE_KEY'] = KEY

    proc = subprocess.Popen(
        [sys.executable, 'server.py', str(PORT)], cwd=WEBAPP, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='ignore')
    out = ''
    try:
        if not wait_up():
            print('服务未启动，输出如下：')
            print(proc.stdout.read()[:2000] if proc.stdout else '')
            return 1

        print('[0] 默认值站在安全侧（事故根因）')
        os.environ.setdefault('BATH_STATE_KEY', KEY)
        import server as _srv
        check('默认 STATELESS=True', _srv.STATELESS is True)
        check('默认 SINGLE=False', _srv.SINGLE is False)

        box = statebox.StateBox(bytes.fromhex(KEY))
        a = Client('A（甲）', {'c': A, 'u': None, 'p': ''})
        b = Client('B（乙）', {'c': B, 'u': None, 'p': ''})
        c = Client('C（未登录访客）')

        print('[1] 三个客户端互相独立')
        check('A/B/C 是三个不同的 cookie jar',
              a.jar is not b.jar and b.jar is not c.jar)

        print('[2] 事故路径：未登录访客访问业务接口')
        st, body = c.get('/api/state')
        check('返回 needLogin', body.get('needLogin') is True, body)
        check('提示语是「还没有登录」', '还没有登录' in str(body.get('error') or ''), body)
        blob = json.dumps(body, ensure_ascii=False)
        leaked = [v for v in (A['name'], B['name'], A['outid'], B['outid'],
                              A['mobile'], B['mobile'], A['token']) if v in blob]
        check('响应里没有甲/乙的任何身份信息', not leaked, '泄漏字段: %s' % leaked)
        check('没有 balance / orders / bathrooms',
              not any(k in body for k in ('balance', 'orders', 'bathrooms')), body)

        print('[3] 未登录访客读 config')
        st, body = c.get('/api/config')
        cfg = body.get('config') or {}
        check('hasToken 为 False', cfg.get('hasToken') is False, cfg)
        check('没有 name / outid / school',
              not any(cfg.get(k) for k in ('name', 'outid', 'school')), cfg)
        check('没有 phone / mobile',
              not any(cfg.get(k) for k in ('phone', 'mobile')), cfg)

        print('[4] 甲只能看到甲，乙只能看到乙')
        _, ra = a.get('/api/config')
        _, rb = b.get('/api/config')
        ca, cb = ra.get('config') or {}, rb.get('config') or {}
        check('A 拿到甲的姓名与学号',
              ca.get('name') == A['name'] and ca.get('outid') == A['outid'], ca)
        check('B 拿到乙的姓名与学号',
              cb.get('name') == B['name'] and cb.get('outid') == B['outid'], cb)
        check('A 的响应里没有乙的任何字段',
              B['name'] not in json.dumps(ra, ensure_ascii=False)
              and B['outid'] not in json.dumps(ra, ensure_ascii=False), ra)
        check('B 的响应里没有甲的任何字段',
              A['name'] not in json.dumps(rb, ensure_ascii=False)
              and A['outid'] not in json.dumps(rb, ensure_ascii=False), rb)

        print('[5] 手机号一律掩码，服务端不下发完整号码')
        check('A 的 phone 是掩码', ca.get('phone') == '138****8000', ca.get('phone'))
        check('B 的 phone 是掩码', cb.get('phone') == '139****9000', cb.get('phone'))
        check('A/B 的响应不含完整手机号',
              A['mobile'] not in json.dumps(ra, ensure_ascii=False)
              and B['mobile'] not in json.dumps(rb, ensure_ascii=False))
        check('不回传 token',
              'token' not in json.dumps(ra, ensure_ascii=False)
              and 'token' not in json.dumps(rb, ensure_ascii=False))

        print('[6] 交替请求 5 轮不串味')
        mixed = False
        for _ in range(5):
            _, x = a.get('/api/config')
            _, y = b.get('/api/config')
            _, z = c.get('/api/config')
            if ((x.get('config') or {}).get('name') != A['name']
                    or (y.get('config') or {}).get('name') != B['name']
                    or (z.get('config') or {}).get('hasToken') is not False):
                mixed = True
                break
        check('交替请求始终各自独立', not mixed)

        print('[7] 篡改过的 cookie 不能冒充任何账号')
        good = box.seal({'c': A, 'u': None, 'p': ''})
        bad_ck = good[:-4] + ('AAAA' if not good.endswith('AAAA') else 'BBBB')
        d = Client('D（篡改）')
        d.cookie = statebox.COOKIE + '=' + bad_ck
        st, body = d.get('/api/config')
        check('不被当成登录态', (body.get('config') or {}).get('hasToken') is False, body)
        check('也没有 500', st == 200, st)

        print('[8] 服务端不保存任何账号数据')
        cfg_path = os.path.join(WEBAPP, 'config.json')
        disk = ''
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding='utf-8') as f:
                disk = f.read()
        check('无状态启动没有写入票据',
              A['token'] not in disk and B['token'] not in disk)

        time.sleep(0.3)
        proc.terminate()
        try:
            out = proc.stdout.read() or ''
        except Exception:
            pass
        proc = None
        # 子进程的启动日志是块缓冲，terminate() 时可能没 flush 出来，
        # 所以不拿它做断言；默认值已经在 [0] 里直接查过常量了。
        check('日志没有出现单人模式警告', '!! 单人模式' not in out)
        check('日志不含完整手机号',
              A['mobile'] not in out and B['mobile'] not in out)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()

    print('\n通过 %d，失败 %d' % (ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
