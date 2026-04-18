from unittest.mock import MagicMock, patch

from agent.reply_usage_footer import (
    append_reply_usage_footer,
    build_reply_usage_footer,
    capture_usage_snapshot,
    diff_usage_snapshots,
    extract_delegate_usage_from_messages,
    merge_usage_dicts,
)


def test_capture_and_diff_usage_snapshots():
    agent = MagicMock(
        session_input_tokens=120,
        session_output_tokens=80,
        session_cache_read_tokens=40,
        session_cache_write_tokens=10,
        session_reasoning_tokens=60,
        session_api_calls=3,
    )
    before = capture_usage_snapshot(agent)

    agent.session_input_tokens = 180
    agent.session_output_tokens = 200
    agent.session_cache_read_tokens = 55
    agent.session_cache_write_tokens = 10
    agent.session_reasoning_tokens = 150
    agent.session_api_calls = 5
    after = capture_usage_snapshot(agent)

    diff = diff_usage_snapshots(before, after)
    assert diff == {
        "input_tokens": 60,
        "output_tokens": 120,
        "cache_read_tokens": 15,
        "cache_write_tokens": 0,
        "reasoning_tokens": 90,
        "api_calls": 2,
    }


def test_build_reply_usage_footer_includes_expected_fields():
    usage = {
        "input_tokens": 120,
        "output_tokens": 80,
        "cache_read_tokens": 40,
        "cache_write_tokens": 10,
        "reasoning_tokens": 60,
        "api_calls": 2,
    }

    with patch("agent.reply_usage_footer.estimate_usage_cost") as mock_cost:
        mock_cost.return_value = MagicMock(amount_usd=0.0123, status="estimated")
        footer = build_reply_usage_footer(
            usage,
            model="openai/gpt-5.4",
            provider="openai",
        )

    assert "model=openai/gpt-5.4" in footer
    assert "calls=2" in footer
    assert "prompt=170" in footer
    assert "input=120" in footer
    assert "cache_read=40" in footer
    assert "cache_write=10" in footer
    assert "hit=24%" in footer
    assert "output=80" in footer
    assert "reasoning=60" in footer
    assert "total=250" in footer
    assert "cost=~$0.0123" in footer


def test_append_reply_usage_footer_keeps_original_text():
    combined = append_reply_usage_footer("Hello", "[usage] total=42")
    assert combined == "Hello\n\n[usage] total=42"


def test_extract_delegate_usage_from_messages():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "delegate_task", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": (
                '{"results":[{"api_calls":2,"tokens":{"input":100,"output":40,'
                '"cache_read":10,"cache_write":5,"reasoning":20,"total":155}}],'
                '"total_duration_seconds":1.23}'
            ),
        },
    ]

    usage = extract_delegate_usage_from_messages(messages)
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "reasoning_tokens": 20,
        "api_calls": 2,
    }


def test_merge_usage_dicts_sums_parent_and_child_usage():
    merged = merge_usage_dicts(
        {
            "input_tokens": 50,
            "output_tokens": 25,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
            "reasoning_tokens": 10,
            "api_calls": 1,
        },
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
            "reasoning_tokens": 20,
            "api_calls": 2,
        },
    )
    assert merged == {
        "input_tokens": 150,
        "output_tokens": 65,
        "cache_read_tokens": 15,
        "cache_write_tokens": 5,
        "reasoning_tokens": 30,
        "api_calls": 3,
    }
