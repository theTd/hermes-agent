"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from model_tools import (
    handle_function_call,
    get_all_tool_names,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)


# =========================================================================
# handle_function_call
# =========================================================================

class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self):
        result = json.loads(handle_function_call("totally_fake_tool_xyz", {}))
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_exception_returns_json_error(self):
        # Even if something goes wrong, should return valid JSON
        result = handle_function_call("web_search", None)  # None args may cause issues
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert len(parsed["error"]) > 0
        assert "error" in parsed["error"].lower() or "failed" in parsed["error"].lower()

    def test_tool_hooks_receive_session_and_tool_call_ids(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            result = handle_function_call(
                "web_search",
                {"q": "test"},
                task_id="task-1",
                tool_call_id="call-1",
                session_id="session-1",
            )

        assert result == '{"ok":true}'
        assert mock_invoke_hook.call_args_list == [
            call(
                "pre_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
            call(
                "post_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                result='{"ok":true}',
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
            call(
                "transform_tool_result",
                tool_name="web_search",
                args={"q": "test"},
                result='{"ok":true}',
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
        ]


def test_core_modules_use_agent_event_bridge():
    run_agent_source = Path("run_agent.py").read_text(encoding="utf-8")
    assert "from agent.observability_bridge import" in run_agent_source
    assert "from gateway.agent_events import" not in run_agent_source
    assert "from gateway.napcat_observability" not in run_agent_source

    model_tools_source = Path("model_tools.py").read_text(encoding="utf-8")
    assert "from agent.tool_event_instrumentation import" in model_tools_source
    assert "from gateway.agent_events import" not in model_tools_source
    assert "from gateway.napcat_observability" not in model_tools_source

    gateway_run = Path("gateway/run.py").read_text(encoding="utf-8")
    assert "from gateway.agent_events import" in gateway_run
    assert "AgentEventType as _NapCatEventType" not in gateway_run
    assert "AgentEventSeverity as _NapCatSeverity" not in gateway_run
    assert "from gateway.napcat_observability.publisher" not in gateway_run
    assert "from gateway.napcat_observability.schema" not in gateway_run
    assert "from gateway.napcat_observability.trace" not in gateway_run
    assert "_napcat_trace_ctx" not in gateway_run
    assert "napcat_referenced_image_urls" not in gateway_run
    assert "napcat_vision_cache" not in gateway_run

    gateway_agent_events = Path("gateway/agent_events.py").read_text(encoding="utf-8")
    assert "from gateway.observability_backend import" in gateway_agent_events
    assert "from gateway.napcat_observability.publisher" not in gateway_agent_events
    assert "from gateway.napcat_observability.schema" not in gateway_agent_events
    assert "from gateway.napcat_observability.trace" not in gateway_agent_events

    runtime_extension_source = Path("gateway/runtime_extension.py").read_text(encoding="utf-8")
    assert "from gateway.runtime_extension_loader import" in runtime_extension_source
    assert "NapCatGatewayExtension" not in runtime_extension_source

    config_extension_source = Path("gateway/config_extension.py").read_text(encoding="utf-8")
    assert "from gateway.config_extension_loader import" in config_extension_source
    assert "NapCatGatewayConfigExtension" not in config_extension_source

    observability_backend_source = Path("gateway/observability_backend.py").read_text(encoding="utf-8")
    assert "from gateway.observability_backend_loader import" in observability_backend_source
    assert "NapCatObservabilityBackend" not in observability_backend_source

    cli_main_source = Path("hermes_cli/main.py").read_text(encoding="utf-8")
    assert "from hermes_cli.commands_extension import" in cli_main_source
    assert "from hermes_cli.napcat_commands import" not in cli_main_source

    cli_commands_extension_source = Path("hermes_cli/commands_extension.py").read_text(encoding="utf-8")
    assert "from hermes_cli.commands_extension_loader import" in cli_commands_extension_source
    assert "register_napcat_cli_commands" not in cli_commands_extension_source


# =========================================================================
# Agent loop tools
# =========================================================================

class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Pre-tool-call blocking via plugin hooks
# =========================================================================

class TestPreToolCallBlocking:
    """Verify that pre_tool_call hooks can block tool execution."""

    def test_blocked_tool_returns_error_and_skips_dispatch(self, monkeypatch):
        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked by policy"}]
            return []

        dispatch_called = False
        _orig_dispatch = None

        def fake_dispatch(*args, **kwargs):
            nonlocal dispatch_called
            dispatch_called = True
            raise AssertionError("dispatch should not run when blocked")

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"error": "Blocked by policy"}
        assert not dispatch_called

    def test_blocked_tool_skips_read_loop_notification(self, monkeypatch):
        notifications = []

        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked"}]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")))
        monkeypatch.setattr("tools.file_tools.notify_other_tool_call",
                            lambda task_id: notifications.append(task_id))

        result = json.loads(handle_function_call("web_search", {"q": "test"}, task_id="t1"))
        assert result == {"error": "Blocked"}
        assert notifications == []

    def test_invalid_hook_returns_do_not_block(self, monkeypatch):
        """Malformed hook returns should be ignored — tool executes normally."""
        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [
                    "block",
                    {"action": "block"},           # missing message
                    {"action": "deny", "message": "nope"},
                ]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"ok": True}

    def test_skip_flag_prevents_double_block_check(self, monkeypatch):
        """When skip_pre_tool_call_hook=True, blocking is not checked (caller did it)."""
        hook_calls = []

        def fake_invoke_hook(hook_name, **kwargs):
            hook_calls.append(hook_name)
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        handle_function_call("web_search", {"q": "test"}, task_id="t1",
                             skip_pre_tool_call_hook=True)

        # Hook still fires for observer notification, but get_pre_tool_call_block_message
        # is not called — invoke_hook fires directly in the skip=True branch.
        assert "pre_tool_call" in hook_calls
        assert "post_tool_call" in hook_calls


# =========================================================================
# Legacy toolset map
# =========================================================================

class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools", "terminal_tools", "vision_tools", "moa_tools",
            "image_tools", "skills_tools", "browser_tools", "cronjob_tools",
            "rl_tools", "file_tools", "tts_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"

    def test_values_are_lists_of_strings(self):
        for name, tools in _LEGACY_TOOLSET_MAP.items():
            assert isinstance(tools, list), f"{name} is not a list"
            for tool in tools:
                assert isinstance(tool, str), f"{name} contains non-string: {tool}"


# =========================================================================
# Backward-compat wrappers
# =========================================================================

class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "web_search" in names
        assert "terminal" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("web_search")
        assert result is not None
        assert isinstance(result, str)

    def test_get_toolset_for_unknown_tool(self):
        result = get_toolset_for_tool("totally_nonexistent_tool")
        assert result is None

    def test_tool_to_toolset_map(self):
        assert isinstance(TOOL_TO_TOOLSET_MAP, dict)
        assert len(TOOL_TO_TOOLSET_MAP) > 0


# =========================================================================
# _coerce_number — inf / nan must fall through to the original string
# (regression: fix: eliminate duplicate checkpoint entries and JSON-unsafe coercion)
# =========================================================================

class TestCoerceNumberInfNan:
    """_coerce_number must honor its documented contract ("Returns original
    string on failure") for inf/nan inputs, because float('inf') and
    float('nan') are not JSON-compliant under strict serialization."""

    def test_inf_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("inf") == "inf"

    def test_negative_inf_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("-inf") == "-inf"

    def test_nan_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("nan") == "nan"

    def test_infinity_spelling_returns_original_string(self):
        from model_tools import _coerce_number
        # Python's float() parses "Infinity" too — still not JSON-safe.
        assert _coerce_number("Infinity") == "Infinity"

    def test_coerced_result_is_strict_json_safe(self):
        """Whatever _coerce_number returns for inf/nan must round-trip
        through strict (allow_nan=False) json.dumps without raising."""
        from model_tools import _coerce_number
        for s in ("inf", "-inf", "nan", "Infinity"):
            result = _coerce_number(s)
            json.dumps({"x": result}, allow_nan=False)  # must not raise

    def test_normal_numbers_still_coerce(self):
        """Guard against over-correction — real numbers still coerce."""
        from model_tools import _coerce_number
        assert _coerce_number("42") == 42
        assert _coerce_number("3.14") == 3.14
        assert _coerce_number("1e3") == 1000
