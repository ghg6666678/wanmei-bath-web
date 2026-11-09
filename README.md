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

也可以不跑 `login.py`：直接打开网页，用手机号 + 短信验证码登录（票据同样写入 `config.json`）。

Windows 上也可以直接双击 `启动.bat`（会调用 `server.py`）。

启动后终端会打印局域网地址，例如 `http://192.168.1.20:8770`，**手机连同一个 WiFi** 用浏览器打开即可。

## 目录结构

```
server.py       零依赖 HTTP 服务（静态页 + JSON 接口 + 网页登录/退出）
vwater.py       智慧用水协议（AES-ECB 签名、entrance、取码、记录、限时限额）
wxlogin.py      完美校园登录协议（RSA 换密钥 + 3DES + campusSign）
login.py        登录命令行：拿 session 并写入 config.json
web/index.html  单页界面
config.json     登录后生成，含票据和个人信息（已在 .gitignore 中）
```

## 它是怎么工作的

1. `login.py` 先调 `exchangeSecretkey` 用 RSA 公钥换密钥，服务端返回一个 `session`
2. 用**同一个 session** 提交短信验证码登录，服务端把它升格为账号会话
3. 这个 session 就是「智慧用水」H5 需要的 token；网页端登录走 `/api/native`，复用同一套 `WxLogin` 会话（`sendCode` 与 `doLogin` 必须是同一个 session）
4. 之后每次请求带三个头：

```
uuid:      entrance 换来的会话 uuid
timestamp: 毫秒时间戳
encrypt:   AES-ECB-PKCS7(uuid + "-" + timestamp, key="wanmeixiaoyuan11") 的 base64
```

签名密钥取自 H5 前端 JS，属于协议层还原，不涉及破解任何账号。

## 功能

- 余额、当前预约（验证码三行居中大字号 + 当日有效倒计时）
- **星标宿舍楼**：只显示自己那栋楼，同楼 A/B 区合并（某个区设备坏了可去另一区）
- 一键取码 / 取消预约
- 消费限时、限额：滑块 + 文本框联动，金额以 **0.05 元**为步进（与浴室实际扣费单位一致），服务端按「分」做整数校验
- **保存限额后自动取消所有已有预约**：订单里存的是取码当时的数值，不重新取码不生效
- 顶部下拉刷新；所有弹窗按住顶部横条下拉可跟手关闭
- 首页「充值」按钮弹出说明弹层；退出登录有二次确认；登录失效先提示再自动弹登录框
- 最近 6 个月的用水记录
- 深色模式自适应、可「添加到主屏幕」（PWA）

## 已知限制

- **单点登录**：在这里登录会让手机上的原版 App 下线（错误码 `100600`），反之亦然
- **充值只给指引**：点首页「充值」会弹出说明，指引到微信小程序「完美校园」充值。原版支付校验 `appid + 包名 + 签名` 三者绑定，第三方客户端调不起，本工具不处理支付
- 会话会过期；失效时会自动提示并弹出登录框，在网页里重新登录即可（也可以重跑 `python login.py`）
- **请只在可信局域网使用**：服务默认监听 `0.0.0.0` 以便手机访问，而网页端现已支持登录/退出。不要把端口暴露到公网；`config.json` 含票据与个人信息，已在 `.gitignore` 中

## 参考实现

登录协议（RSA 换密钥 → 3DES 加密提交 → `campusSign` 签名）不是猜出来的，参考了以下公开实现：

- **[ReaJason/17wanxiaoCheckin](https://github.com/ReaJason/17wanxiaoCheckin)** · MIT · 已归档
  其 `login/` 目录（`campus.py`、`des_3.py`、`rsa_encrypt.py`、`sha1.py`）完整描述了登录握手过程，本项目的 `wxlogin.py` 按这套流程重写。
- **[fuqiuluo/wanxiao-campus](https://github.com/fuqiuluo/wanxiao-campus)** · Rust · 未声明许可证
  用于交叉验证较新版本的登录流程与请求加密细节。

用水业务协议（`entrance`、取码、限时限额、用水记录）来自官方 H5 前端 JavaScript 的协议还原，没有对应的开源项目。

若本仓库中存在源自上述项目的代码片段，其版权归原作者所有，按 MIT 许可使用。

## 适用范围与其他学校

网页端与 [安卓版](https://github.com/ghg6666678/wanmei-bath-android) 同源，目前只在**一所**使用「完美校园 智慧用水」的学校实测。公共域名、签名密钥以及运行时下发的 `customerCode` / `customerid` / `outid` 是通用的，但以下三项来自本校实际配置，换学校可能需要调整：浴室命名格式（楼栋排序与星标 A/B 区合并）、**0.05 元扣费步进 / 1–20 元 / 1–120 分钟档位**、以及 `limittype` / `verificationtype` 等固定业务参数。

跨校试用反馈请到 [安卓仓库的跨校适配反馈模板](https://github.com/ghg6666678/wanmei-bath-android/issues/new/choose) 提交；公开 issue 中请勿粘贴个人信息、验证码或 token。

## 开发复盘

完整开发流程见 [安卓仓库的公开版复盘](https://github.com/ghg6666678/wanmei-bath-android/blob/main/docs/DEVELOPMENT.md)（已脱敏）。

## 免责声明

仅供个人学习与自用。请使用**自己的账号**，不要用于批量操作或影响他人。因使用本工具产生的一切后果由使用者自行承担。

本仓库不含任何真实账号数据；`config.json` 是本地生成的文件，不会被提交。
