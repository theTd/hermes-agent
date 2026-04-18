# NapCatQQ 对接文档

本文面向需要把业务系统接入 NapCatQQ 的开发者，重点说明本项目当前代码库已经实现的对接方式、配置入口、鉴权规则与最小可用示例。

## 1. 项目定位

NapCatQQ 是基于 NTQQ 的协议端实现，当前仓库里对外主要暴露两类能力：

1. `OneBot 11` 业务接口
2. `WebUI / 管理接口`

通常建议这样理解：

- 机器人业务收发消息、处理事件：接 `OneBot 11`
- 读取或修改管理配置、获取登录状态、驱动 WebUI：接 `WebUI 管理接口`

## 2. 运行目录与配置文件

项目运行时通过 `NapCatPathWrapper` 组织目录。默认情况下：

- Windows / Linux: 写目录默认是程序所在目录
- macOS: 写目录默认是 `~/Library/Application Support/QQ/NapCat`
- 如设置环境变量 `NAPCAT_WORKDIR`，则统一写入该目录

关键目录如下：

- `config/`
- `logs/`
- `cache/`
- `plugins/`

关键配置文件如下：

- `config/onebot11.json`
- `config/onebot11_{uin}.json`
- `config/webui.json`

说明：

- `onebot11.json` 是全局默认 OneBot 配置
- QQ 登录后，WebUI 读取和写入的是 `onebot11_{uin}.json`，用于按账号覆盖
- `webui.json` 是 WebUI 服务配置

## 3. 当前默认端口

仓库内示例配置显示的默认值如下：

### OneBot 示例配置

- HTTP Server: `127.0.0.1:3000`
- WebSocket Server: `127.0.0.1:3001`

对应示例文件：

- `packages/napcat-develop/config/onebot11.json`

### WebUI 示例配置

- WebUI: `0.0.0.0:6099`

对应示例文件：

- `packages/napcat-webui-backend/webui.json`

注意：

- WebUI 真正运行端口会在配置端口被占用时自动向后尝试，最多顺延 100 个端口
- 如设置 `NAPCAT_WEBUI_PREFERRED_PORT`，会优先尝试该端口

## 4. OneBot 11 对接

### 4.1 支持的网络模式

当前仓库支持以下网络适配器：

- `httpServers`
- `httpSseServers`
- `httpClients`
- `websocketServers`
- `websocketClients`
- `plugins`

对应能力如下：

- `httpServers`: 正向 HTTP API，可选同端口附带 WebSocket
- `httpSseServers`: HTTP API + SSE 事件推送
- `httpClients`: 反向 HTTP 上报
- `websocketServers`: 正向 WebSocket
- `websocketClients`: 反向 WebSocket

### 4.2 配置格式

OneBot 配置会按 schema 自动补默认值，且读取时使用 `json5`，所以支持注释、尾逗号等 JSON5 语法。

最小示例：

```json5
{
  network: {
    httpServers: [
      {
        enable: true,
        name: 'HTTP',
        host: '127.0.0.1',
        port: 3000,
        enableCors: true,
        enableWebsocket: false,
        messagePostFormat: 'array',
        token: 'your-onebot-token',
        debug: false,
      },
    ],
    websocketServers: [
      {
        enable: true,
        name: 'WebSocket',
        host: '127.0.0.1',
        port: 3001,
        reportSelfMessage: false,
        enableForcePushEvent: true,
        messagePostFormat: 'array',
        token: 'your-onebot-token',
        debug: false,
        heartInterval: 30000,
      },
    ],
    httpSseServers: [],
    httpClients: [],
    websocketClients: [],
    plugins: [],
  },
  musicSignUrl: '',
  enableLocalFile2Url: false,
  parseMultMsg: false,
  imageDownloadProxy: '',
  timeout: {
    baseTimeout: 10000,
    uploadSpeedKBps: 256,
    downloadSpeedKBps: 256,
    maxTimeout: 1800000,
  },
}
```

### 4.3 鉴权规则

OneBot HTTP / WebSocket 统一支持两种 token 传法：

- 请求头：`Authorization: Bearer <token>`
- 查询参数：`access_token=<token>`

如果配置中的 `token` 为空字符串，则不校验。

### 4.4 HTTP API

当启用 `httpServers` 后，接口形式为：

- `GET /`：健康检查
- `POST /{action}`：调用 OneBot 动作
- `GET /{action}`：部分动作也支持 query 方式取参

返回结构：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {},
  "message": "",
  "wording": "",
  "echo": "custom-echo",
  "stream": "normal-action"
}
```

失败时：

```json
{
  "status": "failed",
  "retcode": 1400,
  "data": null,
  "message": "错误信息",
  "wording": "错误信息",
  "echo": "custom-echo",
  "stream": "normal-action"
}
```

#### 示例：获取登录信息

```bash
curl -X POST "http://127.0.0.1:3000/get_login_info" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer your-onebot-token" ^
  -d "{}"
```

#### 示例：发送群消息

```bash
curl -X POST "http://127.0.0.1:3000/send_group_msg" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer your-onebot-token" ^
  -d "{\"group_id\":\"123456\",\"message\":\"hello\"}"
```

### 4.4.1 常用 OneBot HTTP API 速查

下面这些动作已经在仓库中实现，且属于业务对接时最常用的一批接口。

#### 系统类

- `get_login_info`
- `get_status`
- `get_version_info`
- `set_restart`

#### 消息类

- `send_private_msg`
- `send_group_msg`
- `send_msg`
- `get_msg`
- `delete_msg`

#### 群组类

- `get_group_info`
- `get_group_list`
- `get_group_member_info`
- `get_group_member_list`
- `set_group_add_request`
- `set_group_kick`
- `set_group_ban`
- `set_group_whole_ban`

#### 用户类

- `get_friend_list`
- `get_stranger_info`
- `set_friend_add_request`
- `send_like`

#### 文件类

- `get_file`
- `get_image`
- `get_record`
- `get_group_file_url`
- `get_private_file_url`

### 4.4.2 获取群聊中指定成员的信息

本项目已经对外开放：

- `POST /get_group_member_info`

请求体参数：

```json
{
  "group_id": "123456",
  "user_id": "10001",
  "no_cache": true
}
```

参数说明：

- `group_id`: 群号
- `user_id`: 成员 QQ 号
- `no_cache`: 可选，是否绕过缓存

调用示例：

```bash
curl -X POST "http://127.0.0.1:3000/get_group_member_info" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer your-onebot-token" ^
  -d "{\"group_id\":\"123456\",\"user_id\":\"10001\",\"no_cache\":true}"
```

用途说明：

- 如果调用成功，返回该成员的群成员资料
- 如果成员不在群内，接口会返回失败
- 因此业务侧可以直接用它判断“指定用户是否在指定群”

同类接口：

- `get_group_member_list`: 拉取整个群成员列表
- `get_group_info`: 获取群信息
- `get_group_list`: 获取当前账号所在群列表

### 4.4.3 OneBot 返回数据怎么处理

OneBot HTTP / WebSocket 的返回结构是统一的，接入方不要只看 HTTP 状态码。

成功结构：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {},
  "message": "",
  "wording": "",
  "echo": "custom-echo",
  "stream": "normal-action"
}
```

失败结构：

```json
{
  "status": "failed",
  "retcode": 1400,
  "data": null,
  "message": "错误信息",
  "wording": "错误信息",
  "echo": "custom-echo",
  "stream": "normal-action"
}
```

建议按下面规则处理：

- `status == "ok"` 且 `retcode == 0`：视为成功
- `status == "failed"`：视为失败，优先读取 `message`
- `echo`：如果你的客户端有并发请求，应该自行带上并在返回时用它做请求匹配
- `data`：真正的业务返回值

不要只根据 HTTP 200 判断成功。NapCat 的业务失败通常也会返回 HTTP 200，但 `status` 会是 `failed`。

### 4.4.4 常用 OneBot 接口详解

这一节只展开第一批最常用、最适合直接接入的接口。

#### `get_login_info`

用途：

- 获取当前登录机器人账号

请求：

```json
{}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "user_id": 123456789,
    "nickname": "机器人"
  }
}
```

接入处理建议：

- 启动后先调一次，确认 OneBot 服务和 QQ 登录都正常
- 拿 `data.user_id` 作为当前 bot 标识缓存

#### `get_status`

用途：

- 获取当前协议端状态

请求：

```json
{}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "online": true,
    "good": true
  }
}
```

接入处理建议：

- `online` 可用于判断账号在线状态
- `good` 可用于做健康检查面板

#### `send_private_msg`

用途：

- 给指定用户发私聊消息

请求：

```json
{
  "user_id": "10001",
  "message": "hello"
}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "message_id": 123456
  }
}
```

接入处理建议：

- 成功后持久化 `data.message_id`
- 后续如需撤回、查询消息，可继续使用这个 `message_id`

#### `send_group_msg`

用途：

- 给指定群发送消息

请求：

```json
{
  "group_id": "123456",
  "message": "hello"
}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "message_id": 123456
  }
}
```

接入处理建议：

- 这是群消息的最小可用调用
- 如果你系统里维护会话上下文，建议把 `group_id` 和 `message_id` 一起记录

#### `get_group_member_info`

用途：

- 获取指定群内某个成员的资料
- 也可直接用于判断“此人是否在此群”

请求：

```json
{
  "group_id": "123456",
  "user_id": "10001",
  "no_cache": true
}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "group_id": 123456,
    "user_id": 10001,
    "nickname": "昵称",
    "card": "名片",
    "role": "member"
  }
}
```

失败时的典型含义：

- 成员不在群里
- 群不存在
- 当前 bot 无法获取该成员资料

接入处理建议：

- `ok`：视为该用户在群内
- `failed` 且 `message` 提示成员不存在：视为该用户不在群内
- 如果你关心实时性，`no_cache` 传 `true`
- 如果你更关心吞吐量，允许读缓存时可传 `false`

#### `get_group_member_list`

用途：

- 拉取整个群的成员列表

请求：

```json
{
  "group_id": "123456",
  "no_cache": false
}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": [
    {
      "group_id": 123456,
      "user_id": 10001,
      "nickname": "昵称A",
      "card": "名片A",
      "role": "member"
    },
    {
      "group_id": 123456,
      "user_id": 10002,
      "nickname": "昵称B",
      "card": "名片B",
      "role": "admin"
    }
  ]
}
```

接入处理建议：

- 用于做整群同步、成员缓存预热
- 如果只想查一个人，优先用 `get_group_member_info`

#### `get_group_info`

用途：

- 获取单个群信息

请求：

```json
{
  "group_id": "123456"
}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "group_id": 123456,
    "group_name": "测试群",
    "member_count": 100,
    "max_member_count": 500
  }
}
```

接入处理建议：

- 适合在进入群会话前拉一遍群名和人数
- 可用于发现群已解散、bot 已退群等情况

#### `get_group_list`

用途：

- 获取当前 bot 所在群列表

请求：

```json
{}
```

成功响应示例：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": [
    {
      "group_id": 123456,
      "group_name": "测试群",
      "member_count": 100,
      "max_member_count": 500
    }
  ]
}
```

接入处理建议：

- 可作为初始化群索引的入口
- 不要高频轮询，适合启动同步或后台低频巡检

### 4.5 正向 WebSocket

当启用 `websocketServers` 时：

- `ws://host:port/`：事件 + API 都可用，连接后会收到生命周期事件与心跳
- `ws://host:port/api`：仅作为 API 通道，不加入事件推送列表

消息请求格式：

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": "123456",
    "message": "hello"
  },
  "echo": "demo-1"
}
```

特点：

- 事件通道连接建立后会立即收到 `meta_event.lifecycle.connect`
- 心跳间隔由 `heartInterval` 控制，默认 `30000ms`
- 如果消息格式非法，会返回 `retcode: 1400`

### 4.6 HTTP + WebSocket 同端口

如果使用 `httpServers` 且 `enableWebsocket=true`，则会在 HTTP 服务的同一端口挂载 WebSocket。

规则与正向 WebSocket 一致：

- `ws://host:port/`：事件 + API
- `ws://host:port/api`：API-only

### 4.7 HTTP SSE

如果启用 `httpSseServers`：

- `GET /_events`：建立 SSE 事件流
- 其他路径仍按 `POST /{action}` 调用 OneBot API

SSE 单条事件格式：

```text
data: {"post_type":"message", ...}

```

### 4.8 反向 HTTP 上报

如果启用 `httpClients`，NapCat 会把事件 `POST` 到配置的 `url`。

请求头包括：

- `Content-Type: application/json`
- `x-self-id: <bot_qq>`

如果配置了 token，还会带：

- `x-signature: sha1=<hmac>`

签名规则：

- 使用请求体原始 JSON 字符串
- 以 token 为密钥做 `HMAC-SHA1`

服务端返回内容会被当作 Quick Operation 结果继续处理。

### 4.9 反向 WebSocket

如果启用 `websocketClients`，NapCat 会主动连接目标地址，并在握手时附带：

- `Authorization: Bearer <token>`
- `X-Self-ID: <bot_qq>`
- `x-client-role: Universal`
- `User-Agent: OneBot/11`

连接成功后会发送生命周期事件，并按配置周期发送心跳。

### 4.10 消息格式与 ID 类型

仓库内 README 已明确建议：

- `message_id`
- `user_id`
- `group_id`

在接入侧统一按字符串处理。

另外，`messagePostFormat` 支持：

- `array`
- `string`

默认是 `array`。如果未明确需求，建议保持 `array`。

## 5. WebUI / 管理接口对接

WebUI 后端统一挂载在：

- `/api`

也就是说，登录、配置、状态等管理接口完整前缀都是：

- `/api/auth/...`
- `/api/QQLogin/...`
- `/api/OB11Config/...`
- `/api/WebUIConfig/...`

返回结构统一为：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

失败时：

```json
{
  "code": -1,
  "message": "Unauthorized"
}
```

### 5.1 WebUI 鉴权流程

WebUI 不是直接拿明文 token 换会话，而是两步：

1. 客户端先计算 `sha256(token + ".napcat")`
2. 调用 `/api/auth/login` 提交 `{ hash }`
3. 服务端返回一个 Base64 编码的 `Credential`
4. 后续请求使用 `Authorization: Bearer <Credential>`

凭证特点：

- 服务端用 HMAC-SHA256 对凭证签名
- 单个凭证有效期 1 小时
- `/api/auth/logout` 后可被拉黑失效
- 部分 SSE 或特殊场景也支持 query 参数 `webui_token=<Credential>`

#### 登录示例

```bash
curl -X POST "http://127.0.0.1:6099/api/auth/login" ^
  -H "Content-Type: application/json" ^
  -d "{\"hash\":\"<sha256(token+.napcat)>\"}"
```

成功响应示例：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "Credential": "base64-credential"
  }
}
```

### 5.1.1 WebUI 返回数据怎么处理

WebUI 管理接口返回结构与 OneBot 不同，统一是：

成功：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

失败：

```json
{
  "code": -1,
  "message": "Unauthorized"
}
```

建议按下面规则处理：

- `code == 0`：成功，读取 `data`
- `code != 0`：失败，读取 `message`
- 管理接口大部分仍然返回 HTTP 200，所以不要只看 HTTP 状态码

### 5.1.2 WebUI 登录后请求怎么带凭证

`/api/auth/login` 成功后会拿到：

```json
{
  "Credential": "base64-credential"
}
```

后续请求统一带：

```http
Authorization: Bearer base64-credential
```

例如：

```bash
curl "http://127.0.0.1:6099/api/WebUIConfig/GetConfig" ^
  -H "Authorization: Bearer base64-credential"
```

### 5.2 常用管理接口

当前文档上一版遗漏了部分已经对外开放的管理能力。除了认证、QQ 登录和配置接口外，仓库里还开放了日志、终端、文件管理、镜像测试、版本更新、插件管理、NapCat 配置、调试接口等路由。

#### 基础信息与状态

- `GET /api/base/QQVersion`
- `GET /api/base/GetNapCatVersion`
- `GET /api/base/getLatestTag`
- `GET /api/base/getAllReleases`
- `GET /api/base/getMirrors`
- `GET /api/base/GetNapCatFileHash`
- `GET /api/base/GetSysStatusRealTime`
- `GET /api/base/proxy`
- `GET /api/base/Theme`
- `POST /api/base/SetTheme`

#### 认证相关

- `POST /api/auth/login`
- `POST /api/auth/check`
- `POST /api/auth/logout`
- `POST /api/auth/update_token`

#### QQ 登录相关

- `POST /api/QQLogin/CheckLoginStatus`
- `POST /api/QQLogin/GetQQLoginQrcode`
- `POST /api/QQLogin/RefreshQRcode`
- `POST /api/QQLogin/PasswordLogin`
- `POST /api/QQLogin/CaptchaLogin`
- `POST /api/QQLogin/NewDeviceLogin`
- `POST /api/QQLogin/GetQQLoginInfo`
- `POST /api/QQLogin/RestartNapCat`

#### OneBot 配置相关

- `POST /api/OB11Config/GetConfig`
- `POST /api/OB11Config/SetConfig`
- `GET /api/OB11Config/ExportConfig`
- `POST /api/OB11Config/ImportConfig`

#### WebUI 配置相关

- `GET /api/WebUIConfig/GetConfig`
- `POST /api/WebUIConfig/UpdateConfig`
- `GET /api/WebUIConfig/GetDisableWebUI`
- `POST /api/WebUIConfig/UpdateDisableWebUI`
- `GET /api/WebUIConfig/GetClientIP`
- `GET /api/WebUIConfig/GetSSLStatus`
- `POST /api/WebUIConfig/UploadSSLCert`
- `POST /api/WebUIConfig/DeleteSSLCert`

#### 日志与终端

- `GET /api/Log/GetLog`
- `GET /api/Log/GetLogList`
- `GET /api/Log/GetLogRealTime`
- `GET /api/Log/terminal/list`
- `POST /api/Log/terminal/create`
- `POST /api/Log/terminal/:id/close`

#### 进程管理

- `POST /api/Process/Restart`

#### 文件管理

- `GET /api/File/list`
- `GET /api/File/read`
- `POST /api/File/write`
- `POST /api/File/create`
- `POST /api/File/mkdir`
- `POST /api/File/delete`
- `POST /api/File/batchDelete`
- `POST /api/File/rename`
- `POST /api/File/move`
- `POST /api/File/batchMove`
- `POST /api/File/download`
- `POST /api/File/batchDownload`
- `POST /api/File/upload`
- `POST /api/File/font/upload/webui`
- `POST /api/File/font/delete/webui`
- `GET /api/File/font/exists/webui`

#### NapCat 自身配置

- `GET /api/NapCatConfig/GetConfig`
- `POST /api/NapCatConfig/SetConfig`
- `GET /api/NapCatConfig/GetUinConfig`
- `POST /api/NapCatConfig/SetUinConfig`

#### 镜像与更新

- `GET /api/Mirror/List`
- `POST /api/Mirror/SetCustom`
- `GET /api/Mirror/Test/SSE`
- `POST /api/Mirror/Test`
- `POST /api/UpdateNapCat/update`

#### 插件管理

- `GET /api/Plugin/List`
- `POST /api/Plugin/SetStatus`
- `POST /api/Plugin/Uninstall`
- `GET /api/Plugin/Config`
- `POST /api/Plugin/Config`
- `GET /api/Plugin/Config/SSE`
- `POST /api/Plugin/Config/Change`
- `POST /api/Plugin/RegisterManager`
- `GET /api/Plugin/Icon/:pluginId`
- `GET /api/Plugin/Store/List`
- `GET /api/Plugin/Store/Detail/:id`
- `POST /api/Plugin/Store/Install`
- `GET /api/Plugin/Store/Install/SSE`
- `ALL /api/Plugin/ext/:pluginId/*`
- `GET /api/Plugin/page/:pluginId/:pagePath`

#### 调试接口

- `GET /api/Debug/schemas`
- `POST /api/Debug/create`
- `POST /api/Debug/call`
- `POST /api/Debug/call/:adapterName`
- `POST /api/Debug/close/:adapterName`

### 5.2.1 实时接口补充

WebUI 侧不只有普通 HTTP 请求，还有两类实时接口值得单独说明：

- `GET /api/base/GetSysStatusRealTime`: SSE，实时系统状态
- `GET /api/Log/GetLogRealTime`: SSE，实时日志

如果要做运维面板或控制台，通常这两个接口应该直接接入。

### 5.2.2 常用 WebUI 接口详解

#### `POST /api/auth/login`

用途：

- 用 WebUI token 登录管理接口

请求体：

```json
{
  "hash": "<sha256(token + .napcat)>"
}
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "Credential": "base64-credential"
  }
}
```

接入处理建议：

- 你的客户端不应直接提交明文 token
- 先在客户端算 hash，再提交
- 拿到 `Credential` 后缓存，后续请求统一放到 `Authorization` 头里

#### `POST /api/QQLogin/CheckLoginStatus`

用途：

- 查询 QQ 是否已经登录、是否掉线、当前二维码地址和登录错误

请求：

```json
{}
```

成功响应示例：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "isLogin": false,
    "isOffline": false,
    "qrcodeurl": "https://...",
    "loginError": ""
  }
}
```

接入处理建议：

- `isLogin == true`：进入业务运行态
- `isOffline == true`：表示曾登录但当前离线，应提示重登
- `qrcodeurl`：登录页可直接展示二维码
- `loginError`：登录失败时用于提示用户

#### `POST /api/OB11Config/GetConfig`

用途：

- 获取当前账号生效的 OneBot 配置

请求：

```json
{}
```

成功响应示例：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "network": {
      "httpServers": [],
      "httpSseServers": [],
      "httpClients": [],
      "websocketServers": [],
      "websocketClients": [],
      "plugins": []
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": false,
    "parseMultMsg": false,
    "imageDownloadProxy": "",
    "timeout": {
      "baseTimeout": 10000,
      "uploadSpeedKBps": 256,
      "downloadSpeedKBps": 256,
      "maxTimeout": 1800000
    }
  }
}
```

接入处理建议：

- 该接口返回的是可直接展示或编辑的配置对象
- 登录后优先读取它，而不是自己猜配置文件路径

#### `POST /api/OB11Config/SetConfig`

用途：

- 写入 OneBot 配置

请求体格式不是对象直传，而是：

```json
{
  "config": "{\"network\":{\"httpServers\":[]}}"
}
```

也就是：

- 外层是 JSON
- `config` 字段内部是字符串化后的 JSON / JSON5 文本

接入处理建议：

- 先取 `GetConfig` 的完整返回对象
- 在客户端修改字段
- 再整体序列化回 `config` 字符串提交
- 不建议只提交局部片段

#### `GET /api/WebUIConfig/GetConfig`

用途：

- 获取 WebUI 服务本身配置

成功响应示例：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "host": "::",
    "port": 6099,
    "loginRate": 10,
    "disableWebUI": false,
    "accessControlMode": "none",
    "ipWhitelist": [],
    "ipBlacklist": [],
    "enableXForwardedFor": false
  }
}
```

接入处理建议：

- `disableWebUI` 可用于判断面板是否被禁用
- `accessControlMode`、`ipWhitelist`、`ipBlacklist` 适合放到运维面板

#### `POST /api/WebUIConfig/UpdateConfig`

用途：

- 更新 WebUI 配置

请求体示例：

```json
{
  "host": "0.0.0.0",
  "port": 6100,
  "loginRate": 10,
  "disableWebUI": false,
  "accessControlMode": "whitelist",
  "ipWhitelist": ["127.0.0.1"],
  "ipBlacklist": [],
  "enableXForwardedFor": false
}
```

接入处理建议：

- 可以只传你要改的字段
- 端口必须是 `1-65535`
- `accessControlMode` 只能是 `none`、`whitelist`、`blacklist`

### 5.3 获取当前 OneBot 配置

```bash
curl -X POST "http://127.0.0.1:6099/api/OB11Config/GetConfig" ^
  -H "Authorization: Bearer base64-credential"
```

注意：

- 该接口要求 QQ 已登录
- 已登录时优先读取 `onebot11_{uin}.json`
- 若账号专属配置不存在，则回退到 `onebot11.json`

### 5.4 获取和更新 WebUI 配置

查询：

```bash
curl "http://127.0.0.1:6099/api/WebUIConfig/GetConfig" ^
  -H "Authorization: Bearer base64-credential"
```

更新：

```bash
curl -X POST "http://127.0.0.1:6099/api/WebUIConfig/UpdateConfig" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer base64-credential" ^
  -d "{\"host\":\"0.0.0.0\",\"port\":6100,\"loginRate\":10}"
```

### 5.5 SSL 启用方式

WebUI 启动时会检查：

- `config/cert.pem`
- `config/key.pem`

如果两个文件都存在，则 WebUI 自动改为 HTTPS 模式。

也可以通过管理接口上传：

- `POST /api/WebUIConfig/UploadSSLCert`

请求体：

```json
{
  "cert": "-----BEGIN CERTIFICATE----- ...",
  "key": "-----BEGIN PRIVATE KEY----- ..."
}
```

## 6. 插件相关额外对接点

如果运行目录下存在 `plugins/`，OneBot 初始化时会加载插件管理器。

当前仓库还暴露了几类插件路由：

- `/plugin/:pluginId/api/*`
- `/plugin/:pluginId/mem/*`
- `/plugin/:pluginId/files/*`
- `/plugin/:pluginId/page/:pagePath`

这些接口属于插件扩展能力，不建议业务系统直接依赖为主对接面，除非你明确在做 NapCat 插件生态集成。

## 7. 对接建议

建议按下面顺序接入：

1. 先确认 `config/onebot11.json` 中启用了你要的网络适配器
2. 给 OneBot 和 WebUI 都配置明确 token，不要留空
3. 登录 QQ 后先调用 `get_login_info` 验证业务接口
4. 再订阅消息事件
5. 需要运维控制时，再接 `/api` 下的管理接口

推荐实践：

- ID 一律按字符串处理
- 不要把 WebUI 管理接口当成稳定公开协议，业务层优先走 OneBot
- 生产环境务必启用 token
- 如果使用反向 HTTP，上游服务要实现 `x-signature` 校验
- 如果使用 WebUI 且有公网暴露需求，优先配合 IP 白名单或 HTTPS

## 8. 最小联调清单

### 业务侧

1. `POST /get_login_info`
2. `POST /send_private_msg` 或 `POST /send_group_msg`
3. 建立 WebSocket 或 SSE 连接并确认收到 `lifecycle` / `heartbeat`

### 管理侧

1. `POST /api/auth/login`
2. `POST /api/auth/check`
3. `POST /api/OB11Config/GetConfig`
4. `GET /api/WebUIConfig/GetConfig`

## 9. 补充说明

本仓库里已经包含生成 OpenAPI 文档的能力，位于：

- `packages/napcat-schema/index.ts`

它会遍历 OneBot Action 元数据并生成 `openapi.json`。如果后续需要更完整的接口清单，建议基于该模块生成并发布一份随版本变化的 API 文档。

另外，WebUI 调试侧还提供了一个非常适合“接口发现”的入口：

- `GET /api/Debug/schemas`

这个接口会遍历当前 OneBot Action，返回动作描述、请求 schema、响应 schema、示例 payload 和 tags。对于动态生成调试台、SDK 或内部联调页面很有价值。
