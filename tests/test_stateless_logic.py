"""不用网络，验证 state_session 的注入逻辑：

- cookie 里带 uuid → 不再调 entrance（省一次上游请求）
- cookie 里不带 uuid → 调一次 entrance，并把新 uuid 写回 cookie
- entrance 回填的字段要覆盖 cookie 里的旧值
"""
import json
import os
import sys

WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP)

os.environ['BATH_STATELESS'] = '1'
os.environ['BATH_STATE_KEY'] = '22' * 32

import server
import statebox
import vwater

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


class FakeHeader(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


class FakeHandler:
    """只实现 _state/_save_state/state_session 用到的那几样。"""
    pass


def make_handler(cookie_value):
    h = server.Handler.__new__(server.Handler)
    h.headers = FakeHeader({'Cookie': 'bath_state=' + cookie_value} if cookie_value else {})
    h._extra_headers = []
    return h


def main():
    calls = {'entrance': 0, 'init_cfg': []}

    real_session = vwater.Session

    class SpySession(real_session):
        def __init__(self, cfg):
            calls['init_cfg'].append(dict(cfg))
            super().__init__(cfg)

        def entrance(self):
            calls['entrance'] += 1
            # 模拟上游：下发 uuid/page，并回填 outid（权威值覆盖本地）
            self.uuid = 'UPSTREAMUUID0001'
            self.page = 'https://h5.example/index?uuid=UPSTREAMUUID0001'
            self.cfg['outid'] = 'SERVER-SAYS-999'

    vwater.Session = SpySession
    box = server.BOX

    cfg = {'token': 'T' * 36, 'customerCode': 'C1', 'customerid': 'E1',
           'outid': 'LOCAL-111', 'name': '甲', 'school': '某大学'}

    print('[1] cookie 自带 uuid → 不调 entrance')
    calls['entrance'] = 0
    h = make_handler(box.seal({'c': cfg, 'u': 'CACHED-UUID', 'p': 'https://h5/x'}))
    s = h.state_session()
    check('uuid 来自 cookie', s.uuid == 'CACHED-UUID', s.uuid)
    check('page 来自 cookie', s.page == 'https://h5/x', s.page)
    check('没有触发 entrance', calls['entrance'] == 0, calls['entrance'])
    check('没有下发 Set-Cookie', not h._extra_headers, h._extra_headers)
    check('outid 保持 cookie 里的值', s.cfg['outid'] == 'LOCAL-111', s.cfg['outid'])

    print('[2] 同一请求内复用，不重复构造')
    s2 = h.state_session()
    check('返回同一个对象', s2 is s)
    check('Session 只构造了一次', len(calls['init_cfg']) == 1, len(calls['init_cfg']))

    print('[3] cookie 没有 uuid → 调一次 entrance 并写回')
    calls['entrance'] = 0
    calls['init_cfg'] = []
    h = make_handler(box.seal({'c': dict(cfg), 'u': None, 'p': ''}))
    s = h.state_session()
    check('触发了一次 entrance', calls['entrance'] == 1, calls['entrance'])
    check('uuid 用上游值', s.uuid == 'UPSTREAMUUID0001', s.uuid)
    check('回写了 Set-Cookie', any('bath_state=' in v for k, v in h._extra_headers), h._extra_headers)

    print('[4] 回写的 cookie 能解出新的 uuid 与上游回填字段')
    raw = [v for k, v in h._extra_headers if 'bath_state=' in v][0]
    val = raw.split('bath_state=', 1)[1].split(';', 1)[0]
    st = box.open(val)
    check('uuid 已更新', st['u'] == 'UPSTREAMUUID0001', st['u'])
    check('page 已更新', st['p'].startswith('https://h5.example'), st['p'])
    check('outid 被上游覆盖', st['c']['outid'] == 'SERVER-SAYS-999', st['c']['outid'])
    check('cookie 里没有明文 token', cfg['token'] not in val)

    print('[5] force=True 强制重换')
    calls['entrance'] = 0
    h = make_handler(box.seal({'c': dict(cfg), 'u': 'OLD', 'p': 'x'}))
    s = h.state_session()
    s2 = h.state_session(force=True)
    check('force 触发了一次 entrance', calls['entrance'] == 1, calls['entrance'])
    check('uuid 被刷新', s2.uuid == 'UPSTREAMUUID0001')

    print('[6] 缺关键字段 → 明确报未登录')
    for miss in ('token', 'customerCode', 'customerid', 'outid'):
        c = dict(cfg)
        c.pop(miss)
        h = make_handler(box.seal({'c': c, 'u': 'x', 'p': ''}))
        try:
            h.state_session()
            check('缺 %s 应抛 NotLoggedIn' % miss, False)
        except server.NotLoggedIn:
            check('缺 %s 抛 NotLoggedIn' % miss, True)

    print('[7] 无 cookie → 未登录')
    h = make_handler(None)
    try:
        h.state_session()
        check('应抛 NotLoggedIn', False)
    except server.NotLoggedIn:
        check('抛 NotLoggedIn', True)

    print('[8] current_cfg 与 _public_config 一致性')
    h = make_handler(box.seal({'c': dict(cfg), 'u': 'x', 'p': ''}))
    pub = server._public_config(h.current_cfg())
    check('hasToken 为 True', pub['hasToken'] is True)
    check('不含 token 字段', 'token' not in pub)
    check('姓名正确', pub['name'] == '甲')

    print('\n通过 %d，失败 %d' % (ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
