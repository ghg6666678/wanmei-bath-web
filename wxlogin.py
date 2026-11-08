"""完美校园 App 登录（短信验证码 / 密码）。

协议来源：开源实现 zhongbr(2020, server.17wanxiao.com) 与 fuqiuluo/wanxiao-campus(2024, app.17wanxiao.com)。
流程：
  1. 本地生成 RSA-1024 密钥对，POST exchangeSecretkey.action 换上服务端 RSA 密文
  2. 解密得到 session 与 appKey（appKey 前 24 字节即 3DES key，IV 固定 "66666666"）
  3. 所有业务参数 3DES-CBC 加密后 base64，包成 {"session":..., "data":...}
     请求头 campusSign = sha256(紧凑 JSON(payload))
  4. 登录成功后 data 里解出 user.token（前 36 位），正是智慧用水 H5 需要的 token
"""
import base64
import hashlib
import json
import random
import uuid

import requests
from Crypto.Cipher import DES3, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

requests.packages.urllib3.disable_warnings()

UA = 'Dalvik/2.1.0 (Linux; U; Android 5.1.1; HUAWEI MLA-AL10 Build/HUAWEIMLA-AL10)'
IV = b'66666666'
WX_VERSION = 10552101

# 服务端域名（新老两套，实测哪个通用哪个）
HOSTS = ['https://app.17wanxiao.com:443', 'https://server.17wanxiao.com']


class LoginError(Exception):
    pass


def _des_encrypt(raw: bytes, key: bytes) -> bytes:
    cipher = DES3.new(key, DES3.MODE_CBC, IV)
    return cipher.encrypt(pad(raw, DES3.block_size))


def _des_decrypt(raw: bytes, key: bytes) -> bytes:
    cipher = DES3.new(key, DES3.MODE_CBC, IV)
    return unpad(cipher.decrypt(raw), DES3.block_size)


class WxLogin:
    def __init__(self, host=None, device_id=None):
        self.host = host or HOSTS[0]
        self.device_id = device_id or str(uuid.uuid4())
        self.session_id = ''
        self.app_key = ''
        self.token = ''
        self.ecard_customer_id = ''
        self.customer_id = ''
        self.raw_user = {}
        self.raw_data = {}
        self.raw_token = ''
        self._rsa = RSA.generate(1024)

    # ---------- 底层 ----------
    def _post(self, url, payload, sign=None):
        headers = {'User-Agent': UA, 'Content-Type': 'application/text;charset=UTF-8'}
        if sign:
            headers['campusSign'] = sign
        r = requests.post(url, data=json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8'),
                          headers=headers, verify=False, timeout=25)
        return r

    def _sign_and_post(self, path, params):
        plain = json.dumps(params, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        enc = _des_encrypt(plain, self.app_key.encode()[:24])
        payload = {'session': self.session_id, 'data': base64.b64encode(enc).decode()}
        compact = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        sign = hashlib.sha256(compact.encode('utf-8')).hexdigest()
        r = self._post(self.host + path, payload, sign)
        try:
            return r.json()
        except Exception:
            raise LoginError('返回不是 JSON: %s' % r.text[:200])

    # ---------- 会话 ----------
    def exchange_secret(self):
        pub_der = self._rsa.publickey().export_key(format='DER')
        pub_b64 = base64.b64encode(pub_der).decode()
        r = self._post(self.host + '/campus/cam_iface46/exchangeSecretkey.action', {'key': pub_b64})
        txt = r.text.strip()
        if not txt:
            raise LoginError('交换密钥失败：空响应')
        cipher = PKCS1_v1_5.new(self._rsa)
        plain = cipher.decrypt(base64.b64decode(txt), b'FAIL')
        try:
            obj = json.loads(plain)
        except Exception:
            raise LoginError('交换密钥失败：无法解密响应')
        if 'session' not in obj or 'key' not in obj:
            raise LoginError('交换密钥失败：%s' % json.dumps(obj, ensure_ascii=False)[:200])
        self.session_id = obj['session']
        self.app_key = obj['key']
        return obj

    @staticmethod
    def pick_host():
        """探测哪个域名可用。"""
        for h in HOSTS:
            try:
                s = WxLogin(host=h)
                s.exchange_secret()
                return h
            except Exception:
                continue
        return None

    # ---------- 业务 ----------
    def check_phone(self, phone):
        return self._sign_and_post('/campus/cam_iface46/checkBeforeSendRegisterCode.action', {
            'action': 'registAndLogin',
            'confirm': False,
            'deviceId': self.device_id,
            'mobile': phone,
            'requestMethod': 'cam_iface46/gainMatrixCaptcha543.action',
            'type': 'sms',
        })

    def send_code(self, phone):
        return self._sign_and_post('/campus/cam_iface46/gainMatrixCaptcha543.action', {
            'action': 'registAndLogin',
            'confirm': False,
            'deviceId': self.device_id,
            'mobile': phone,
            'requestMethod': 'cam_iface46/gainMatrixCaptcha543.action',
            'type': 'sms',
        })

    def _parse_login(self, resp):
        code = resp.get('code_')
        if str(code) != '0':
            raise LoginError(resp.get('message_') or '登录失败')
        data_b64 = resp.get('data')
        if not data_b64:
            raise LoginError('登录返回缺少 data')
        dec = _des_decrypt(base64.b64decode(data_b64), self.app_key.encode()[:24])
        obj = json.loads(dec.decode('utf-8', 'ignore').replace('\r', ''))
        self.raw_data = obj                      # 完整响应，供排查
        user = obj.get('user') or {}
        self.raw_user = user
        token = (user.get('token') or '')
        if not token:
            raise LoginError('登录返回缺少 token')
        self.raw_token = token
        self.token = token[:36]
        self.ecard_customer_id = str(user.get('ecardCustomerid') or '')
        self.customer_id = str(user.get('customId') or '')
        return user

    def login_sms(self, phone, code):
        resp = self._sign_and_post('/campus/cam_iface46/registerUsersByTelAndLoginNew.action', {
            'appCode': 'M002',
            'deviceId': self.device_id,
            'mobile': phone,
            'netWork': 'wifi',
            'qudao': 'tencent',
            'requestMethod': 'cam_iface46/registerUsersByTelAndLoginNew.action',
            'shebeixinghao': 'LGE-AN10',
            'sms': code,
            'systemType': 'android',
            'telephoneInfo': '12',
            'telephoneModel': 'LGE-AN10',
            'wanxiaoVersion': WX_VERSION,
        })
        return self._parse_login(resp)

    def login_pwd(self, phone, pwd):
        key = self.app_key.encode()[:24]
        pwd_list = []
        for ch in pwd.encode('utf-8'):
            pwd_list.append(base64.b64encode(_des_encrypt(bytes([ch]), key)).decode())
        resp = self._sign_and_post('/campus/cam_iface46/loginnew.action', {
            'appCode': 'M002',
            'deviceId': self.device_id,
            'netWork': 'wifi',
            'password': pwd_list,
            'qudao': 'tencent',
            'requestMethod': 'cam_iface46/loginnew.action',
            'shebeixinghao': 'LGE-AN10',
            'systemType': 'android',
            'telephoneInfo': '12',
            'telephoneModel': 'LGE-AN10',
            'type': '1',
            'userName': phone,
            'wanxiaoVersion': WX_VERSION,
            'yunyingshang': '07',
        })
        return self._parse_login(resp)
