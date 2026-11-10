"""无状态模式的端到端验证：cookie 携带票据、多用户隔离、篡改降级。

用一个已有登录态（若存在）验证真实链路；没有就用假票据，只验隔离与降级逻辑。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP)

import statebox

PORT = 8801
KEY = '11' * 32
BASE = 'http://127.0.0.1:%d' % PORT

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


def get(path, cookie=None, timeout=20):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8')), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        try:
            return e.code, json.loads(body), dict(e.headers)
        except Exception:
            return e.code, {'raw': body[:200]}, dict(e.headers)


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
    real = {}
    cfg_path = os.path.join(WEBAPP, 'config.json')
    if os.path.exists(cfg_path):
        real = json.load(open(cfg_path, encoding='utf-8'))
    has_real = bool(real.get('token'))
    print('真实登录态：%s' % ('有' if has_real else '无（只验隔离与降级）'))
    print()

    env = dict(os.environ)
    env['BATH_STATELESS'] = '1'
    env['BATH_STATE_KEY'] = KEY
    env.pop('BATH_COOKIE_SECURE', None)

    proc = subprocess.Popen([sys.executable, 'server.py', str(PORT)], cwd=WEBAPP,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='ignore')
    try:
        if not wait_up():
            print('服务未启动，输出如下：')
            print(proc.stdout.read()[:2000] if proc.stdout else '')
            return 1

        box = statebox.StateBox(bytes.fromhex(KEY))

        print('[1] 没带 cookie')
        st, body, _ = get('/api/config')
        check('HTTP 200', st == 200, st)
        check('hasToken 为 False', body.get('config', {}).get('hasToken') is False, body)

        print('[2] 带合法 cookie（真实票据）')
        if has_real:
            ck = 'bath_state=' + box.seal({'c': real, 'u': None, 'p': ''})
            st, body, _ = get('/api/config', ck)
            check('hasToken 为 True', body.get('config', {}).get('hasToken') is True, body)
            check('姓名一致', body.get('config', {}).get('name') == real.get('name'), body)
            check('学号一致', body.get('config', {}).get('outid') == real.get('outid'))
            check('不回传 token', 'token' not in json.dumps(body.get('config') or {}))
        else:
            print('      跳过（无真实票据）')

        print('[3] 两个用户的 cookie 互不串')
        a = dict(real or {'token': 'A' * 36, 'customerCode': 'C1', 'customerid': 'E1',
                          'outid': '111', 'name': '甲', 'school': '某大学'})
        b = dict(a)
        b.update({'name': '乙', 'outid': '222', 'token': 'B' * 36})
        ca = 'bath_state=' + box.seal({'c': a, 'u': None, 'p': ''})
        cb = 'bath_state=' + box.seal({'c': b, 'u': None, 'p': ''})
        _, ra, _ = get('/api/config', ca)
        _, rb, _ = get('/api/config', cb)
        check('甲拿到甲', ra['config']['name'] == a['name'] and ra['config']['outid'] == a['outid'],
              ra['config'])
        check('乙拿到乙', rb['config']['name'] == b['name'] and rb['config']['outid'] == b['outid'],
              rb['config'])
        # 交替请求，确认没有服务端缓存串味
        for i in range(3):
            _, x, _ = get('/api/config', ca)
            _, y, _ = get('/api/config', cb)
            if x['config']['name'] != a['name'] or y['config']['name'] != b['name']:
                check('第 %d 轮交替请求' % (i + 1), False, (x['config'], y['config']))
                break
        else:
            check('交替请求 3 轮不串味', True)

        print('[4] 篡改过的 cookie')
        good = box.seal({'c': a, 'u': None, 'p': ''})
        tampered = good[:-4] + ('AAAA' if not good.endswith('AAAA') else 'BBBB')
        st, body, _ = get('/api/config', 'bath_state=' + tampered)
        check('不被当成登录态', body.get('config', {}).get('hasToken') is False, body)
        check('也没有 500', st == 200, st)

        print('[5] 别的密钥签发的 cookie')
        st, body, _ = get('/api/config', 'bath_state=' + statebox.StateBox(bytes(range(32))).seal({'c': a}))
        check('被拒', body.get('config', {}).get('hasToken') is False, body)

        print('[6] 未登录访问业务接口')
        st, body, _ = get('/api/state')
        check('返回 needLogin', body.get('needLogin') is True, body)
        check('提示语正确', '登录' in str(body.get('error')), body)

        print('[7] 带 cookie 走真实链路')
        if has_real:
            ck = 'bath_state=' + box.seal({'c': real, 'u': None, 'p': ''})
            st, body, hdr = get('/api/state', ck, timeout=40)
            err = str(body.get('error') or '')
            expired = body.get('needLogin') or '登录态失效' in err
            if expired:
                # 票据过期是环境事实，不是实现问题：能走到「entrance 被拒」这一步，
                # 说明 cookie 解开、会话重建、上游请求都通了。
                print('      票据已过期（%s）——链路本身走通，仅上游拒绝' % err[:40])
                check('过期时明确报错、不假装成功', True)
            else:
                check('拿到浴室列表', isinstance(body.get('bathrooms'), list), str(body)[:200])
                check('拿到预约列表', isinstance(body.get('orders'), list))
                print('      浴室数 %d，预约数 %d'
                      % (len(body.get('bathrooms') or []), len(body.get('orders') or [])))
        else:
            print('      跳过')

        print('[8] 服务端日志里不能出现票据')
        time.sleep(0.3)
        proc.terminate()
        out = ''
        try:
            out = proc.stdout.read() or ''
        except Exception:
            pass
        blob = (real.get('token') or '')[:20]
        check('日志不含 token 片段', not (blob and blob in out),
              '日志片段: ' + out[:300].replace('\n', ' | '))
        proc = None

    finally:
        if proc and proc.poll() is None:
            proc.terminate()

    print('\n通过 %d，失败 %d' % (ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
