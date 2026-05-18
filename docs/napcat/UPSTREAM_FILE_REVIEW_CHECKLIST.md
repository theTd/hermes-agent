# Napcat 上游文件改动审查清单

本清单用于在修改 napcat 分支时审查对上游文件的改动面。

## 核心原则

> 尽最大努力将修改面控制在**本分支新增的文件**中。
> 如果必须修改上游文件，每次改动前都要问自己：这个改动能不能放到 napcat 自己的文件里？

## 高风险上游文件（改动需特别审查）

| 文件 | 风险等级 |  napcat 当前注入行数 | 备注 |
|------|---------|---------------------|------|
| `run_agent.py` | 🔴 高 | ~0（已提取到 `agent/napcat_agent_instrumentation.py`） | 已完成提取 |
| `gateway/run.py` | 🔴 高 | ~60（已提取部分到 mixin） | 仍含 extension init + _build_gateway_extension |
| `gateway/session.py` | 🟡 中 | ~40（resolve_session_isolation + is_multi_user_session） | resolve_session_isolation 提供平台 override 能力 |
| `model_tools.py` | 🟡 中 | 待审计 | 核心工具编排 |
| `cli.py` | 🟡 中 | 待审计 | CLI 入口 |
| `tools/registry.py` | 🟢 低 | 0 | 通常不修改 |

## 审查 Checklist

提交 PR 前确认：

- [ ] 本次修改是否**必须**在现有上游文件中进行？能否放到 `gateway/napcat_*.py` / `agent/napcat_*.py` / `tools/napcat_*.py` 中？
- [ ] 本次修改是否增加了新的 import 到上游文件的 import 区？如果是，考虑提取到独立模块。
- [ ] 本次修改是否引入了与 upstream 功能重复的代码？（如 `_base_url_hostname` vs upstream 的 `base_url_hostname`）
- [ ] 本次修改是否会导致下次重放时产生冲突？（如修改了 upstream 正在活跃改动的区域）
- [ ] 如果修改了测试文件，是否用了 napcat 特有的平台（如 NAPCAT）替换了 upstream 的平台（如 TELEGRAM）？应该保留 upstream 的测试平台。

## 已完成的提取

- [x] `agent/napcat_agent_instrumentation.py` — 从 `run_agent.py` 提取 observability facade
- [x] `gateway/napcat_gateway_instrumentation.py` — 从 `gateway/run.py` 提取 orchestrator/eviction mixin
- [ ] `gateway/session.py` 中的 `resolve_session_isolation` — 因被 5 个文件引用，暂不迁移

## 历史冲突记录

| 重放日期 | 冲突文件 | 冲突原因 | 解决方案 |
|---------|---------|---------|---------|
| 2026-04-22 | `run_agent.py` | upstream 引入 `base_url_hostname` / `base_url_host_matches` vs napcat 本地 `_base_url_hostname` | 删除本地实现，改用 upstream 的 |
| 2026-04-22 | `run_agent.py` | upstream 移除冗余本地 import vs napcat 的本地 `from hermes_constants import get_hermes_home` | 删除本地 import，改用模块顶层导入 |
| 2026-04-22 | `gateway/run.py` | upstream 重构 shared session vs napcat 的 `is_multi_user_session` | 统一使用 upstream 的 `is_shared_multi_user_session` |
| 2026-04-22 | `gateway/session.py` | upstream 的 `shared_multi_user_session` vs napcat 的 `multi_user_session` | 统一字段名和 prompt 措辞 |
| 2026-04-22 | `tests/gateway/test_session.py` | upstream 的 TELEGRAM 平台测试 vs napcat 的 NAPCAT 平台测试 | 保留 upstream 的测试结构 |
| 2026-04-22 | `tools/image_generation_tool.py` | upstream 的 FAL_KEY 检查 vs napcat 的空代码 | 采用 upstream 的 FAL_KEY 检查 |
| 2026-04-22 | `tools/skills_tool.py` | upstream 引入 `_get_session_platform` vs napcat 直接读 env var | 采用 upstream 的方案 |
