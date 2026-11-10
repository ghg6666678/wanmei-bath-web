"""statebox 单元测试：往返、篡改、密钥不一致、压缩率。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statebox

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


def main():
    key = bytes(range(32))
    key2 = bytes(range(1, 33))
    box = statebox.StateBox(key)

    print('[1] 基本往返')
    payload = {
        'c': {'token': 'x' * 36, 'customerCode': 'C1', 'customerid': 'E1',
              'outid': '20230001', 'name': '张三', 'school': '某大学'},
        'u': '9f2c8e14b7a34d6f8c1e5a2b6d7f8e9c',
        'p': 'https://example.com/h5/index?uuid=9f2c&source=wx&tVer=20260918',
    }
    tok = box.seal(payload)
    back = box.open(tok)
    check('解出来与原文一致', back == payload)
    check('是字符串', isinstance(tok, str))
    check('URL 安全字符', all(ch.isalnum() or ch in '-_' for ch in tok),
          '异常字符: %r' % [ch for ch in tok if not (ch.isalnum() or ch in '-_')][:5])
    print('      原始 JSON %d 字节 → cookie %d 字节（压缩率 %.0f%%）'
          % (len(json.dumps(payload, ensure_ascii=False).encode()), len(tok),
             100.0 * len(tok) / len(json.dumps(payload, ensure_ascii=False).encode())))

    print('[2] 篡改检测')
    for i in [0, 5, len(tok) // 2, len(tok) - 1]:
        t = tok[:i] + ('A' if tok[i] != 'A' else 'B') + tok[i + 1:]
        try:
            box.open(t)
            check('改第 %d 位应失败' % i, False)
        except statebox.StateError:
            check('改第 %d 位被拒' % i, True)

    print('[3] 换密钥')
    try:
        statebox.StateBox(key2).open(tok)
        check('换密钥应失败', False)
    except statebox.StateError:
        check('换密钥被拒', True)

    print('[4] 异常输入')
    for name, val in [('空串', ''), ('None', None), ('乱码', '!!!not-base64!!!'), ('过短', 'AAAA')]:
        try:
            box.open(val)
            check('%s 应失败' % name, False)
        except statebox.StateError:
            check('%s 被拒' % name, True)
        except Exception as e:
            check('%s 抛了非 StateError（应统一）' % name, False, type(e).__name__)

    print('[5] 空对象与超长内容')
    check('空 dict 往返', box.open(box.seal({})) == {})
    big = {'c': {'token': 'y' * 2000}, 'u': 'z' * 64, 'p': 'w' * 2000}
    bt = box.seal(big)
    check('4KB 级内容仍能往返', box.open(bt) == big)
    check('压缩后 < 2KB（cookie 上限 4KB）', len(bt) < 2048, '实际 %d 字节' % len(bt))

    print('[6] 两次加密结果不同（nonce 随机）')
    a, b = box.seal(payload), box.seal(payload)
    check('密文不重复', a != b)
    check('但解出来相同', box.open(a) == box.open(b))

    print('[7] cookie 解析')
    ck = statebox.parse_cookies('a=1; bath_state=xyz; b=2')
    check('解析出 bath_state', ck.get('bath_state') == 'xyz')
    check('空头返回空 dict', statebox.parse_cookies(None) == {})
    hdr = statebox.build_cookie('k', 'v', max_age=60)
    check('带 HttpOnly', 'HttpOnly' in hdr)
    check('带 SameSite', 'SameSite=Lax' in hdr)
    check('带 Max-Age', 'Max-Age=60' in hdr)

    print('\n通过 %d，失败 %d' % (ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
