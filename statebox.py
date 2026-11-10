"""无状态会话存储：票据加密后放进浏览器 cookie，服务端不保留任何用户数据。

设计目标
--------
1. 服务端**不落盘**任何票据、账号、手机号；
2. 服务端**不写日志**任何票据内容；
3. 每个请求自带凭据，多个使用者互不干扰（没有全局 session 单例）；
4. 浏览器里那串 cookie 是密文，`HttpOnly` 防脚本读取，篡改会被 GCM 校验拒绝。

安全边界
--------
- 密钥由服务端持有（环境变量或本地文件），**它不是用户数据**，但必须保密：
  拿到密钥 + 任一用户的 cookie，就能解出那一个人的票据。
- 真正的"服务端零数据"在登录握手阶段做不到——换密钥、发短信这两步必须有服务端
  持有 RSA 私钥和临时 cookie。那部分放在内存里，带 TTL，登录完立即丢弃。
  浏览器里只剩下最终的票据密文。这一点在 docs/STATELESS.md 里写清楚了。

用法
----
    export BATH_STATELESS=1
    export BATH_STATE_KEY=<64 位十六进制>      # 不设则自动生成到 state.key
    python server.py 8799
"""

import base64
import json
import os
import secrets
import threading
import zlib

from Crypto.Cipher import AES

# cookie 名
COOKIE = 'bath_state'          # 登录后的票据密文
PENDING_COOKIE = 'bath_pending'  # 登录握手过程中的临时标识

# 环境变量
MODE_ENV = 'BATH_STATELESS'
KEY_ENV = 'BATH_STATE_KEY'
SECURE_ENV = 'BATH_COOKIE_SECURE'   # 走 HTTPS 时设 1，cookie 加 Secure

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, 'state.key')

NONCE_LEN = 12
TAG_LEN = 16


class StateError(Exception):
    """cookie 解不开：被篡改、换了密钥，或者根本不是我们发的。"""


def mode_enabled():
    return os.environ.get(MODE_ENV, '').strip() in ('1', 'true', 'yes', 'on')


def cookie_secure():
    return os.environ.get(SECURE_ENV, '').strip() in ('1', 'true', 'yes', 'on')


def _load_key():
    """密钥优先取环境变量；没有就生成一份存到 state.key（0600）。

    存 state.key 不违反「不保存用户数据」——它是一把锁，不是锁里的东西。
    删掉它等于让所有人重新登录一次，随时可以删。
    """
    raw = os.environ.get(KEY_ENV, '').strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            raise SystemExit('%s 必须是 64 位十六进制（32 字节），当前值无法解析' % KEY_ENV)
        if len(key) != 32:
            raise SystemExit('%s 必须是 32 字节，当前 %d 字节' % (KEY_ENV, len(key)))
        return key

    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            key = f.read().strip()
        if len(key) == 32:
            return key
        # 文件坏了就当没有，重新生成（旧 cookie 会全部失效）

    key = secrets.token_bytes(32)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    print('已生成会话密钥 %s（换机器/删除后所有人需重新登录）' % KEY_FILE)
    return key


class StateBox:
    """把 dict 压紧 → 加密 → base64url，整个过程无状态。"""

    def __init__(self, key):
        if len(key) != 32:
            raise ValueError('密钥必须是 32 字节')
        self.key = key
        self._lock = threading.Lock()

    def seal(self, obj):
        plain = zlib.compress(
            json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), 9)
        with self._lock:
            cipher = AES.new(self.key, AES.MODE_GCM, nonce=secrets.token_bytes(NONCE_LEN))
            ct, tag = cipher.encrypt_and_digest(plain)
            nonce = cipher.nonce
        return base64.urlsafe_b64encode(nonce + tag + ct).decode('ascii').rstrip('=')

    def open(self, token):
        if not token or not isinstance(token, str):
            raise StateError('空 cookie')
        pad = '=' * (-len(token) % 4)
        try:
            blob = base64.urlsafe_b64decode(token + pad)
        except Exception:
            raise StateError('不是合法的 base64')
        if len(blob) < NONCE_LEN + TAG_LEN:
            raise StateError('长度不足')
        nonce = blob[:NONCE_LEN]
        tag = blob[NONCE_LEN:NONCE_LEN + TAG_LEN]
        ct = blob[NONCE_LEN + TAG_LEN:]
        try:
            with self._lock:
                plain = AES.new(self.key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
        except ValueError:
            raise StateError('校验失败（被改动过，或换过密钥）')
        try:
            return json.loads(zlib.decompress(plain).decode('utf-8'))
        except Exception:
            raise StateError('内容无法解析')


_BOX = None
_BOX_LOCK = threading.Lock()


def box():
    """懒加载单例；不开无状态模式时不要调用，免得平白生成一个密钥文件。"""
    global _BOX
    with _BOX_LOCK:
        if _BOX is None:
            _BOX = StateBox(_load_key())
        return _BOX


def parse_cookies(header):
    """把 Cookie 头解析成 dict。重复名取第一个（与浏览器行为一致）。"""
    out = {}
    if not header:
        return out
    for part in header.split(';'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = k.strip()
        if k and k not in out:
            out[k] = v.strip()
    return out


def build_cookie(name, value, max_age=None):
    parts = ['%s=%s' % (name, value), 'Path=/', 'HttpOnly', 'SameSite=Lax']
    if max_age is not None:
        parts.append('Max-Age=%d' % max_age)
    if cookie_secure():
        parts.append('Secure')
    return '; '.join(parts)


def clear_cookie(name):
    return build_cookie(name, '', max_age=0)
