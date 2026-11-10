# 无状态会话：把票据交给浏览器，服务端不留

这份文档说明 `server.py` 的「无状态模式」——**票据加密后放进浏览器 cookie，服务端不保存任何账号数据**。

多人共用一个服务端时必须这么做：默认模式把票据写进 `config.json`、进程里只维护一份 `Session`，第二个人登录会把第一个人挤掉。

> 你如果只是想自己用，**不需要看这份文档**。默认模式更省事：重启不用重新登录。

---

## 一、要解决的问题

默认模式（单人自用）有三个全局状态：

| 状态 | 位置 | 多人用会怎样 |
| --- | --- | --- |
| `_config` | `config.json` 落盘 | 后登录的人覆盖前一个 |
| `_session` | 进程内单例 | 同上，且无法区分请求来自谁 |
| `_wx` | 进程内单例（登录握手） | 两个人同时登录，验证码会串到对方的会话上 |

无状态模式把这三样拆开：

- **票据** → 加密进 cookie，浏览器保管，服务端每次请求现解；
- **登录握手** → 按临时标识分桶，带 TTL，用完即丢（见第四节）；
- **全局单例** → 全部去掉，每个请求独立。

## 二、核心：一个自包含的密文 cookie

```
cookie 值 = base64url( nonce(12) || GCM tag(16) || 密文 )
明文     = zlib( JSON )
JSON     = {"c": 账号配置, "u": 上游会话 uuid, "p": 入口页地址}
```

为什么这么设计：

- **zlib 先压**：`u` 和 `p` 都是长字符串，压缩后 cookie 通常 300–800 字节，离 4 KB 上限很远；
- **AES-GCM**：自带完整性校验，改一个字节就解不开，不用担心「篡改 cookie 提权」；
- **nonce 随机**：同样的内容两次加密结果不同，无法通过比对 cookie 判断是不是同一个人；
- **不用服务端 session 表**：解出来就能用，多进程/多机部署天然一致。

加密只用一条依赖：`pycryptodome`（`vwater.py` 本来就依赖它，没有引入新东西）。

## 三、服务端要改的四处

### 1. cookie 的读写

```python
def _state(self):
    raw = self._cookies().get(COOKIE)
    if not raw:
        return None
    try:
        return BOX.open(raw)          # 解不开就当没登录
    except statebox.StateError:
        return None                    # 关键：不要 500，否则篡改一个 cookie 就能刷错误日志

def _save_state(self, cfg, uuid=None, page=''):
    blob = BOX.seal({'c': cfg, 'u': uuid, 'p': page or ''})
    self._hdr('Set-Cookie', build_cookie(COOKIE, blob, max_age=7*24*3600))
```

### 2. 从 cookie 重建会话

这是整个方案的关键——**cookie 里带着上游会话的 `uuid` 和入口页地址，复用它就不必每次重新换取**：

```python
def state_session(self, force=False):
    st = self._state()
    if not st:
        raise NotLoggedIn('还没有登录，请用手机号登录')
    cfg = st['c']
    s = vwater.Session(cfg)
    s.uuid = st.get('u')          # 有就复用
    s.page = st.get('p') or ''
    if force or not s.uuid:       # 没有才换一次
        s.entrance()
        self._save_state(s.cfg, s.uuid, s.page)   # 换来的新值回写
    return s
```

**这一步省掉了每次请求的一次上游往返**，是无状态方案能用的前提。如果每次都重新换取，一个页面刷三次就会多打三个请求，很容易触发上游风控。

### 3. 每个请求独立，不能有缓存

```python
def state_session(self, force=False):
    cached = getattr(self, '_sess', None)
    if cached is not None and not force:
        return cached          # 只在「同一个请求内」复用
    ...
```

实例级缓存是安全的（`ThreadingHTTPServer` 每个请求一个新 Handler 实例），**模块级缓存是致命的**——那就退回成「一份凭据给所有人用」了。

### 4. 常量必须在请求间隔离

上面前提是：`get_session()` 不再接受「无参调用」。所有调用点改成 `get_session(self)`，否则无状态模式下拿不到 handler，直接报错。这是**故意的**——留着无参调用，就等于留了一条退化成单例的缝。

## 四、登录握手：为什么这里做不到完全无状态

换密钥要用 RSA 私钥，发短信要维持一次会话 cookie。这两步必须在服务端完成，浏览器做不了（除非把整套登录协议搬到前端，那是另一个量级的事，也会把协议细节暴露给任何人）。

所以这部分**放在内存里，但不落盘、不写日志、带 TTL**：

```python
_PENDING = {}          # sid -> (WxLogin 实例, 创建时间)
_PENDING_TTL = 300     # 5 分钟没人用就清掉

# 发送验证码：新建或复用，sid 通过 cookie 交给浏览器
# 提交验证码：取走并删除，保证一个 sid 只能用一次
```

边界说清楚：

| 阶段 | 服务端是否有状态 | 说明 |
| --- | --- | --- |
| 登录握手（秒级） | **有**，内存 + 5 分钟 TTL | RSA 私钥与临时会话，登录完成即丢 |
| 登录之后 | **无** | 票据只在浏览器，服务端每次现解 |

附带修掉一个原有 bug：`_wx` 原来是模块级单例，两个人同时登录时，后一个人的 `sendCode` 会覆盖前一个人的握手会话，导致验证码发到 A 的会话、登录却在 B 的会话上——**表现为「验证码正确但登录失败」**。按 `sid` 分桶后不再有这个问题。

## 五、怎么启用

```bash
export BATH_STATELESS=1
export BATH_STATE_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
python server.py 8799
```

- 不设 `BATH_STATE_KEY` 会自动生成 `state.key`（权限 0600）。它是一把锁，不是锁里的东西，随时可删——删了所有人重新登录一次。
- 走 HTTPS 时加 `BATH_COOKIE_SECURE=1`，cookie 会带 `Secure`。**纯 HTTP 下千万别开**，否则浏览器拒收。
- **前端一行都不用改**：cookie 由浏览器自动携带，`fetch` 默认同源带 cookie。

## 六、安全边界（要照抄的话，这几条必须一起抄）

1. **cookie 是 `HttpOnly`**：脚本读不到，XSS 拿不走票据。
2. **`SameSite=Lax`**：跨站 POST 不带 cookie。如果前后端不同源，改成 `None` 就必须同时开 `Secure`。
3. **`Secure` 只在 HTTPS 下开**：本地 HTTP 调试时开了会导致「登录成功但立刻又要登录」——这个坑很常见。
4. **密钥泄露 = 全部票据泄露**：拿到密钥 + 任一用户 cookie 就能解出那一个人的票据。密钥放环境变量，别提交进仓库。
5. **不做服务端缓存**：一旦为了「性能」加一层 dict 缓存，多用户就会串号。
6. **解不开一律当未登录**：不要区分「格式错」和「验签失败」，否则等于给攻击者一个探测口。

## 七、移植到别的技术栈

思路与语言无关，只需要三样能力：对称加密（带认证）、压缩、base64url。

| 栈 | 建议 |
| --- | --- |
| Node.js | `crypto.createCipheriv('aes-256-gcm')` + `zlib.deflateRawSync`；或直接用 `iron-session`、`@hapi/iron` 这类成熟封装 |
| Go | `crypto/aes` + `cipher.NewGCM` + `compress/zlib`；`gorilla/securecookie` 也行（但默认不带压缩） |
| PHP | `openssl_encrypt(..., 'aes-256-gcm')` + `gzdeflate` |
| Python | 见本仓库 `statebox.py`，约 120 行，无框架依赖 |

如果不想自己写，**标准做法是「签名 cookie」而不是「加密 cookie」**（如 Flask 的 `itsdangerous`、Rails 的 `MessageVerifier`）。区别是：签名 cookie 的内容**浏览器能读**，只是不能改。票据属于敏感信息，**用加密**，不要只签名。

顺带一个通用经验：**cookie 里只放「恢复会话所必需的最小字段」**。这个项目一共 6 个字段加两个会话参数，压缩后 300 多字节；如果把整份用户信息塞进去，很容易撞 4 KB 上限，而浏览器超限是**静默丢弃**，表现为「登录成功但下次请求又没登录」，极难排查。

## 八、测试

```bash
python tests/test_statebox.py          # 22 项：往返、篡改、换密钥、容量
python tests/test_stateless_logic.py   # 24 项：uuid 复用、强制重换、字段覆盖、缺字段
python tests/test_stateless_e2e.py     # 11–16 项：两用户隔离、篡改降级、日志不含票据
```

前两个不需要网络，也不需要真实账号，clone 下来直接能跑。

`test_stateless_logic.py` 用假的 `Session` 替身，这是这套改动里最值得参考的部分：**验证「不重复换取」这种优化，不必真的去连上游**。它把 `entrance()` 换成一个只记次数的桩，然后断言「cookie 带 uuid 时调用次数为 0」——比起搭一套测试环境，这样既快又能精确断言。

`test_stateless_e2e.py` 会起一个真实服务端，用两个不同用户的 cookie 交替请求同一个接口，确认没有串号。仓库里没有 `config.json` 时会自动跳过需要真实票据的两项。

---

## 附：这个方案不适合什么

- **单机自用**：默认模式更省事，重启不用重新登录。
- **票据需要撤销**：无状态 cookie 发出去就收不回，只能靠换密钥让所有人失效。如果需要「踢掉某个设备」，就得引入服务端黑名单，那又变回有状态了。
- **超大 cookie 场景**：这里是因为字段少才成立。字段一多就该换回服务端 session + 短 ID。
