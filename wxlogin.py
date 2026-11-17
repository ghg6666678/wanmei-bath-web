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

IV = b'66666666'
WX_VERSION = 10552101

# ---------- 设备画像（2026-11-16 起：登录握手与业务请求统一描述同一台设备） ----------
# 登录握手有【四处】向上游描述设备：
#   1. 请求头 User-Agent —— Dalvik 格式（安卓 http.agent 的原生样式）
#   2. 请求体里的 shebeixinghao / telephoneModel —— 服务器登记的设备字段
#   3. 请求体里的 telephoneInfo —— 安卓系统版本号（旧代码硬编码 '12'，与任何画像都对不上；
#      2026-11-18 起随画像走，见 login_sms / login_pwd）
#   4. 登录成功后所有业务请求的 H5 WebView UA（vwater.Session 使用，见 h5_ua）
# 2026-11-15 之前这里是三台互不相干的手机：UA 头报 2020 年的华为、请求体报 LGE-AN10、
# 业务 UA 报 PJE110 —— 同一个出口 IP 上三种请求三种设备，比「同 IP 多账号」更容易被聚类。
# 现在登录时从池里挑一套自洽画像，随票据封进浏览器 cookie（server._login_config），
# 7 天内同一浏览器始终是「同一台手机、同一次安装」（deviceId 一并固定）。
# 型号与 build 只要格式正确即可：上游从未校验过具体型号（此前三台混用也从未被拒），
# 重要的是会话内自洽、且别让全站所有账号共用一个型号。
# 2026-11-18 起：全池统一为 Android 14，六台不同品牌的国内常见机型，型号互不重复。
# 选型原则：都是真实存在、且**官方最终系统就是 Android 14** 的老机器（不是跑着 Android 16
# 的新旗舰）——旧机型在 2026 年仍以原厂最后版本上网更自然，也避免「新机跑老系统」的倒挂。
# 前缀差异由厂商决定，不要按品牌习惯去「修正」：
#   UP1A / UKQ1 = 高通、联发科通用的 AOSP 14 基线（OPPO/vivo/一加用 UP1A，小米/红米用 UKQ1）
#   HONORPGT-AN00 = 荣耀沿用的「厂商+型号」写法，真实机器上就是这个格式
DEVICES = [
    {'model': 'PJE110',     'os': '14', 'build': 'UP1A.231005.007'},   # 一加 Ace 3（ColorOS 14）
    {'model': 'M2011K2C',   'os': '14', 'build': 'UKQ1.230804.001'},   # 小米 11（末版 HyperOS 1.0）
    {'model': '22041211AC', 'os': '14', 'build': 'UKQ1.230804.001'},   # Redmi K50（末版 HyperOS 1.0）
    {'model': 'PHZ110',     'os': '14', 'build': 'UP1A.231005.007'},   # OPPO Find X7（ColorOS 14）
    {'model': 'V2309A',     'os': '14', 'build': 'UP1A.231005.007'},   # vivo X100（OriginOS 4）
    {'model': 'PGT-AN00',   'os': '14', 'build': 'HONORPGT-AN00'},     # 荣耀 Magic5（MagicOS 8.0，出厂 7.1/13）
]
DALVIK_TMPL = 'Dalvik/2.1.0 (Linux; U; Android {os}; {model} Build/{build})'
# Chrome 与 wanxiao H5 版本号跟设备无关（各自独立升级），只有机型与系统版本随画像走；
# Chrome/138 是从真实设备上抓下来的版本，保持不动。
H5_UA_TMPL = ('Mozilla/5.0 (Linux; Android {os}; {model}) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/138.0.7204.179 Mobile Safari/537.36 wanxiao/5.8.36')


def device_from_cfg(cfg):
    """从票据字段还原设备画像。字段不全（旧票据 / 从没登录过）返回 None，
    让登录时随机挑一套新的——旧票据不强制重登，登录成功后自然补全。"""
    if not isinstance(cfg, dict):
        return None
    m, o, b = cfg.get('devModel'), cfg.get('devOs'), cfg.get('devBuild')
    if m and o and b:
        return {'model': str(m), 'os': str(o), 'build': str(b)}
    return None


def device_from_dev(dev):
    """浏览器 localStorage 里记住的设备画像：登出、票据过期（7 天）后，
    同一个浏览器重新登录还是「同一台手机、同一次安装」，而不是每次换新。

    只接受与 DEVICES 池完全一致的组合——这个字段来自客户端请求体，不能让
    它指定池外的任意机型串（那会把怪值注进 UA 头和 shebeixinghao）。对不上就当没有。"""
    if not isinstance(dev, dict):
        return None
    for d in DEVICES:
        if (dev.get('model') == d['model'] and dev.get('os') == d['os']
                and dev.get('build') == d['build']):
            return dict(d)
    return None

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
    def __init__(self, host=None, device_id=None, device=None):
        self.host = host or HOSTS[0]
        # device_id：真实 App 首装生成后一般不变。传进来就沿用（同一浏览器重登
        # 还是同一次“安装”），否则新建——首个 UUID 就是这台“手机”的 ID。
        self.device_id = device_id or str(uuid.uuid4())
        # 画像三件套：显式传入（重登还原）优先，否则从池里随机挑一套。
        if not (isinstance(device, dict) and device.get('model')
                and device.get('os') and device.get('build')):
            device = random.choice(DEVICES)
        self.device = dict(device)
        self.model = self.device['model']
        # 登录握手 UA（Dalvik）与业务请求 UA（H5 WebView）格式本来就不同——
        # 真实用户在这两个端点上也呈现两种 UA；要统一的是“描述同一台设备”。
        self.dalvik_ua = DALVIK_TMPL.format(**self.device)
        self.h5_ua = H5_UA_TMPL.format(**self.device)
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
        headers = {'User-Agent': self.dalvik_ua, 'Content-Type': 'application/text;charset=UTF-8'}
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
            'shebeixinghao': self.model,
            'sms': code,
            'systemType': 'android',
            'telephoneInfo': self.device['os'],
            'telephoneModel': self.model,
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
            'shebeixinghao': self.model,
            'systemType': 'android',
            'telephoneInfo': self.device['os'],
            'telephoneModel': self.model,
            'type': '1',
            'userName': phone,
            'wanxiaoVersion': WX_VERSION,
            'yunyingshang': '07',
        })
        return self._parse_login(resp)
