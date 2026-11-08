# 智水 · 网页版（Python）

完美校园「智慧用水 → 洗澡」的精简网页客户端：只保留取码、取消预约、限时限额、用水记录，去掉开屏广告和首页。

手机浏览器打开即可用，也可以「添加到主屏幕」当 App 使用。

## 依赖

- Python 3.9+
- 依赖包：`pip install -r requirements.txt`（requests、pycryptodome）

不需要 Node.js，签名已经用 Python 实现。

## 快速开始

```bash
pip install -r requirements.txt

python login.py        # 手机号 + 短信验证码登录，票据写入 config.json
python server.py 8770  # 启动服务
```

Windows 上也可以直接双击 `启动.bat`（会调用 `server.py`）。

启动后终端会打印局域网地址，例如 `http://192.168.1.20:8770`，**手机连同一个 WiFi** 用浏览器打开即可。

## 目录结构

```
server.py       零依赖 HTTP 服务（静态页 + JSON 接口）
vwater.py       智慧用水协议（AES-ECB 签名、entrance、取码、记录、限时限额）
wxlogin.py      完美校园登录协议（RSA 换密钥 + 3DES + campusSign）
login.py        登录命令行：拿 session 并写入 config.json
web/index.html  单页界面
config.json     登录后生成，含票据和个人信息（已在 .gitignore 中）
```

## 它是怎么工作的

1. `login.py` 先调 `exchangeSecretkey` 用 RSA 公钥换密钥，服务端返回一个 `session`
2. 用**同一个 session** 提交短信验证码登录，服务端把它升格为账号会话
3. 这个 session 就是「智慧用水」H5 需要的 token
4. 之后每次请求带三个头：

```
uuid:      entrance 换来的会话 uuid
timestamp: 毫秒时间戳
encrypt:   AES-ECB-PKCS7(uuid + "-" + timestamp, key="wanmeixiaoyuan11") 的 base64
```

签名密钥取自 H5 前端 JS，属于协议层还原，不涉及破解任何账号。

## 功能

- 余额、当前预约（验证码 + 当日有效倒计时）
- 20 个浴室的空闲率，按宿舍号排序，可置顶常用浴室
- 一键取码 / 取消预约
- 消费限时、限额设置（从设备开阀起累计）
- 最近 6 个月的用水记录
- 深色模式自适应

## 已知限制

- **单点登录**：在这里登录会让手机上的原版 App 下线（错误码 `100600`），反之亦然
- **充值要唤起原版**：页面上选好金额点「去支付」会通过 `wanxiao://` 打开完美校园 App 的充值页。原版支付走微信小程序，微信对 `appid + 包名 + 签名` 三重校验，第三方客户端调不起，所以这一步交给原版完成
- 会话会过期，失效时重新跑一次 `python login.py`

## 免责声明

仅供个人学习与自用。请使用**自己的账号**，不要用于批量操作或影响他人。因使用本工具产生的一切后果由使用者自行承担。

本仓库不含任何真实账号数据；`config.json` 是本地生成的文件，不会被提交。
