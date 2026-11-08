# -*- coding: utf-8 -*-
"""手机号 + 短信验证码登录，把票据写进 config.json。

用法：
    python login.py

登录拿到的 session 就是「智慧用水」H5 需要的 token，
其余字段（学校码、学号、姓名）也一并从登录结果里取。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'config.json')

try:                                    # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import wxlogin                          # noqa: E402
import vwater                           # noqa: E402


def read_cfg():
    if os.path.exists(CFG_PATH):
        try:
            return json.load(open(CFG_PATH, encoding='utf-8'))
        except Exception:
            pass
    return {}


def main():
    print('=' * 46)
    print(' 完美校园 · 智慧用水 登录')
    print('=' * 46)
    print('注意：完美校园是单点登录，这里登录后手机上原版 App 会被踢下线。')
    print()

    phone = input('手机号：').strip()
    if len(phone) != 11 or not phone.isdigit():
        print('手机号格式不对')
        return 1

    s = wxlogin.WxLogin(host=wxlogin.HOSTS[0])
    print('正在交换密钥…')
    s.exchange_secret()

    try:
        r = s.check_phone(phone)
        if '已经注册' not in str(r.get('message_', '')):
            print('提示：服务端返回', r.get('message_'))
    except Exception as e:
        print('检查手机号失败：', e)

    print('正在发送验证码…')
    r = s.send_code(phone)
    if str(r.get('code_')) != '0':
        print('发送失败：', r.get('message_'))
        return 1
    print('验证码已发送（5 分钟内有效）')

    code = input('验证码：').strip()
    if len(code) != 6 or not code.isdigit():
        print('验证码格式不对')
        return 1

    print('正在登录…')
    user = s.login_sms(phone, code)

    def pick(*keys):
        for k in keys:
            v = user.get(k)
            if v not in (None, ''):
                return str(v)
        return ''

    cfg = read_cfg()
    cfg.update({
        'token': s.session_id,                       # H5 认的正是这个 session
        'customerCode': s.customer_id or pick('customId'),
        'customerid': s.ecard_customer_id or pick('ecardCustomerid'),
        'outid': pick('authStuNo', 'userName', 'account'),
        'name': pick('name', 'userName'),
        'school': pick('customName_', 'schoolName', 'customerName'),
        'mobile': phone,
    })

    print()
    print('登录成功')
    print('  姓名    ', cfg['name'])
    print('  学号    ', cfg['outid'])
    print('  学校    ', cfg['school'])
    print('  token   ', cfg['token'])

    print()
    print('正在校验用水接口…')
    try:
        v = vwater.Session(cfg)
        v.entrance()
        print('  校验通过，水控余额 ¥%s' % vwater.balance(v))
    except Exception as e:
        print('  校验失败：', e)
        print('  仍会写入配置，可稍后重试 python login.py')
        json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return 1

    json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print()
    print('已写入', CFG_PATH)
    print('接下来：python server.py 8770  然后手机浏览器打开局域网地址')
    return 0


if __name__ == '__main__':
    sys.exit(main())
