# NapCat Gateway Adapter Manual

本文是给 Hermes 使用者的操作手册，目标是把 NapCat 配置成 Hermes 的 `gateway adapter`，让 QQ 私聊和群聊消息直接进入 Hermes。

手册内容基于当前仓库已经实现的 NapCat 适配器，适用对象是：

- 已经能正常运行 Hermes
- 已经安装并登录 NapCat / QQ
- 想通过 NapCat OneBot 11 正向 WebSocket 把 QQ 接入 Hermes Gateway

## 1. 当前支持范围

当前仓库里的 NapCat 适配方式固定为：

- 传输方式：NapCat OneBot 11 正向 WebSocket
- 会话类型：QQ 私聊 + QQ 群聊
- 群聊触发：支持 `@bot`、关键词、正则、reply-to-bot、可选 fast-model LLM gate
- 权限：支持 NapCat 用户 allowlist / allow-all
- 扩展：支持群/用户级 skill 策略、群/用户级 system prompt、超管 debug、私聊读取群聊上下文授权

当前不做：

- QQ forum / thread 子会话建模
- HTTP webhook 模式接入 NapCat
- tool 级 ACL

## 2. 先决条件

在配置 Hermes 之前，先确认下面几点：

1. NapCat 已启动，且 QQ 账号已登录。
2. NapCat OneBot 11 已启用正向 WebSocket 服务。
3. `messagePostFormat` 设置为 `array`。
4. Hermes 运行环境里安装了 `aiohttp`。
5. 你知道 NapCat 的 WebSocket 地址，例如 `ws://127.0.0.1:3001/`。
6. 如果 NapCat 配了 OneBot token，你也已经拿到该 token。

## 3. 在 NapCat 侧要怎么配

Hermes 当前只依赖 NapCat OneBot 11 的正向 WebSocket。

推荐的 NapCat OneBot 配置要点：

```json5
{
  network: {
    websocketServers: [
      {
        enable: true,
        name: "WebSocket",
        host: "127.0.0.1",
        port: 3001,
        reportSelfMessage: false,
        enableForcePushEvent: true,
        messagePostFormat: "array",
        token: "your-onebot-token",
        debug: false,
        heartInterval: 30000
      }
    ]
  }
}
```

关键点：

- `messagePostFormat` 必须是 `array`
- `reportSelfMessage` 建议保持 `false`
- `token` 可以为空，但生产环境不建议留空
- WebSocket URL 通常是 `ws://127.0.0.1:3001/`

NapCat 启动后，你至少应能通过它的 OneBot 11 拿到 `get_login_info` 成功响应。

## 4. Hermes 侧的最小配置

有两种主流配置方式：

- 方式 A：用环境变量 / `~/.hermes/.env`
- 方式 B：用 `config.yaml` 的 `gateway.platforms.napcat`

最小可用条件只有两个：

- `NAPCAT_WS_URL`
- 授权策略，二选一：
  - `NAPCAT_ALLOWED_USERS`
  - `NAPCAT_ALLOW_ALL_USERS=true`

## 5. NapCat 监控面板 (Web UI)

Hermes 为 NapCat 提供了一个独立的监控面板，可以实时查看 NapCat 的运行日志、上报事件以及发送的消息。

### 5.1 启用监控服务

监控服务默认是关闭的，你需要在 `config.yaml` 中启用它：

```yaml
napcat_observability:
  enabled: true             # 必须设置为 true 以启用
  bind_host: "127.0.0.1"    # 绑定地址，默认 127.0.0.1
  port: 9120                # 监控服务端口，默认 9120
  allow_control_actions: false # 是否允许通过 Web 界面执行控制操作（如下线、重启）
```

### 5.2 启动监控服务

从现在开始，只要你用 `hermes gateway` 启动 NapCat 网关，并且
`napcat_observability.enabled: true`，监控服务就会随 gateway 自动启动，
默认监听 `http://127.0.0.1:9120`。不需要再额外手动拉一个
`hermes napcat-monitor` 进程。

如果你只是想单独调试监控面板，不启动整个 gateway，也仍然可以手动运行：

```bash
# 使用默认配置启动
hermes napcat-monitor

# 或者指定端口启动
hermes napcat-monitor --port 9120
```

启动后，你可以通过浏览器访问 `http://127.0.0.1:9120` 来查看监控面板。

### 5.3 监控数据存储

监控服务会将捕获到的事件和日志存储在本地 SQLite 数据库中。数据库文件通常位于 `~/.hermes/napcat_observability.db`（或对应 profile 目录下）。

你可以通过以下配置项控制数据保留时间：

```yaml
napcat_observability:
  retention_days: 30        # 数据保留天数
  queue_size: 10000         # 内存队列大小
```

### 4.1 用 `.env` 配置

最小示例：

```env
NAPCAT_WS_URL=ws://127.0.0.1:3001/
NAPCAT_TOKEN=your-onebot-token
NAPCAT_ALLOWED_USERS=10001,10002
NAPCAT_HOME_CHANNEL=private:10001
```

字段说明：

- `NAPCAT_WS_URL`
  - NapCat OneBot 11 正向 WebSocket 地址
- `NAPCAT_TOKEN`
  - NapCat OneBot token，可选，但如果 NapCat 开启了 token 校验，这里必须填
- `NAPCAT_ALLOWED_USERS`
  - 允许和 Hermes 对话的 QQ 号列表，逗号分隔
- `NAPCAT_ALLOW_ALL_USERS`
  - 是否允许所有 QQ 用户访问，默认不启用
- `NAPCAT_HOME_CHANNEL`
  - home channel，用于 cron 和跨平台投递
  - 必须显式写成 `group:群号` 或 `private:QQ号`
- `NAPCAT_HOME_CHANNEL_NAME`
  - home channel 的显示名，可选

如果你只想本机临时测试，也可以在当前 shell 里直接设置：

```powershell
$env:NAPCAT_WS_URL = "ws://127.0.0.1:3001/"
$env:NAPCAT_TOKEN = "your-onebot-token"
$env:NAPCAT_ALLOWED_USERS = "10001"
```

### 4.2 用 `config.yaml` 配置

如果你更倾向于把 NapCat 的高级策略写进 YAML，建议这样配：

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      token: your-onebot-token
      home_channel:
        platform: napcat
        chat_id: group:123456
        name: QQ Home
      extra:
        ws_url: ws://127.0.0.1:3001/
        group_chat_enabled: true
        super_admins:
          - "10001"
        timeout_seconds: 20
        platform_system_prompt: |
          你现在通过 QQ 与用户对话，回复要短、清楚、聊天化。
        default_system_prompt: |
          你是该 QQ 机器人的默认助理。
        extra_system_prompt: |
          如无必要，不要输出过长段落。
        cross_session_auth_store: gateway/napcat_cross_session_auth.json
        group_trigger_rules:
          "123456":
            require_at: true
            keywords: ["hermes", "助手"]
            regexes: ["^总结一下", "^帮我看"]
            allowed_users: []
            denied_users: []
        group_policies:
          "123456":
            enabled: true
            allow_skills: ["blogwatcher", "find-nearby"]
            deny_skills: ["terminal"]
            model_override:
              model: gpt-5.4
              provider: openai-codex
              base_url: https://chatgpt.com/backend-api/codex
              api_key: ${OPENAI_CODEX_API_KEY}
              api_mode: codex_responses
            default_system_prompt: |
              这是研发群，优先给出技术答案。
            extra_system_prompt: |
              对外链和命令要谨慎。
        yolo_groups:
          - "123456"
        user_policies:
          "10001":
            enabled: true
            deny_groups: []
            allow_skills: []
            deny_skills: []
            model: openai/gpt-5-mini
            provider: openrouter
            default_system_prompt: |
              这是项目负责人。
            extra_system_prompt: |
              回答时优先给结论。
```

说明：

- 环境变量和 `config.yaml` 可以混用
- 当前代码会优先从环境变量桥接：
  - `NAPCAT_WS_URL`
  - `NAPCAT_TOKEN`
  - `NAPCAT_HOME_CHANNEL`
- 高级 NapCat 策略更适合写在 `gateway.platforms.napcat.extra`

`group_policies` / `user_policies` 现在也支持静态模型路由。

推荐写法：

- 嵌套写法：`model_override: { model, provider, api_key, base_url, api_mode }`
- 直写写法：直接在 policy 下写 `model`、`provider`、`api_key`、`base_url`、`api_mode`

优先级：

1. 当前 session 里手动执行的 `/model`
2. `group_policies.<group_id>` 的模型配置
3. `user_policies.<user_id>` 的模型配置
4. 全局默认模型

这意味着你现在可以把不同 QQ 群固定到不同模型，而不会影响其他群。

### 4.3 多级模型自动提升怎么配置

NapCat 现在支持一套由 orchestrator 驱动的多级模型链路：

- `L1`：轻量一审模型，负责本轮先判断“直接回复 / 不回复 / 升级到 L2”
- `L2`：旗舰模型，只在 L1 明确升级，或 L1 实际执行复杂度超阈值时接管

配置入口在：

```yaml
gateway:
  platforms:
    napcat:
      extra:
        model_promotion:
          enabled: true
          apply_to_groups: true
          apply_to_dms: false

          l1:
            model: qwen-plus
            provider: openrouter
            api_key: ${OPENROUTER_API_KEY}
            base_url: https://openrouter.ai/api/v1
            api_mode: chat_completions

          l2:
            model: claude-opus-4.1
            provider: anthropic
            api_key: ${ANTHROPIC_API_KEY}
            base_url: https://api.anthropic.com
            api_mode: anthropic_messages

          protocol:
            no_reply_marker: "[[NO_REPLY]]"
            escalate_marker: "[[ESCALATE_L2]]"

          safeguards:
            l1_max_tool_calls: 2
            l1_max_api_calls: 4
            force_l2_toolsets:
              - terminal
              - browser
              - code_execution
              - delegate
              - mcp
              - web
```

字段说明：

- `enabled`
  - 是否启用自动提升；默认关闭
- `apply_to_groups`
  - 是否对群聊开启；默认 `true`
- `apply_to_dms`
  - 是否对私聊开启；默认 `false`
- `l1`
  - 一级模型的完整运行时配置
- `l2`
  - 二级模型的完整运行时配置
- `protocol`
  - L1 与 gateway 之间的内部控制标记；一般保持默认即可
- `safeguards`
  - 网关对 L1 的兜底复杂度限制，不建议删掉

建议理解成两层开关：

1. 群消息入口由 NapCat trigger 规则和 orchestrator 一起控制
2. `model_promotion.*` 决定“进入 Hermes 以后先用哪个模型处理”

默认行为：

- promotion 关闭时，NapCat 仍走你当前的主模型/静态模型路由
- promotion 开启时，群聊正式 turn 会先跑 `L1`
- `L1` 返回 `[[NO_REPLY]]` 时，机器人静默
- `L1` 返回 `[[ESCALATE_L2]]` 时，同一轮立即切到 `L2`
- 即使 `L1` 给了正文，只要真实工具链太重，也会被 gateway 强制升级到 `L2`

同模型优化：

- 如果 `L1` 和 `L2` 的运行时配置完全一致，promotion 会自动退化成单阶段 turn：
  - 不再给 `L1` 注入 `[[NO_REPLY]]` / `[[ESCALATE_L2]]` 协议提示词
  - 不再做“先跑 L1、再升级到同一个 L2”这种重复调用

缺项时的实际表现：

- 没配 `model_promotion.l1.model`
  - 自动提升整套关闭，不会进入 `L1 -> L2`
  - 正式 turn 直接走当前 session 模型 / 静态 policy / 全局默认模型
- 没配 `model_promotion.l2.model`
  - 自动提升整套关闭，效果和没配 `L1` 一样
  - 当前实现不支持“只跑 L1、不配 L2”
- 只配了 `L1` 或只配了 `L2`
  - 都不会进入半套 promotion 模式
  - gateway 会把 `model_promotion` 视为配置不完整并整体旁路

换句话说：

- `L1` 和 `L2` 必须同时完整，promotion 才会生效

和手动 `/model` 的关系：

- 当前 session 一旦有显式 `/model` override，本轮自动提升整轮旁路
- 优先级仍然是：
  1. 当前 session 的 `/model`
  2. 当前 turn 的自动提升模型
  3. `group_policies` / `user_policies` 里的静态模型
  4. 全局默认模型

推荐起步配置：

- `L0` 用便宜的 7B 级模型
- `L1` 用响应快、成本较低但质量稳定的模型
- `L2` 用你真正希望处理复杂任务的旗舰模型
- `force_l2_toolsets` 至少保留 `terminal`、`browser`、`code_execution`、`delegate`、`mcp`、`web`

如果你只是想按群固定模型，不想启用自动提升，只配 `group_policies` / `user_policies` 就够了，不需要打开 `model_promotion`。

## 5. 交互式配置

如果你不想手改文件，可以直接运行：

```bash
hermes gateway setup
```

在交互式菜单里选择 `NapCat / QQ`，按提示输入：

1. `NAPCAT_WS_URL`
2. `NAPCAT_TOKEN`
3. `NAPCAT_ALLOWED_USERS`
4. `NAPCAT_HOME_CHANNEL`

当前 CLI 给 NapCat 的提示要点是：

1. 在 NapCat OneBot 11 启用 forward WebSocket server
2. `messagePostFormat` 设为 `array`
3. 把 WebSocket URL 填给 Hermes
4. 如果有 token，一并填写
5. 用 `NAPCAT_ALLOWED_USERS` 或 `NAPCAT_ALLOW_ALL_USERS` 明确授权策略

## 6. 如何启动 Gateway

前台运行：

```bash
hermes gateway run
```

或：

```bash
hermes gateway
```

如果你要把它作为后台服务运行：

```bash
hermes gateway install
hermes gateway start
```

查看状态：

```bash
hermes gateway status
```

Hermes 的状态输出里已经会识别 NapCat，检查项是：

- `NAPCAT_WS_URL`
- `NAPCAT_HOME_CHANNEL`

## 7. 启动后怎么验证

推荐按这个顺序验证。

### 7.1 先验证 NapCat 本身

确保 NapCat OneBot 11 正向 WebSocket 是活的，并且当前 QQ 已登录。

如果你习惯用 HTTP 侧自测，可以先对 NapCat 做：

- `get_login_info`
- `send_private_msg`
- `send_group_msg`

只要 NapCat OneBot 自己不通，Hermes 这边不会好。

### 7.2 再验证 Hermes Gateway

启动 Hermes gateway 后，观察日志里有没有：

- NapCat 连接成功
- `get_login_info` 返回成功
- 未出现 token 错误
- 未出现 scoped lock 冲突

### 7.3 私聊验证

用允许的 QQ 号给机器人发私聊：

```text
你好
```

预期：

- Hermes 会回复
- 会话类型是 DM
- 未授权用户默认不会通过

### 7.4 群聊验证

在群里先发：

```text
@机器人 你好
```

如果你启用了 `require_at: true`，这是最稳妥的第一条测试消息。

预期：

- 机器人只在命中规则时受理
- 未命中时应保持静默

## 8. 群聊触发规则怎么理解

要让下面这套规则生效，前提是先开启：

```yaml
extra:
  group_chat_enabled: true
```

如果你只想开放部分群，可以直接写群号列表：

```yaml
extra:
  group_chat_enabled:
    - "547996548"
```

当前 NapCat 群聊受理顺序固定为：

1. 权限检查
2. `@bot`
3. 关键词
4. 正则
5. reply-to-bot
6. LLM gate

`super_admins` 仍然影响 QQ 渠道命令和调试能力，但不再绕过群聊触发规则；在群里是否回复，和普通群员一致。

如果某个群启用了：

```yaml
require_at: true
```

那么：

- 单纯关键词不会受理
- 必须 `@bot` 或回复 bot 才会继续

### 示例 1：只接受 @

```yaml
group_trigger_rules:
  "123456":
    require_at: true
```

效果：

- 只有 `@bot` 或 reply-to-bot 才进入 Hermes

### 示例 2：允许关键词

```yaml
group_trigger_rules:
  "123456":
    require_at: false
    keywords: ["hermes", "助手"]
```

效果：

- 只要消息中包含 `hermes` 或 `助手` 就会进入 Hermes

### 示例 3：普通群消息交给 orchestrator 判定

```yaml
extra:
  group_trigger_rules:
    "123456":
      require_at: false
```

效果：

- 没命中 `@bot`、关键词、正则、reply-to-bot 时
- 仍会进入 orchestrator，由主 agent 决定回复还是静默

### 8.4 群 session 默认是“按群共享”

NapCat 和大多数平台不一样，QQ 群聊默认不是“群内每人一个 session”，而是：

- 同一个 QQ 群共享一个 session
- 群里不同成员会进入同一段上下文
- 因此 `/model`、`/yolo`、上下文压缩、技能策略等都会默认以“这个群”为粒度生效

如果你想退回到“同群内每个用户各自独立 session”，可以显式打开：

```yaml
gateway:
  platforms:
    napcat:
      extra:
        group_sessions_per_user: true
```

默认共享的好处是：

- 更像真正的群聊机器人
- 可以天然做到“不同群用不同模型”
- 群里的上下文连续性更强

代价是：

- 同群成员会共享 transcript
- 某个成员在群里切 `/model`，默认影响该群当前 session

## 9. 权限和安全建议

NapCat 接到 Hermes 后，最容易配错的是授权边界。

建议按下面的顺序收紧权限：

1. 默认只用 `NAPCAT_ALLOWED_USERS`
2. 只有明确需要公开机器人时才启用 `NAPCAT_ALLOW_ALL_USERS=true`
3. 群里默认加 `require_at: true`
4. 用 `group_policies` 和 `user_policies` 缩小 skill 范围
5. 把高权限用户放入 `super_admins`

### 9.1 `NAPCAT_ALLOWED_USERS`

这是入口层授权。

示例：

```env
NAPCAT_ALLOWED_USERS=10001,10002
```

效果：

- 只有这几个 QQ 号能直接使用 Hermes

### 9.2 `NAPCAT_ALLOW_ALL_USERS`

示例：

```env
NAPCAT_ALLOW_ALL_USERS=true
```

效果：

- 所有 NapCat / QQ 用户都能进入授权检查后的对话路径

不建议默认开启。

### 9.3 群/用户级 skill 策略

示例：

```yaml
group_policies:
  "123456":
    allow_skills: ["blogwatcher"]
    deny_skills: ["terminal"]
user_policies:
  "10001":
    deny_skills: ["skill-installer"]
```

效果：

- 某群可以只开少数技能
- 某用户可以额外禁用某些技能

### 9.4 群/用户级模型策略

如果你希望“研发群用强模型，闲聊群用便宜模型”，现在可以直接按群配。

示例：

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        group_policies:
          "123456":
            model_override:
              model: gpt-5.4
              provider: openai-codex
              base_url: https://chatgpt.com/backend-api/codex
              api_key: ${OPENAI_CODEX_API_KEY}
              api_mode: codex_responses
          "654321":
            model: qwen-max-latest
            provider: openrouter
            base_url: https://openrouter.ai/api/v1
            api_key: ${OPENROUTER_API_KEY}
            api_mode: chat_completions
```

效果：

- 群 `123456` 默认走 Codex / GPT-5.4
- 群 `654321` 默认走 OpenRouter 上的 Qwen
- 两个群互不影响

补充说明：

- 群 policy 会覆盖 user policy
- 当前 session 手动执行 `/model` 仍然比 policy 更高优先级
- `/new` 会清掉当前 session 的 `/model` 覆盖，但不会改你写在 YAML 里的群策略

### 9.5 QQ 渠道命令默认只给超管

当前 NapCat 平台上的 slash 命令默认只允许 `super_admins` 使用。

这包括：

- `/model`
- `/yolo`
- `/new`
- `/sethome`
- 其他 gateway 命令

非超管在 QQ 里发命令时，会直接收到限制提示，不会进入命令逻辑。

这样做的目的，是避免群里普通用户直接改 session 行为、模型路由或高风险策略。

### 9.6 某些群默认跳过危险命令审批

如果你明确知道某个 QQ 群就是受信环境，可以把它加入 `yolo_groups`：

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        yolo_groups:
          - "123456"
          - "group:654321"
```

效果：

- 这些群对应的 session 会默认开启 YOLO
- 重启 gateway 后依然会自动生效
- 群里仍然可以手动发 `/yolo` 临时关掉或重新打开当前 session

不建议把陌生人会进来的群放进这个列表。

## 10. Home Channel 怎么配

NapCat 的 home channel 不接受裸数字，推荐始终带前缀。

合法示例：

- `group:123456`
- `private:10001`

用途：

- cron 输出投递
- 跨平台消息投递
- `send_message` 等工具的默认目标

例如：

```env
NAPCAT_HOME_CHANNEL=group:123456
```

如果没配 home channel，NapCat 仍可接收实时消息，但部分自动投递能力没有默认目标。

## 11. 私聊读取群聊上下文怎么启用

NapCat 适配器支持“私聊读取群聊上下文”的临时授权，但它有严格边界：

- 只支持私聊读取群聊
- 只对当前私聊 session 生效
- 会检查用户当前是否仍在目标群中
- 注入的是只读群聊摘要，不是整段 transcript

授权状态默认存放在：

```text
<HERMES_HOME>/gateway/napcat_cross_session_auth.json
```

也可以通过：

```yaml
cross_session_auth_store: gateway/napcat_cross_session_auth.json
```

来自定义。

用户在私聊中表达“授权读取某群上下文”的自然语言后，Hermes 会在当前私聊 session 注入该群的只读摘要背景。

## 12. `send_message` 里怎么发到 NapCat

当前仓库已经把 NapCat 接到 `send_message` 工具里。

显式目标格式必须是：

- `napcat:group:123456`
- `napcat:private:10001`

示例：

```text
napcat:group:123456
```

如果只写 `napcat`，则会投递到 `NAPCAT_HOME_CHANNEL`。

### 12.1 NapCat 的媒体发送行为

现在 `send_message` 发到 NapCat 时，已经按媒体类型做原生段发送：

- 图片：走 `image` 段
- 语音 / 音频：走 `record` 段
- 其他附件：走 `file` 段

这比早期“多数东西都按普通文件发”更接近 QQ 原生体验。

如果 Hermes 和 NapCat 不在同一台机器，仍然要确保：

- 你配置了可用的 `file_path_map`
- 或者让 Hermes 能把文件暂存到 NapCat 能访问到的共享目录

### 12.2 NapCat session 内可读当前会话历史

NapCat session 现在多了一个专用工具：`napcat_read_history`。

它的特点是：

- 只能在当前 NapCat 会话里使用
- 自动绑定当前群聊或当前私聊
- 群聊走 `get_group_msg_history`
- 私聊走 `get_friend_msg_history`

常见参数：

```json
{
  "count": 20,
  "message_seq": 0,
  "reverse_order": false
}
```

适合场景：

- 让 agent 回看当前 QQ 群稍早一些的聊天记录
- 做“刚才谁提过这个话题”的补充判断
- 在当前私聊里翻阅最近历史消息

## 13. 常见故障排查

### 13.1 Gateway 启动后 NapCat 没连上

先检查：

- `NAPCAT_WS_URL` 是否正确
- NapCat 的正向 WebSocket 是否真的在监听
- Hermes 环境里是否装了 `aiohttp`

如果日志里出现：

- `napcat_missing_ws_url`
  - 没配 `NAPCAT_WS_URL`
- `napcat_missing_dependency`
  - 缺少 `aiohttp`
- `napcat_lock_conflict`
  - 已有另一个本地 Hermes gateway 占用了同一个 NapCat WS

### 13.2 token 错误

表现：

- 能连到 WebSocket，但 action 调用失败
- `get_login_info` 不成功

检查：

- NapCat 的 token 和 Hermes 的 `NAPCAT_TOKEN` 是否一致
- 是否把 WebUI token 错当成 OneBot token

Hermes 这里要的是 OneBot token，不是 WebUI 登录凭证。

### 13.3 群里不回消息

优先检查：

1. 该发言用户是否通过 NapCat 授权入口
2. 群规则是否启用了 `require_at`
3. 是否没有命中关键词/正则/reply-to-bot/LLM gate
4. 群或用户 policy 是否被 `enabled: false`

补一条容易误判的现象：

- 某些群消息虽然没有触发正式回复，但仍可能被静默写入该群共享 session，作为后续上下文

这意味着：

- “这条消息机器人没回” 不等于 “这条消息完全没被看到”
- 后面一旦触发正式回复，模型可能已经带着刚才群里的上下文

这是当前 NapCat 群共享 session 设计的一部分，不是异常。

### 13.4 私聊可以，群聊不可以

通常是：

- 群规则过严
- 机器人不在群里
- 群消息没有命中触发规则
- 该用户被 `denied_users` 或 `denied_groups` 限制

### 13.5 `send_message` 发 NapCat 失败

检查目标格式是否正确。

对 NapCat，推荐写法是：

- `napcat:group:123456`
- `napcat:private:10001`

不要写成：

- `napcat:123456`

因为 standalone 发送需要明确知道这是群还是私聊。

如果是媒体发送失败，再额外检查：

- 图片/音频/文件的本地路径对 NapCat 是否可见
- `file_path_map` 是否配置正确
- 是否把 Windows 路径、WSL 路径、Docker 挂载路径混用了

## 14. 推荐的最小生产配置

如果你想先稳一点上线，推荐从这套开始：

`.env`:

```env
NAPCAT_WS_URL=ws://127.0.0.1:3001/
NAPCAT_TOKEN=replace-with-real-token
NAPCAT_ALLOWED_USERS=10001
NAPCAT_HOME_CHANNEL=private:10001
```

`config.yaml`:

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        ws_url: ws://127.0.0.1:3001/
        group_trigger_rules:
          "123456":
            require_at: true
        group_policies:
          "123456":
            deny_skills: ["terminal"]
```

这套配置的特点是：

- 入口用户范围小
- 群聊默认必须 `@bot`
- 不保留额外前置 LLM gate
- 把高风险 skill 先关掉

## 15. 最后检查清单

在你认为配置完成之前，逐项确认：

1. NapCat OneBot 11 正向 WebSocket 已启用
2. `messagePostFormat=array`
3. `NAPCAT_WS_URL` 正确
4. 如果 NapCat 配了 token，`NAPCAT_TOKEN` 也正确
5. 已设置 `NAPCAT_ALLOWED_USERS` 或 `NAPCAT_ALLOW_ALL_USERS`
6. 群聊策略符合你的预期
7. home channel 写成 `group:` 或 `private:` 前缀格式
8. `hermes gateway status` 能看到 NapCat 配置
9. 私聊测试通过
10. 群聊触发测试通过

如果这 10 项都没问题，NapCat 作为 Hermes gateway adapter 基本就已经配好了。
