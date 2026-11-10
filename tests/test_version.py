"""版本号比较的回归测试。

这个文件存在的原因很实在：`_ver_num` 曾经把元组拼成 (补丁, 次, 主)，
导致 1.1.0 被判成比 1.0.5 小——检查更新永远选不到新版本。
而 1.0.x 之间因为主次版本相同，倒序后恰好仍是对的，所以这个 bug 潜伏了很久，
直到版本号跨到 1.1.0 才暴露。

凡是「看起来显然正确」的比较函数，都应该有一组固定用例盯着。
"""
import os
import sys

WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP)

import updater

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
    print('[1] 次版本跨级（正是当年翻车的场景）')
    check('1.1.0 > 1.0.5', updater._ver_num('1.1.0') > updater._ver_num('1.0.5'))
    check('1.0.5 < 1.1.0', updater._ver_num('1.0.5') < updater._ver_num('1.1.0'))
    check('1.2.0 > 1.10.0 不成立（按数值比）',
          not (updater._ver_num('1.2.0') > updater._ver_num('1.10.0')))

    print('[2] 补丁位')
    check('1.0.10 > 1.0.9', updater._ver_num('1.0.10') > updater._ver_num('1.0.9'))
    check('1.0.9 < 1.0.10', updater._ver_num('1.0.9') < updater._ver_num('1.0.10'))
    check('1.0.5 > 1.0.4', updater._ver_num('1.0.5') > updater._ver_num('1.0.4'))

    print('[3] 主版本')
    check('2.0.0 > 1.9.9', updater._ver_num('2.0.0') > updater._ver_num('1.9.9'))

    print('[4] 相等与缺位')
    check('1.0.5 == 1.0.5', updater._ver_num('1.0.5') == updater._ver_num('1.0.5'))
    check('1.0 == 1.0.0', updater._ver_num('1.0') == updater._ver_num('1.0.0'))
    check('1 == 1.0.0', updater._ver_num('1') == updater._ver_num('1.0.0'))

    print('[5] 异常输入不炸')
    for v in ('', None, 'v', 'abc'):
        try:
            updater._ver_num(v)
            check('%r 不抛异常' % v, True)
        except Exception as e:
            check('%r 不抛异常' % v, False, type(e).__name__)

    print('[6] 带前缀 v')
    check('v1.1.0 > 1.0.5', updater._ver_num('v1.1.0') > updater._ver_num('1.0.5'))

    print('\n通过 %d，失败 %d' % (ok, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
