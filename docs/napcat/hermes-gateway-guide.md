# 平台适配指南

本文面向第一次接触 Hermes gateway 的开发者，目标不是“解释概念”，而是让你在**不参考其他现有 adapter 实现**的情况下，也能独立写出一个可用、可维护、符合框架契约的新平台 adapter。

阅读方式：

- [gateway/platforms/ADDING_A_PLATFORM.md](C:/Users/cui/standalone/hermes-agent/gateway/platforms/ADDING_A_PLATFORM.md) 负责说明“要改哪些接线点”
- 本文负责说明“adapter 本体应该怎么写，运行时必须满足哪些契约，哪些能力可以降级，哪些不能乱做”

如果你只看一份文档就想动手，优先看本文；接线时再回到 `ADDING_A_PLATFORM.md` 逐项注册。

## 1. 先建立正确心智模型

一个 Hermes 平台 adapter 不是“消息发送 SDK 的薄封装”，而是一个长期运行的异步桥接器。它在系统中的职责是：

- 与外部 IM 平台建立连接并持续接收事件
- 把平台原始事件标准化为 `MessageEvent`
- 通过 `self.handle_message(event)` 把消息交给 gateway
- 把 gateway/agent 的响应按平台能力送回去
- 处理 typing、thread、编辑、媒体、审批交互、重试、断线恢复

它**不负责**这些事情：

- 不负责 Agent 推理循环
- 不负责工具调用编排
- 不负责 session reset 策略判定
- 不负责 prompt 构建
- 不负责 cron 调度

这些都由 gateway 其他层完成。adapter 的职责是“把平台语义正确映射到 Hermes 的统一运行时”。

## 2. 你真正要实现的能力边界

### 2.1 必须实现

这是一个新平台 adapter 至少要做到的能力：

- `connect()`：建立连接、启动监听，成功时调用 `_mark_connected()`
- `disconnect()`：关闭连接、取消后台任务、释放锁
- `send()`：发送文本消息，返回 `SendResult`
- `send_typing()`：发送 typing 指示，哪怕是 no-op 也要有明确实现
- `get_chat_info()`：返回 `{name, type, chat_id}`
- 入站消息标准化：能正确构造 `SessionSource` 和 `MessageEvent`
- 使用 `self.handle_message(event)` 进入 gateway 管道
- 正确区分可重试错误与不可重试错误
- 正确清理自身启动的后台任务、连接对象和锁

如果以上任何一项不成立，这个 adapter 只能算半成品。

### 2.2 强烈建议实现

这些不是强制，但缺了会明显影响用户体验或运维质量：

- `edit_message()`：用于 streaming 和 tool progress 聚合
- `stop_typing()`：如果平台 typing 不是瞬时动作，就必须实现
- `format_message()`：做平台专属格式转换
- `MAX_MESSAGE_LENGTH` + `truncate_message()`：处理长消息
- `send_image()` / `send_image_file()` / `send_document()` / `send_voice()` / `send_video()` / `send_animation()`
- `send_exec_approval()`：危险命令审批的原生交互
- `on_processing_start()` / `on_processing_complete()`：做 reaction 或平台侧反馈

### 2.3 可以接受的降级

下面这些能力允许缺省，但要知道降级后会发生什么：

| 能力 | 不实现时的结果 |
|------|----------------|
| `edit_message()` | 流式输出和工具进度只能退化成新消息，体验更碎 |
| `stop_typing()` | 如果平台 typing 有持续状态，可能残留“正在输入” |
| 原生图片/文件/语音发送 | 系统会退化成发 URL、路径或普通文本 |
| `send_exec_approval()` | 审批退化成纯文本 `/approve` `/deny` |
| `format_message()` | 默认按原文发送，可能在 markdown/mention 上不兼容 |

### 2.4 不能靠“最小可运行”糊弄过去的点

这些点即使平台能收能发，也不能省：

- `chat_id` / `user_id` / `thread_id` 的语义不能错
- self-message / echo message 必须拦住
- `SendResult.retryable` 不能乱标
- `connect()` / `disconnect()` 不能泄漏锁和后台任务
- 不能绕开 `self.handle_message(event)` 自己调 gateway handler

## 3. 适配器和 gateway 的真实交互链路

你写 adapter 时，脑子里应该始终保留这条链路：

```text
平台原始事件
  -> adapter 解析
  -> SessionSource
  -> MessageEvent
  -> self.handle_message(event)
  -> BasePlatformAdapter 并发/中断/typing 管理
  -> GatewayRunner._handle_message()
  -> SessionStore / AIAgent
  -> gateway 回调 adapter.send()/edit_message()/send_*()
```

关键结论：

- 你只负责平台侧“翻译”
- 不要在 adapter 内复写 gateway 的流程

## 4. 先准备一份可照着填的 adapter 模板

下面这个模板不是示意图，而是推荐的最小结构。你可以直接按这个骨架开始。

```python
import asyncio
import logging
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


def check_yourplatform_requirements() -> bool:
    # 检查依赖、环境变量、运行前提
    return True


class YourPlatformAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.YOUR_PLATFORM)
        self._client = None
        self._listen_task: Optional[asyncio.Task] = None
        self._lock_identity: Optional[str] = None

    async def connect(self) -> bool:
        try:
            # 1. 可选：申请 scoped lock
            # 2. 创建 SDK client / session
            # 3. 启动监听任务
            # 4. 成功后 _mark_connected()
            self._mark_connected()
            return True
        except Exception as exc:
            self._set_fatal_error(
                "yourplatform_connect_error",
                str(exc),
                retryable=True,
            )
            return False

    async def disconnect(self) -> None:
        # 1. 取消监听任务
        # 2. 关闭网络连接 / SDK client
        # 3. 释放 scoped lock
        # 4. _mark_disconnected()
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
            last_message_id = None
            for idx, chunk in enumerate(chunks):
                # 根据 metadata/thread_id/reply_to 发消息
                # 成功后更新 last_message_id
                pass
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=False,
            )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        return SendResult(success=False, error="Not supported")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # 瞬时 typing 平台可以直接调用一次 API
        # 持续 typing 平台可以在这里启动/刷新平台自己的 typing 机制
        return None

    async def stop_typing(self, chat_id: str) -> None:
        # 若平台 typing 有持久状态，务必停止
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

    def format_message(self, content: str) -> str:
        # 做平台 markdown / mention / 富文本转换
        return content

    async def _on_platform_event(self, raw_event: Any) -> None:
        # 1. 过滤 bot 自己发出的消息 / echo 消息
        # 2. 解析 chat/user/thread/reply/media
        # 3. build_source(...)
        # 4. 构造 MessageEvent
        # 5. await self.handle_message(event)
        source = self.build_source(
            chat_id="123",
            chat_name="Example",
            chat_type="dm",
            user_id="u_1",
            user_name="Alice",
            thread_id=None,
        )
        event = MessageEvent(
            text="hello",
            message_type=MessageType.TEXT,
            source=source,
            raw_message=raw_event,
            message_id="msg_1",
        )
        await self.handle_message(event)
```

这个模板还不完整，但它提供了正确的职责切分。后面的章节会把每一块应该填什么讲清楚。

## 5. 入站事件应该怎么映射

### 5.1 统一原则

构造 `MessageEvent` 时，重点是把平台事件稳定映射成下面这些字段：

| 字段 | 含义 | 规则 |
|------|------|------|
| `text` | 文本正文 | 没有文本就给空字符串，不要塞 display name |
| `message_type` | 消息类型 | 用最接近的 `MessageType` |
| `source.chat_id` | 会话所在 chat / room / channel / DM 主键 | 必须稳定、可重复 |
| `source.chat_type` | `dm` / `group` / `channel` / `thread` | 选能表达真实语义的那个 |
| `source.user_id` | 发信人主键 | 必须稳定，不要用昵称 |
| `source.thread_id` | thread/topic/root id | 只有存在真实 thread 语义才填 |
| `message_id` | 当前平台消息 ID | 供 reply、reaction、编辑、审批上下文使用 |
| `reply_to_message_id` | 这条消息在平台上回复的是哪条 | 不等于 thread_id |
| `reply_to_text` | 被回复消息的文本 | 有能力拿到就填，利于上下文 |
| `media_urls` | 本地文件路径列表 | 供 vision / 文档后续处理 |
| `media_types` | 媒体 MIME/type 列表 | 与 `media_urls` 对齐 |

### 5.2 DM、群聊、频道、线程如何判定

优先按“平台真实会话模型”映射，而不是按 UI 文案映射。

推荐判定：

- 私聊：`chat_type="dm"`
- 多人群：`chat_type="group"`
- 广播频道/公告频道：`chat_type="channel"`
- 平台原生 thread / forum topic / threaded reply：`thread_id=<root/thread/topic id>`

注意：

- `chat_type="thread"` 在 Hermes 里不是常规必选值，大多数 threaded 平台仍然是 `group/channel + thread_id`
- 是否按 thread 单独建 session，主要由 `thread_id` 决定

### 5.3 `thread_id` 应该怎么选

只要平台存在“在同一个 chat 内分叉子对话”的原生语义，就应尽量把那个稳定主键映射为 `thread_id`。

可以接受的来源：

- forum topic id
- thread channel id
- thread root message id
- 平台明确提供的 threaded conversation id

不应当拿来当 `thread_id` 的值：

- 当前消息 id
- 被回复消息 id（如果平台回复并不形成 thread）
- 用户 id
- chat display name

### 5.4 `reply_to_message_id` 的作用边界

这个字段只表达“本条消息回复了哪条消息”。它不参与 session key 的计算，也不能代替 thread 语义。

正确用途：

- 平台原生 reply 展示
- 在上下文里附带被回复消息文本
- 生命周期反馈（reaction / reply）

错误用途：

- 代替 `thread_id`
- 作为新的 chat 标识
- 当作 user 标识参与授权

### 5.5 媒体消息如何构造

Hermes 的统一约定是：

- `media_urls` 存本地绝对路径
- 不是远程临时 URL

因此平台附件通常要先：

1. 下载到本地
2. 放进 image/audio/document cache
3. 再把本地路径放进 `media_urls`

原因很简单：

- 远程平台 URL 可能很快过期
- vision / 文档处理通常需要稳定的本地路径

### 5.6 self-message / echo message 必须先拦截

最迟要在构造 `MessageEvent` 之前完成。

必须拦截的典型来源：

- bot 自己发出的消息
- 平台回流的 webhook echo
- message update / delivery receipt / read receipt
- 平台的系统事件但被误当成消息

如果你让这些事件流进 `handle_message()`，常见结果是：

- 机器人自言自语
- session transcript 被污染
- interrupt 机制误触发
- reaction / typing 跑到错误消息上

## 6. 出站发送应该满足什么契约

### 6.1 `send()` 的最小契约

`send()` 需要保证：

- 成功时返回 `SendResult(success=True, message_id=...)`
- 失败时返回 `SendResult(success=False, error=..., retryable=...)`
- 能正确理解 `reply_to`
- 能正确理解 `metadata`

其中 `metadata` 当前最重要的键就是：

- `thread_id`

如果你的平台支持在线程中回复，`send()` 应优先使用 `metadata["thread_id"]`。

### 6.2 `message_id` 为什么重要

`message_id` 不是装饰字段。它直接影响：

- 后续 `edit_message()`
- reaction / 审批按钮 / 平台交互更新
- 多段消息中的“最后一条消息”跟踪

如果平台 send API 能拿到消息 ID，应该尽量返回真实值。

### 6.3 长消息应该怎么发

推荐统一流程：

1. `content = self.format_message(content)`
2. `chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)`
3. 逐块发送
4. 返回最后一条消息的 `message_id`

不要自己随意截断字符串。`truncate_message()` 已经帮你处理：

- 多段编号
- 尽量保持代码块闭合
- 避免一刀切坏 markdown 结构

### 6.4 `edit_message()` 的期望行为

如果平台支持编辑消息，就应实现它。

成功契约：

- 返回 `SendResult(success=True, message_id=原 message_id 或平台返回的新 id)`

失败契约：

- 返回 `SendResult(success=False, error=...)`

不要在不支持编辑的平台里抛异常。基类与 gateway 预期的是“失败返回值”，不是崩溃。

### 6.5 `metadata["thread_id"]` 的解释规则

推荐统一策略：

- 若平台支持 thread：把 `thread_id` 当作目标线程主键
- 若平台不支持 thread：忽略该字段，但不要报错

这样 gateway 的上层逻辑不需要因平台差异写大量分支。

### 6.6 `reply_to` 与 `thread_id` 同时存在时怎么处理

优先原则：

- `thread_id` 决定发到哪个线程
- `reply_to` 决定是否显式回复某条消息

如果平台 API 同时支持这两者，就都带上。  
如果只支持其一，优先保住 `thread_id`，因为它决定上下文落点。

## 7. `SendResult.retryable` 和错误分类一定要做对

Hermes 基类会根据 `SendResult` 自动决定是否重试。  
错误分类建议按下面的口径做：

| 错误类型 | `retryable` | 说明 |
|----------|-------------|------|
| DNS/连接失败/连接重置/握手失败 | `True` | 可安全重试 |
| 平台明确返回暂时性网络错误 | `True` | 可安全重试 |
| 读写超时但请求可能已被平台接受 | `False` | 避免重复发送 |
| markdown/格式错误 | `False` | 应走纯文本降级 |
| 机器人无权限、chat 不存在、消息不可编辑 | `False` | 永久错误 |
| 用户拉黑、频道禁止发送 | `False` | 永久错误 |

一个简单原则：

- “这次请求大概率根本没到平台”才可重试
- “请求可能已经到了平台，只是客户端没确认”就不要重试

## 8. typing、streaming、tool progress 的平台契约

### 8.1 typing

基类会启动 `_keep_typing()` 定时调用 `send_typing()`。因此你要先判断平台 typing 属于哪类：

- 一次调用有效几秒：`send_typing()` 每次打一下即可
- 平台要求启动一个持续状态：`send_typing()` 内部维护状态，`stop_typing()` 负责停止
- 完全不支持：`send_typing()` 安静 no-op

### 8.2 streaming

gateway 的 streaming 依赖：

- `edit_message()` 可用
- `message_id` 真实可追踪

如果平台不支持编辑，gateway 会降级，但你要接受：

- 不会有高质量 token streaming
- tool progress 也会退化

### 8.3 tool progress

tool progress 不是 adapter 的主职责，但 adapter 需要配合：

- `send()` 能发短文本状态
- 若支持编辑，`edit_message()` 能稳定更新
- `metadata["thread_id"]` 能把状态发回正确线程

否则用户会看到工具日志刷屏到错误位置。

## 9. 媒体与文件能力应该怎么实现

### 9.1 推荐实现顺序

如果平台原生支持这些能力，建议按这个顺序补齐：

1. `send_image()`
2. `send_image_file()`
3. `send_document()`
4. `send_voice()`
5. `send_video()`
6. `send_animation()`

### 9.2 为什么即使最小可运行也建议补媒体

因为 Hermes 在发送前会自动识别：

- markdown 图片
- HTML 图片
- `MEDIA:/path/...`
- 本地图片/视频路径
- TTS 生成的音频

如果 adapter 不实现原生媒体发送，用户最终得到的往往是：

- 一个裸 URL
- 一个本地路径字符串
- 一段“文件在这里”的说明文本

这不算真正接好平台。

### 9.3 纯媒体响应是正常情况

不要假设“先有文本，再有媒体”。以下都是正常的：

- 只有图片，没有正文
- 只有语音，没有正文
- 只有文件，没有正文
- 文本和媒体混合，但文本先被 streaming 消费掉

所以 adapter 的媒体方法不能依赖“文本发送成功之后才会调用”。

## 10. 审批交互应该怎么支持

危险命令审批至少要支持文本回退：

- 用户看到纯文本提示
- 再发送 `/approve` 或 `/deny`

如果平台支持交互组件，建议实现 `send_exec_approval()`。  
这样 gateway 可以直接发：

- 按钮
- 卡片
- 交互式消息

这不是必需，但对于现代 IM 平台非常值得做。

## 11. 连接生命周期应该怎么写

### 11.1 `connect()` 推荐时序

推荐按这个顺序写：

1. 检查依赖和配置前提
2. 如有唯一凭证/唯一 session 约束，申请 scoped lock
3. 初始化 SDK client / HTTP session / websocket 连接
4. 启动监听任务
5. 必要时做一次健康检查
6. 成功后调用 `_mark_connected()`
7. 返回 `True`

失败时：

- 可恢复问题：`_set_fatal_error(..., retryable=True)` 后返回 `False`
- 不可恢复冲突：`_set_fatal_error(..., retryable=False)` 后返回 `False`

### 11.2 `disconnect()` 推荐时序

推荐按这个顺序写：

1. 取消你自己创建的后台任务
2. 关闭 websocket / stream / HTTP session / SDK client
3. 释放 scoped lock
4. 调 `_mark_disconnected()`

不要指望 gateway 自动知道你内部开了什么任务。你开出来的东西，要你自己收。

### 11.3 什么时候该用 scoped lock

只要平台存在“同一身份不能被两个本地 gateway 进程同时占用”的事实，就应该加：

- bot token
- app token
- phone number
- 本地 bridge session
- 本地客户端 profile

推荐在：

- `connect()` 成功前申请
- `disconnect()` 时释放
- 连接异常退出时也尽量释放

## 12. fatal error、重连、冲突的边界

### 12.1 应标记为 `retryable=False` 的典型情况

- 同 token / 同账号已被别的本地 gateway 占用
- 平台明确返回“只能有一个活跃连接”
- 本地桥接会话被独占
- 永久配置错误且当前进程不可能自行恢复

### 12.2 应标记为 `retryable=True` 的典型情况

- 短时网络不可达
- websocket 断线
- 平台临时服务异常
- 上游 API 短时失败

### 12.3 不要滥用异常抛出

在 adapter 边界，很多失败都应该“记录并返回失败状态”，而不是直接把异常炸到 gateway 顶层。  
特别是：

- `send()`
- `edit_message()`
- reaction / typing / 平台状态反馈

这类路径应该尽量返回 `SendResult` 或吞掉局部错误，避免把整条会话搞崩。

## 13. `SessionSource` 应该怎么选值

### 13.1 选值原则

使用 [base.py](C:/Users/cui/standalone/hermes-agent/gateway/platforms/base.py) 提供的 `build_source()`，不要手搓 dataclass。

推荐规则：

- `chat_id`: 目标 chat / room / channel 的稳定 ID
- `chat_name`: 人能看懂的名字，可为空
- `chat_type`: `dm` / `group` / `channel`
- `user_id`: 发信人的稳定 ID
- `user_name`: 人类可读名称，可为空
- `thread_id`: 平台原生 thread/topic id
- `chat_topic`: thread/topic 的可读名称，可为空
- `user_id_alt` / `chat_id_alt`: 只在平台存在第二套稳定身份时使用

### 13.2 最常见的错误映射

错误做法：

- 把 username 当作 `user_id`
- 把 chat title 当作 `chat_id`
- 把当前消息 ID 当作 `thread_id`
- 把 reply-to message ID 当作 `thread_id`

后果是 session 隔离、授权、interrupt、跨平台投递全部可能失真。

## 14. `handle_message()` 为什么绝不能绕过

这是本文里最重要的一条约束，值得单独重复。

基类 `handle_message()` 负责：

- 同 session 并发保护
- 新消息打断老消息
- 某些命令绕过活动会话保护
- photo burst 合并
- 自动 typing 生命周期
- 统一的后处理发送逻辑
- 后台任务生命周期管理

如果你不走它，等于要自己重写一套 gateway 平台运行时。  
这在 Hermes 里不是推荐扩展方式。

## 15. 能力分级：什么时候算“接好了”

### 15.1 Level 1：基础可用

达到下面条件，才算基础可用：

- 稳定收消息
- 稳定回文本
- 正确 session 隔离
- 正确 self-message 过滤
- 正确错误分类
- 连接断开后不泄漏任务/锁

### 15.2 Level 2：网关体验完整

达到下面条件，才能说和 Hermes gateway 集成良好：

- 支持 thread 语义或能明确降级
- `message_id` 可追踪
- `edit_message()` 可用或明确降级
- typing 行为正常
- 长消息分片正确
- 平台格式化可控

### 15.3 Level 3：平台原生体验完整

达到下面条件，才算“平台级体验好”：

- 原生图片/文件/语音/视频发送
- 原生审批交互
- processing feedback/reaction
- streaming 体验良好
- thread / reply 行为自然

## 16. 一套不依赖其他 adapter 的验收清单

在你没看任何现有 adapter 的前提下，只要把下面场景全部跑通，就说明实现已经比较靠谱：

### 16.1 入站场景

- DM 文本消息
- group/channel 文本消息
- thread/topic 文本消息
- reply 消息
- 图片/音频/文件消息
- bot 自己发的消息回流
- 平台非消息事件误入

### 16.2 会话场景

- 同一用户连续两条消息是否落同一 session
- 群聊不同用户是否按配置隔离
- thread 内外是否能正确区分 session
- session 中断后是否能继续处理后续消息

### 16.3 出站场景

- 普通短文本
- 超长文本自动分段
- 代码块跨段不损坏
- 带 `thread_id` 的文本发送
- reply_to + thread_id 同时存在
- 编辑消息成功
- 编辑消息失败后的降级

### 16.4 媒体场景

- 纯图片响应
- 纯文件响应
- 纯语音响应
- 文本 + 图片混合
- 文本被 streaming 消费后媒体仍能正确送达

### 16.5 运行时场景

- 新消息打断长任务
- `/stop`、`/new`、`/approve`、`/deny` 在任务进行中仍有效
- typing 不残留
- 断线重连
- 不可恢复冲突时正确进入 fatal 状态
- gateway 停止/重启后锁和后台任务被清理

## 17. 开发时最容易踩的坑

把这些坑逐个排掉，通常就不需要再翻现有 adapter：

1. 用 display name 当 `chat_id` 或 `user_id`
2. 把 reply message id 当 `thread_id`
3. 让平台临时媒体 URL 直接进入 `media_urls`
4. 不返回真实 `message_id`
5. 在 `send()` 里乱标 `retryable=True`
6. 不实现 `stop_typing()`，导致平台一直显示忙碌
7. 不释放 scoped lock
8. 不取消后台监听任务
9. 绕过 `self.handle_message(event)`
10. 只测“hello world”，不测 interrupt/thread/media

## 18. 一个真正可独立开发的建议顺序

如果你从零开始写，不参考别的 adapter，建议按这个顺序推进：

1. 写 `check_requirements()`
2. 写类骨架和 `connect()/disconnect()`
3. 先打通 DM 文本入站
4. 正确构造 `SessionSource` / `MessageEvent`
5. 打通 `send()`
6. 做 self-message 过滤
7. 做长消息分段
8. 加 `thread_id` 支持
9. 加 `edit_message()`
10. 加媒体能力
11. 加 scoped lock / fatal error 分类
12. 最后跑完整验收清单

这个顺序比一上来追求“全平台特性一次写完”更稳。

## 19. 总结

`ADDING_A_PLATFORM.md` 让你知道“系统里要接哪些线”。  
本文让你知道“adapter 本体必须满足哪些运行时契约、可以降级到哪里、怎样才算真的接好”。

如果你严格按本文实现，再回到 `ADDING_A_PLATFORM.md` 补完注册点，理论上已经不需要参考其他 adapter 才能完成新平台接入。
