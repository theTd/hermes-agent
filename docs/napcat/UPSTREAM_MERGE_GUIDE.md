# Upstream Merge Guide

napcat 分支同步上游的冲突解决思路与预防策略。

## 同步流程

```bash
git fetch origin
python3 scripts/napcat_merge_upstream_until_conflict.py main
# 遇到冲突 → 解决 → git add + git rebase --continue → 重新运行脚本
# 完成后: git push --force-with-lease theTd napcat
```

**禁止** `git merge upstream/main`，必须用 replay 脚本逐个重放。

---

## 冲突解决原则

### 1. 优先保留我们分支的结构性改动

napcat 分支对上游做了几类结构性改动，解决冲突时**必须保留**：

| 改动 | 文件 | 说明 |
|------|------|------|
| `_emit_short_circuit_response` | `gateway/run.py` | 替代 `EphemeralReply`，增加 trace 可观测性 |
| `_response_reply_to_message_id` | `gateway/platforms/base.py` | 集中化 reply-to 逻辑（含飞书 topic） |
| `split_session_on_compress` | `run_agent.py` | gateway 会话压缩时保持 session_id 稳定 |
| `existing_keys` 去重 | `run_agent.py` | flush 消息防重复 |
| orchestrator session 感知 | `gateway/run.py` | stop/restart 检查子任务 |
| `_adapter_for_source` 等 helper | `gateway/run.py` | adapter 抽象层 |

### 2. 上游新功能 → 两边都保留

当上游新增独立的类/方法/测试（不与我们的改动重叠），直接保留两边。例如：
- 上游加 `EphemeralReply` → 保留（我们的 `_emit_short_circuit_response` 在 run.py 层面包装它）
- 上游加 `AdapterInboundDecision` → 保留（和我们的改动不冲突）
- 上游加新测试 → 保留（测试不互相排斥）

### 3. 同一行改动 → 取我们侧，适配上游语义

当上游和我们改了同一行代码（如 return 语句、参数列表），取我们侧的写法，但确保语义兼容上游的新功能。例如：
- 上游加了 `reply_to` 参数新逻辑 → 用我们的 `_response_reply_to_message_id()` 但确保它覆盖上游的新场景
- 上游改了 session 创建参数 → 用我们的模式但传递上游新增的参数

### 4. 旧管线代码被新管线替代 → 直接删除

`gateway/platforms/base.py` 的 `_process_message_background` 中，HEAD 侧的旧消息处理管线（EphemeralReply unwrap、旧 TTS 逻辑等）已被我们重写。遇到该区域冲突时，直接取 napcat 侧（通常为空或新代码），因为冲突标记之后的代码已是我们的新管线。

---

## 可避免的重复冲突

以下冲突在每次合并时**反复出现**，通过重构可以消除。

### P0: `run_agent.py` — session 初始化

**冲突模式：** 上游用 lazy creation (`_session_db_created` + `_ensure_db_session`)，我们用 eager creation。

**解决：** 采用上游的 lazy creation 模式。我们的 `user_id` 传递需求嫁接到 `_ensure_db_session()` 方法中，而不是在 `__init__` 里直接创建 session。

```python
# 不要这样（我们的旧方式）：
if self._session_db:
    self._session_db.create_session(..., user_id=...)

# 而是这样（适配上游 lazy 模式）：
self._session_db_created = False
self._session_init_model_config = {...}
# 在 _ensure_db_session 中传递 user_id
```

### P0: `run_agent.py` — `split_session_on_compress`

**冲突模式：** 上游无条件 split session，我们用 `if self.split_session_on_compress` 分支。

**解决：** 提取为可覆盖方法：

```python
def _compress_session(self, old_session_id, new_system_prompt, old_title=None):
    """子类或 gateway extension 可覆盖此方法改变压缩行为。"""
    # 上游默认实现：always split
    ...
```

我们分支通过 gateway extension 覆盖此方法实现 in-place rewrite，上游改默认实现时不会冲突。

### P1: `gateway/run.py` — `_emit_short_circuit_response`

**冲突模式：** 每个 slash-command handler 的 `return EphemeralReply(...)` 被我们改成 `return self._emit_short_circuit_response(event, ...)`。上游改任何一个 handler 都会冲突。

**解决：** 用底层拦截替代逐个替换。在 `_process_message_background` 的返回路径或 adapter 的 send 路径统一处理：

```python
# 在 adapter 层或 runner 层统一拦截
async def _dispatch_command(self, event, handler):
    response = await handler(event)
    if isinstance(response, str) and response:
        self._emit_short_circuit_response(event, response)
    return response
```

这样 handler 内部的 return 语句保持和上游一致，不再冲突。

### P1: `gateway/platforms/base.py` — 消息处理管线

**冲突模式：** 上游持续修改 `_process_message_background` 中的旧管线代码，我们完全重写了该区域。

**解决：** 确认旧管线已被新管线完全替代后，在 rebase 脚本中增加自动处理逻辑：检测到该文件该区域的冲突时，自动取 napcat 侧并 `git add`。

### P2: `gateway/platforms/base.py` — 新增 class 位置

**冲突模式：** 我们的 `AdapterInboundDecision`、`AdapterTurnPlan` 等 dataclass 和上游的 `EphemeralReply` 定义在同一区域。

**解决：** 移到独立文件 `gateway/platforms/adapter_types.py`，从 `base.py` import。

### P2: `gateway/run.py` — orchestrator hook

**冲突模式：** 我们在 stop/restart handler 中间插入 orchestrator 检查。

**解决：** 用 hook 模式：stop handler 调用 `_on_before_stop(extension)` / `_on_after_stop(extension)`，我们把 orchestrator 逻辑放在 extension 的 hook 实现中。

### P3: `tools/send_message_tool.py` — 平台列表

**冲突模式：** 上游硬编码平台列表，我们用 `_media_delivery_support_text()` 动态生成。

**状态：** 已解决。加测试断言错误消息格式，防止上游 revert。

---

## 冲突解决 Checklist

每次遇到冲突时：

1. `git diff --name-only --diff-filter=U` — 查看哪些文件冲突
2. `grep -n '^<<<<<<<\|^=======\|^>>>>>>>' <file>` — 定位冲突标记
3. 判断冲突类型：
   - **独立新增 vs 独立新增** → 保留两边
   - **我们重构 vs 上游修改** → 取我们侧，适配上游新语义
   - **同一行竞争** → 取我们侧，检查上游是否加了新参数/新逻辑需要融入
   - **旧代码被替代** → 删除旧代码，保留新代码
4. `grep -rn '<<<<<<<' <file>` — 确认无残留冲突标记
5. `git add <file> && git rebase --continue`
6. 重新运行 replay 脚本
