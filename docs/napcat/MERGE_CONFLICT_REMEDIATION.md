# Napcat 分支冲突预防整改方案

本文档记录需要评估后实施的改进项（P1-1、P1-2、P2-2），与 `UPSTREAM_MERGE_GUIDE.md` 配套。

---

## P1-1: `gateway/run.py` — `_emit_short_circuit_response` 统一拦截

### 问题

我们将 ~30 个 `/command` handler 的 `return EphemeralReply(...)` 逐个替换为 `return self._emit_short_circuit_response(event, ...)`。上游改任何一个 handler 的 return 语句都冲突。

### 方案

在 handler dispatch 返回路径上统一拦截，handler 内部恢复为原始的 `EphemeralReply` 返回：

```python
# gateway/run.py — dispatch 层统一处理
async def _dispatch_command(self, event: MessageEvent, handler) -> Union[str, EphemeralReply]:
    response = await handler(event)
    if isinstance(response, EphemeralReply):
        self._emit_short_circuit_response(event, str(response))
    return response
```

### 前置验证

1. 确认所有 `/command` handler 是否都经过同一个 dispatch 入口（grep `_handle_.*_command` 的调用链）
2. 确认是否有散落在消息管线中的直接 `return EphemeralReply(...)` 未走 dispatch
3. 如果存在散落调用，评估是否可以在 adapter send 路径（`_unwrap_ephemeral`）拦截

### 改动范围

- 回退 ~30 个 handler 的 return 语句到 `EphemeralReply(...)`
- 添加 1 个统一拦截方法
- 可选：在 `BasePlatformAdapter._unwrap_ephemeral` 中加 trace（如果 dispatch 路径不统一）

### 风险

- 中等。需要确认 dispatch 路径覆盖度，遗漏会导致部分命令丢失 trace

### 收益

消除 ~30 个潜在冲突点，是单项收益最高的改进。

---

## P1-2: `gateway/platforms/base.py` — 消息处理管线

### 问题

我们完全重写了 `_process_message_background` 中的旧管线代码（EphemeralReply unwrap、旧 TTS 逻辑等）。上游持续修改该区域，每次合并都冲突。

### 方案 A：rebase 脚本自动处理（低成本）

在 `scripts/napcat_merge_upstream_until_conflict.py` 中增加规则：

```python
AUTO_RESOLVE = {
    "gateway/platforms/base.py": {
        # _process_message_background 区域冲突自动取 napcat 侧
        "pattern": r"_process_message_background",
        "action": "take_napcat",
    },
}
```

**优点：** 改动极小，只改脚本
**缺点：** 盲区——上游在该区域加的新功能会被丢弃，需要人工审查

### 方案 B：提取新管线到独立方法（高收益）

将我们的新管线逻辑提取为独立方法：

```python
async def _process_message_background_v2(self, event, ...):
    """napcat 新管线：typing + handler + response + MEDIA tag + drain"""
    ...
```

`_process_message_background` 只保留一个调用入口或条件分支。

**优点：** 上游改旧管线代码时完全不冲突
**缺点：** `_process_message_background` 是 400+ 行核心方法，提取需要仔细测试

### 建议

1. 先实施方案 A（低成本，立即见效）
2. 评估方案 B 的可行性：确认旧管线代码是否已被新管线**完全替代**（不再有旧逻辑被执行到）
3. 如果旧管线确实已死，方案 B 的提取就是把死代码删掉 + 新代码重命名，风险可控

### 风险

- 方案 A：低
- 方案 B：高（核心方法重构，需全面回归测试）

### 收益

消除高频冲突区域，是合并痛苦的主要来源之一。

---

## P2-2: `gateway/run.py` — orchestrator hook

### 问题

我们在 stop/restart handler 中间插入了 orchestrator 检查（`cancel_orchestrator_children` 等），上游改 stop 逻辑时冲突。

### 现状

orchestrator 实现已通过 `NapcatGatewayMixin` 隔离到独立文件（`gateway/napcat_gateway_instrumentation.py`，393 行），但 `gateway/run.py` 中仍有 ~15 个调用点，每个 2-5 行。

### 方案

让 stop/restart handler 提供 hook 点：

```python
# gateway/run.py — stop handler 中
async def _handle_stop_command(self, event):
    ...
    # hook: before stop
    for ext in self._extensions:
        await ext.on_before_stop(session_key)
    # 原有 stop 逻辑
    ...
    # hook: after stop
    for ext in self._extensions:
        await ext.on_after_stop(session_key)
```

mixin 实现 hook：

```python
# gateway/napcat_gateway_instrumentation.py
class NapcatGatewayMixin:
    async def on_before_stop(self, session_key):
        await self.cancel_orchestrator_children(session_key, reason="stop requested")
```

### 前置验证

1. 统计所有 orchestrator 调用点，确认哪些在 hot path（stop/restart）vs cold path（session expiry、model switch）
2. cold path 的调用点冲突概率低，可能不值得加 hook

### 改动范围

- stop/restart handler：加 hook 调用（~10 行）
- mixin：实现 hook 方法（~20 行）
- 其他 lifecycle 调用点保持现状（冲突频率低）

### 风险

- 低。hook 模式成熟，且我们控制整个 fork

### 收益

中等。调用点本身只有 2-5 行，冲突概率和解决成本都不高。ROI 不如 P0 和 P2-1。

### 建议

**可延后实施。** orchestrator 的 mixin 隔离已经做得很好，调用点冲突的解决成本低（每次 ~1 分钟），不值得花时间重构 hook 机制。除非上游大幅重写 stop handler。

---

## P0: `run_agent.py` — `AIAgent.__init__` 参数对象化

### 问题

上游仍在 `AIAgent.__init__` 中使用 7 个独立参数传递 gateway 会话上下文：`user_id`、`user_name`、`chat_id`、`chat_name`、`chat_type`、`thread_id`、`gateway_session_key`。napcat 分支已将其重构为 `AgentRuntimeContext` dataclass。每次 upstream 修改这些参数的传递、校验或序列化逻辑，都会触发冲突。

### 方案

将 napcat 的 `AgentRuntimeContext` 作为**唯一**的会话上下文载体，`__init__` 签名只保留一个 `runtime_context` 参数。上游若新增字段，只需在 `AgentRuntimeContext` 中追加属性，不再触碰 `AIAgent` 签名。

```python
# agent/runtime_context.py — 唯一可信来源
@dataclass
class AgentRuntimeContext:
    user_id: str | None = None
    user_name: str | None = None
    chat_id: str | None = None
    chat_name: str | None = None
    chat_type: str | None = None
    thread_id: str | None = None
    gateway_session_key: str | None = None
    # 上游未来新增字段直接追加在这里
```

### 改动范围

- `agent/runtime_context.py`：追加缺失的上游字段（如有）
- `run_agent.py`：`AIAgent.__init__` 删除 7 个独立参数，保留 `runtime_context`
- `gateway/run.py` 及各 platform adapter：统一构造 `AgentRuntimeContext` 后传入

### 风险

- 中。调用点遍布 gateway 和各 platform adapter，需要全局替换，但改动机械、可批量脚本化。

### 收益

- **极高。** 消除 `run_agent.py` 最频繁的冲突来源。本次 13 轮冲突中，session init 参数相关冲突出现了 3 次。

---

## P0: `run_agent.py` — `split_session_on_compress` 与 session flush 去重策略统一

### 问题

napcat 使用 `self._last_flushed_db_idx` 索引跳过已 flush 的消息；upstream 新增了 `existing_keys: set` 对历史消息（JSONL 恢复场景）做内容级去重。两套机制在同一循环内重叠，upstream 任何对 flush 逻辑的修改都会与 napcat 的索引机制冲突。

### 方案

**统一为单一去重策略：**

1. 保留 `existing_keys` 作为启动时的基准 set（覆盖 JSONL 恢复场景）
2. 保留 `_last_flushed_db_idx` 作为运行时增量边界（避免每次全量扫描）
3. 在 `append_message` 层面增加幂等性：`(session_id, role, content, tool_call_id)` 组合唯一索引，重复写入自动忽略

这样循环内不再需要显式去重判断，upstream 和 napcat 的 flush 逻辑都不再需要维护 `existing_keys`。

### 改动范围

- `run_agent.py`：删除 `existing_keys` 循环内去重逻辑
- `gateway/session.py` 或 `_session_db` 实现：添加唯一索引/幂等写入

### 风险

- 中低。依赖 SQLite 表的 schema 变更或写入前查询，需确认性能影响。

### 收益

- 高。消除 session flush 区域的重复冲突，且让 upstream 的 multimodal 内容剥离逻辑可以无冲突地合并。

---

## P2-1: `run_agent.py` — 工具结果处理链提取为独立函数

### 问题

`_execute_tool_calls`（或等效方法）中，工具结果从执行到构造 `tool_msg` 的管线包含 10+ 个步骤：observability event、memory hook、guardrail observation、`maybe_persist_tool_result`、multimodal unwrapping、subdirectory hints、`timestamp` 注入等。upstream 和 napcat 不断在同一代码块追加新步骤，导致密集冲突。

### 方案

提取 `_build_tool_message(agent, function_result, tool_call, ...)` 或类方法 `AIAgent._finalize_tool_result`：

```python
def _finalize_tool_result(
    self,
    function_result,
    tool_call,
    function_name,
    function_args,
    tool_duration,
    _tool_event_ctx,
    effective_task_id,
) -> dict:
    # 1. observability / memory hooks
    # 2. guardrail
    # 3. persist
    # 4. multimodal unwrap
    # 5. timestamp
    return tool_msg
```

upstream 和 napcat 各自只在该函数内部扩展，主循环不再冲突。

### 改动范围

- `run_agent.py`：提取 1 个私有方法，主循环调用点替换为一行

### 风险

- 低。纯代码移动，无行为变更。

### 收益

- 高。本次 13 轮冲突中，tool result 处理链相关冲突出现 4 次，是最大冲突源之一。

---

## P1-3: 工具函数签名 — 用 `**kwargs` / `options` dict 消除参数冲突

### 问题

`tools/send_message_tool.py` 中 `_send_to_platform` 等函数，upstream 新增 `force_document=False`，napcat 新增 `platform_adapter=None`。双方在同一签名位置追加参数，任何新参数的引入都冲突。

### 方案

将平台相关可选参数统一收入 `options: dict | None = None`：

```python
async def _send_to_platform(
    self,
    message: str,
    recipient_id: str,
    platform: str,
    options: dict | None = None,
) -> str:
    options = options or {}
    force_document = options.get("force_document", False)
    platform_adapter = options.get("platform_adapter")
    ...
```

### 改动范围

- `tools/send_message_tool.py`：重构 `_send_to_platform` 及相关调用点
- 其他有类似模式的工具函数（如 `_send_telegram`）同步处理

### 风险

- 低。纯签名重构，调用点都在本文件内。

### 收益

- 中高。消除工具函数签名层面的所有未来冲突。

---

## P2-3: 硬编码平台列表改为动态注册

### 问题

`agent/prompt_builder.py` 的 `PLATFORM_HINTS`、文档中的平台描述列表、`hermes_cli/platforms.py` 等位置，upstream 和 napcat 不断在同一 dict/list 中追加新平台。upstream 新增 Microsoft Teams / Feishu，napcat 新增 NapCat/QQ，列表越长冲突概率越高。

### 方案

将平台元数据改为注册表模式：

```python
# agent/platform_registry.py
PLATFORM_REGISTRY: dict[str, PlatformMeta] = {}

def register_platform(name: str, meta: PlatformMeta):
    PLATFORM_REGISTRY[name] = meta

# 各平台在自身模块中注册
# gateway/platforms/napcat.py
register_platform("napcat", PlatformMeta(
    display_name="NapCat (QQ)",
    hint="QQ 群聊与私聊机器人平台",
))
```

`prompt_builder.py` 和文档生成器从 `PLATFORM_REGISTRY` 动态渲染列表，不再手写。

### 改动范围

- 新增 `agent/platform_registry.py`
- `agent/prompt_builder.py`：删除硬编码 `PLATFORM_HINTS`
- 各 platform 模块：末尾添加 `register_platform` 调用
- 文档：改为从注册表生成（可选，可手动维护）

### 风险

- 低。新增文件 + 删除硬编码，行为不变。

### 收益

- 中高。彻底消除平台列表相关冲突，且让新增平台零侵入。

---

## P2-4: `gateway/platforms/discord.py` — DM/Thread 判断逻辑通用化

### 问题

upstream 新增 `_read_dm_role_auth_guild`，napcat 新增 `_is_dm_channel_like` / `_is_thread_channel_like`。三者在 "判断 channel 类型" 的语义域重叠，upstream 任何对 DM/Thread/Guild 逻辑的修改都可能波及 napcat 的 helper。

### 方案

将 napcat 的 `_is_dm_channel_like` 和 `_is_thread_channel_like` 重命名为更通用的 `_get_channel_type_flags`，返回 dataclass：

```python
@dataclass
class ChannelFlags:
    is_dm: bool
    is_thread: bool
    is_guild: bool
    is_category: bool
```

这样 upstream 新增 guild 相关 helper 时，可以自然映射到 `ChannelFlags.is_guild`，而不是与 napcat 的独立方法冲突。

### 改动范围

- `gateway/platforms/discord.py`：重构 2 个 helper 为 1 个返回 dataclass 的方法
- 调用点：更新条件判断

### 风险

- 低。纯内部 helper 重构。

### 收益

- 中。减少 Discord adapter 区域的冲突概率。

---

## P2-5: `tests/gateway/test_run_progress_topics.py` — 按平台拆分测试文件

### 问题

upstream 和 napcat 不断在同一测试文件追加新平台相关的 progress topic 测试。upstream 加 Feishu 测试，napcat 加 NapCat 测试，每次都在文件末尾追加，极易冲突。

### 方案

按平台拆分为独立模块：

```
tests/gateway/test_run_progress_topics_feishu.py
tests/gateway/test_run_progress_topics_napcat.py
tests/gateway/test_run_progress_topics_common.py  # 平台无关的基础逻辑
```

原文件只保留 import 和公共 fixture，或完全删除。

### 改动范围

- 新建 2-3 个测试文件
- 原文件：删除已拆分的测试函数

### 风险

- 极低。纯测试文件移动，不影响生产代码。

### 收益

- 中高。消除测试文件级别的所有追加型冲突，且让测试结构更清晰。

---

## 优先级总览

| 编号 | 主题 | 优先级 | 冲突频率 | 实施成本 | 建议时机 |
|------|------|--------|----------|----------|----------|
| P0 | `run_agent.py` session init 参数对象化 | P0 | 极高 | 中 | **立即** |
| P0 | session flush 去重策略统一 | P0 | 高 | 中低 | **立即** |
| P1-1 | `gateway/run.py` `_emit_short_circuit_response` 统一拦截 | P1 | 高 | 低 | 尽快 |
| P1-2 | `gateway/platforms/base.py` 消息管线提取 | P1 | 极高 | 高 | 评估后实施 |
| P1-3 | 工具函数签名 `options` dict 化 | P1 | 中 | 低 | 尽快 |
| P2-1 | `run_agent.py` 工具结果处理链提取 | P2 | 高 | 低 | 下次合并前 |
| P2-2 | `gateway/run.py` orchestrator hook | P2 | 低 | 低 | 可延后 |
| P2-3 | 平台列表动态注册 | P2 | 中 | 低 | 下次合并前 |
| P2-4 | Discord helper 通用化 | P2 | 中 | 低 | 有空时 |
| P2-5 | 测试文件按平台拆分 | P2 | 中 | 极低 | 有空时 |

> **建议实施顺序：** P0 → P1-3 → P2-1 → P2-3 → P1-1 → P2-5 → P2-4 → P1-2（评估）→ P2-2（延后）

---

## 2026-05-13 合并实战更新

本次合并 replay 了 ~468 个上游提交，遇到 ~14 轮冲突。以下是基于实战的新增发现。

### 新发现：P0 — `run_agent.py` `_build_system_prompt` 架构反复

**问题：** 上游把 `_build_system_prompt` 重构为 `_build_system_prompt_parts` (返回 `Dict[str,str]`) + `_build_system_prompt` 包装器，并在后续提交中进一步把 parts 内部拆分为 `stable_parts` / `context_parts` / `volatile_parts`。napcat 在之前的合并中回退了这套架构（删除 parts 方法、恢复单一方法），导致本次上游再次重构时，**整个方法体 200+ 行全面冲突**。

**根因：** napcat commit 中携带了「删除 `_build_system_prompt_parts`」的 diff，每次上游重新引入或修改该方法，git rebase 都会把整段标记为冲突。

**方案（零代码改动）：**
- 停止在 napcat commit 中删除/重命名 `_build_system_prompt_parts`
- 如果 napcat 需要简化调用，**新增**自己的包装方法，不碰上游方法名
- `turn_system_context` 等 napcat 特有注入逻辑，移到 `_build_system_prompt` 包装器中处理，不修改 `_build_system_prompt_parts` 内部

**收益：** 消除 `run_agent.py` 最大冲突源（本次 3 次独立冲突，单次解决耗时 ~15 分钟）。

### 新发现：P1 — `run_agent.py` `_build_api_kwargs` 中 `tools_for_api` 变量

**问题：** 上游简化了长生命期前缀缓存逻辑后，保留了 `tools_for_api = self.tools` 赋值。napcat 之前的合并连这个简化赋值一起删除，导致本次冲突。

**方案：** 保留 `tools_for_api = self.tools`，即使当前只是透传。这样上游未来再改 tools 处理时，冲突面只限于赋值右侧。

### 新发现：P2 — `gateway/session_context.py` ContextVar 追加

**问题：** 上游新增 `_SESSION_ID`，napcat 已有 `_SESSION_CHAT_TYPE`。两者在同一 `_VAR_MAP` dict 中注册，虽然 git 本次自动合并成功，但增加了认知负担。

**方案：** 把 napcat 特有的 ContextVar 移到 `gateway/napcat_session_context.py` 扩展文件，运行时注册到 `_VAR_MAP`，不直接修改 `session_context.py`。

### 优先级调整（基于 2026-05-13 经验）

| 编号 | 主题 | 调整后优先级 | 原因 |
|------|------|-------------|------|
| 新-P0 | 不再回退 `_build_system_prompt_parts` | **P0** | 零成本，立即消除最大冲突源 |
| 原-P0 | session init 参数对象化 | P0 | 仍是 top-3 冲突源 |
| 原-P0 | session flush 去重统一 | P0 | 高优先级不变 |
| 原-P1-1 | `_emit_short_circuit_response` 拦截 | P1 | 收益最高（~30 冲突点） |
| 原-P2-1 | 工具结果处理链提取 | P1↑ | 本次冲突 4 次，实际频率比预估高 |
| 原-P1-3 | 工具函数签名 options dict 化 | P1 | 低成本高防冲突收益 |
| 原-P2-3 | 平台列表动态注册 | P2 | 本次 PLATFORM_HINTS 冲突 1 次 |
| 新-P2 | session_context.py 扩展隔离 | P2 | 预防性改进，当前冲突频率低 |
