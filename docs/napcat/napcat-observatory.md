# NapCat Observatory

这份文档总结当前代码库里 NapCat observability/monitoring 栈的前后端结构、数据流和职责边界，目标是让后续开发者能快速回答三个问题：

1. 这套系统到底在观测什么
2. 前端看到的数据是怎么来的
3. 哪些运行模式是完整闭环，哪些只是 API/UI 暴露

## 1. 核心定位

NapCat observability 不是通用日志面板，也不是传统 APM。

它更接近一套围绕 Hermes + NapCat 消息处理链路的 trace 可视化系统：

- 以 `NapcatEvent` 为最小观测单元
- 以 `trace` 为一次消息处理的主线
- 以 `session` 为更高层的会话聚合
- 以 `alert` 为错误/异常信号
- 以 `runtime_state` 为控制或诊断状态补充

前端并不直接把原始事件平铺展示，而是把事件重建成一个更适合人理解的执行过程：

- 消息是怎么进来的
- group trigger 是否放行
- orchestrator 做了什么决策
- 有没有生成子任务
- 模型是否流式输出
- 是否调用了工具
- 最终响应、拒绝或报错发生在什么阶段

## 2. 代码入口

### 前端

- `hermes-agent/napcat_web`
- 路由入口：`src/App.tsx`
- 页面入口：
  - `src/pages/MonitorPage.tsx`
  - `src/pages/TraceDetailPage.tsx`

### 后端

- FastAPI 入口：`hermes-agent/hermes_cli/napcat_observability_server.py`
- 运行时装配：`hermes-agent/gateway/napcat_observability/runtime_service.py`
- 数据存储：`hermes-agent/gateway/napcat_observability/store.py`
- 查询层：`hermes-agent/gateway/napcat_observability/repository.py`
- REST API：`hermes-agent/gateway/napcat_observability/query_api.py`
- WebSocket：`hermes-agent/gateway/napcat_observability/ws_hub.py`
- 聚合/写入线程：`hermes-agent/gateway/napcat_observability/ingest_client.py`
- 事件定义：`hermes-agent/gateway/napcat_observability/schema.py`
- 事件发布器：`hermes-agent/gateway/napcat_observability/publisher.py`

## 3. 数据模型

### 3.1 Event

`NapcatEvent` 是统一事件结构，字段里包含：

- trace 关联：`trace_id`, `span_id`, `parent_span_id`
- 会话上下文：`session_key`, `session_id`
- 平台上下文：`chat_type`, `chat_id`, `user_id`, `message_id`
- 事件分类：`event_type`, `severity`
- 负载：`payload`
- 时间：`ts`

`schema.py` 里定义了一套固定 `EventType`，覆盖：

- adapter 生命周期
- message 接收与解析
- group trigger
- gateway turn
- orchestrator 主流程与子任务
- agent 模型、工具、memory、skill
- final response / suppressed response
- policy / error

同一个模块还定义了两个很关键的元信息：

- 默认 severity
- 默认 UI stage

这意味着“事件如何显示”有一部分在埋点定义层就已经决定了，而不是完全留给前端推测。

### 3.2 Trace

`trace` 不是原始输入，而是聚合结果。它持久化在 `traces` 表里，字段包括：

- 主上下文：`trace_id`, `session_key`, `chat_id`, `user_id`, `message_id`
- 生命周期：`started_at`, `ended_at`
- 聚合指标：`event_count`, `tool_call_count`, `error_count`
- 状态：`status`
- 展示辅助：`latest_stage`, `active_stream`, `model`
- 语义摘要：`summary`

其中 `summary` 里会保存：

- event type 列表
- 是否有 error
- 用过哪些工具
- orchestrator/controller outcome
- child task 统计
- 最后终止事件
- headline 预览

### 3.3 Session / Alert / Runtime

- `session` 是从 `traces` 聚出来的，不是独立写入源
- `alert` 由 ingest 阶段从 ERROR 级事件派生
- `runtime_state` 用来保存控制请求或诊断状态，例如 reconnect 请求、diagnostic level

## 4. 后端分层

后端可以理解成四层。

### 4.1 Store：SQLite 持久化层

`store.py` 负责：

- 初始化独立 observability SQLite 数据库
- 管理 `events`, `traces`, `runtime_state`, `alerts`
- 提供批量插入 events
- 提供 trace upsert
- 提供 runtime / alert CRUD
- 提供 retention cleanup 和 clear-all

几个重要特征：

- 数据库默认是 `~/.hermes/napcat_observability.db`
- 使用 WAL
- 和 session transcript 数据库隔离
- `traces` 表是派生视图，不是原始事实

### 4.2 IngestClient：聚合与派生层

`ingest_client.py` 是 observability 的核心“解释器”。

它从 publisher 队列里批量取事件，然后：

1. 先把 event batch 写进 `events`
2. 再按 `trace_id` 回读整条 trace 的所有事件
3. 分析 trace 状态
4. 生成 trace summary 并 upsert 到 `traces`
5. 对 ERROR 级事件生成 alert
6. 通知 trace update / alert listeners

这意味着 trace 不是增量就地更新某几个字段，而是“看整条 trace 重新归纳一次”。

这套设计的优点：

- 聚合逻辑集中
- summary 可持续演化
- 前端不必重复承受复杂状态推导

代价：

- 每次更新 trace 都要回读该 trace 全量 events
- 长 trace 的聚合成本会随事件数增长

### 4.3 Repository + Query API：查询层

`repository.py` 提供面向 UI 的统一查询接口：

- `query_events`
- `query_traces`
- `get_trace`
- `query_sessions`
- `get_message_events`
- `query_alerts`
- `get_events_after_cursor`

它负责：

- 过滤参数拼装
- 搜索
- 特定视图条件，如 `tools` / `streaming` / `alerts`
- 分页
- 把 SQLite 行对象转成前端能直接消费的 dict

`query_api.py` 再把这些能力暴露为 `/api/napcat/*` REST 接口。

### 4.4 WebSocketHub：实时分发层

`ws_hub.py` 负责：

- 接受 WebSocket 客户端连接
- 在连接初始发送 `snapshot.init`
- 广播 `event.append`
- 广播 `trace.update`
- 广播 `alert.raised`
- 支持 per-client subscribe filters
- 支持 pause / resume
- 支持基于 cursor 的 backfill

这里的设计不是“纯事件广播”，而是混合了：

- 事件流
- 聚合结果更新
- 初始快照
- 断线补偿

这套协议正好匹配前端的 controller 模式。

## 5. 前端结构

### 5.1 页面层

前端只有两个主要页面：

- `/`：监控总览台
- `/traces/:traceId`：单 trace 详情页

`MonitorPage.tsx` 负责布局和 URL 参数同步，不承载复杂业务逻辑。

主要区域是：

- `TopStatusBar`
- `FilterBar`
- `LiveTraceConsole`
- `TraceInspector`
- `AlertsRail`
- `SessionsRail`

### 5.2 Controller 层

前端真正的核心是 `useObservabilityController.ts`。

它统一负责：

- 首次 bootstrap
- 过滤状态
- trace 选中态
- WebSocket 生命周期
- pause / resume
- reconnect
- detail 模式与 monitor 模式差异
- 断线 backfill
- URL query 对应的初始过滤恢复

前端的数据获取策略是：

1. 先用 REST 拉快照
2. 再用 WebSocket 收增量

不是单纯依赖 WebSocket 的冷启动。

这样做的好处：

- 初始页面稳定
- REST 查询更容易支持筛选、分页和 detail hydration
- WebSocket 只承担增量同步

### 5.3 Reducer + Selectors

前端本地状态是 reducer 驱动的，实体态主要包含：

- `tracesById`
- `eventsByTraceId`
- `alertsById`
- `sessionsByKey`

selectors 会进一步派生出：

- 可见 trace groups
- 可见 sessions
- 可见 alerts
- 选中 trace
- 10 秒事件速率

所以前端状态设计是：

- store 里保存基础实体
- selector 层做只读派生
- controller 负责把 REST/WS 更新喂给 reducer

## 6. 前端最重要的语义层：stage mapper

`stage-mapper.ts` 是前端理解 observability 数据的关键。

它把原始 trace + events 变成 `LiveTraceGroup`。这是 UI 真正依赖的视图模型。

`LiveTraceGroup` 里包含：

- trace 状态和时间
- latest stage
- active stream
- headline
- main orchestrator state
- child task 列表
- child details
- tool call 列表
- stream chunk 列表
- stage 列表
- raw events

### 6.1 Stage 重建

前端会把事件归到这些阶段：

- `inbound`
- `trigger`
- `context`
- `model`
- `tools`
- `memory_skill_routing`
- `final`
- `error`
- `raw`

事件优先用 `payload.stage`，否则再按 `event_type` fallback 推断。

### 6.2 Tool Call 重建

从 `agent.tool.called/completed/failed` 组合出一个逻辑 tool call：

- tool name
- started / ended
- duration
- args/result/stdout/stderr preview
- error

### 6.3 Stream 重建

从：

- `agent.reasoning.delta`
- `agent.response.delta`

还原出 thinking / response chunks。

这就是为什么 UI 能显示“思维流”和“回复流”。

### 6.4 Child Task 重建

从：

- `orchestrator.child.spawned`
- `orchestrator.child.reused`
- `orchestrator.child.completed`
- `orchestrator.child.failed`
- `orchestrator.child.cancelled`

还原出：

- child task 列表
- child trace 状态
- child 自己的 stage/tool/stream 明细

这层设计非常关键，因为它说明 observability 的目标不只是看主 agent，还要看 orchestrator 的并行/异步工作流。

### 6.5 Main Orchestrator 重建

从：

- `orchestrator.turn.started`
- `orchestrator.decision.parsed`

前端推导出主 orchestrator 的状态：

- `running`
- `spawned`
- `reused`
- `responded`
- `ignored`
- `failed`

并补充：

- `spawnCount`
- `cancelCount`
- `usedFallback`
- `reasoningSummary`
- `immediateReplyPreview`

这使 UI 能从“事件日志”提升到“决策过程”。

## 7. REST 与 WebSocket 的分工

### REST 负责

- 首屏快照
- 带筛选的 trace / event 查询
- detail 页全量 trace hydration
- sessions / alerts / runtime / stats 拉取
- actions 调用

### WebSocket 负责

- 新事件增量同步
- trace 聚合结果增量同步
- alert 增量同步
- 初始 snapshot
- reconnect 后 cursor backfill

这是一个典型的“REST for state, WS for deltas” 模型。

## 8. 运行模式

这里有一个必须明确的边界。

### 8.1 完整闭环：gateway 内 runtime service

`runtime_service.py` 启动的是完整闭环：

1. `init_publisher()`
2. `ObservabilityStore()`
3. `IngestClient.start()`
4. 可选启动 HTTP/UI server

这个模式下，链路是完整的：

- 埋点发事件
- publisher 入队
- ingest 批量落库
- trace/alert 聚合
- WS 增量广播
- 前端实时可见

### 8.2 独立命令：`napcat-monitor`

`hermes_cli.main napcat-monitor` 最终走到 `start_server()`，它主要做的是：

- 读取配置
- `create_app()`
- `uvicorn.run(app)`

从当前代码来看，这个独立 server 自己不会主动把 `IngestClient` 一并启动成完整 pipeline。

因此当前更准确的理解是：

- `napcat-monitor` 更像是 observability API/UI server
- 完整的事件采集与 trace 聚合主要依赖 gateway 进程内的 runtime service

代码里确实预留了 ingest API 和 `ingest_client` 注入点，但独立命令本身不是完整 sidecar orchestration。

这是后续开发时最容易误判的一点。

## 9. 配置

`config.py` 里 `napcat_observability` 默认配置包含：

- `enabled`
- `bind_host`
- `port`
- `retention_days`
- `queue_size`
- `max_stdout_chars`
- `max_stderr_chars`
- `allow_control_actions`
- `access_token`

需要注意：

- 默认 `enabled` 是 `False`
- 默认只绑定 `127.0.0.1`
- `allow_control_actions` 默认关闭
- `access_token` 为空时不启用登录拦截，设置后需先登录才可访问 query/actions/ws API

## 10. 当前设计的优点

### 10.1 优点

- 前后端职责边界清晰
- 事件 schema 统一
- trace 聚合集中在 ingest，而不是散落在前端
- UI 表达层次高，不是单纯日志流
- 支持 orchestrator / child task 这种高阶 agent 工作流
- REST + WS 组合合理
- 存储与 transcript 解耦

### 10.2 代价与风险

- trace 聚合依赖回读全量事件，长 trace 成本会上升
- `summary` 是半结构化 JSON，方便演进但查询能力一般
- 前端有一层较重的解释逻辑，`stage-mapper.ts` 复杂度不低
- 独立 `napcat-monitor` 的角色容易被误解为“完整 sidecar”
- runtime state 广播链路相比 event/trace/alert 没那么完整，更多像辅助控制面

## 11. 如果后续要改，建议优先关注什么

### 11.1 如果要加新的 observability 事件

优先看：

- `schema.py`
- 埋点调用点
- `ingest_client.py` 的 summary / status 推导
- `stage-mapper.ts` 的 fallback 与展示逻辑

否则很容易出现：

- 后端有事件，前端看不懂
- 前端能看到 raw event，但没有进入正确 stage
- trace summary 和 UI 表现不一致

### 11.2 如果要优化性能

优先看：

- `IngestClient._update_traces()` 的全量回读
- `stage-mapper.ts` 的重复派生成本
- bootstrap 时 `getTraces + getEvents` 的组合查询

### 11.3 如果要把独立 monitor 做成真正 sidecar

需要补齐：

- 独立 ingest pipeline 启动方式
- 远程 ingest 的正式接入路径
- runtime state 广播与控制动作闭环
- 与主 gateway 的职责和部署约束

## 12. 一句话总结

NapCat observability 的本质不是“看日志”，而是把 Hermes 处理一条 NapCat 消息时的事件流，分层重建成一个可调试、可筛选、可实时追踪的执行叙事。
