import json

import pytest

from plugins.memory import load_memory_provider
from plugins.memory.lightrag import (
    LightRAGMemoryProvider,
    _chat_workspace,
    _derive_workspace,
    _load_lightrag_config,
    _load_napcat_authorized_group_ids,
    _user_workspace,
)
from plugins.memory.lightrag.fact_envelope import build_fact_file_source, parse_fact_file_source


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def endpoint_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("LIGHTRAG_ENDPOINT", "http://lightrag.local")
    monkeypatch.delenv("LIGHTRAG_API_KEY", raising=False)
    monkeypatch.delenv("LIGHTRAG_WORKSPACE", raising=False)
    return tmp_path


def test_load_provider_by_name():
    provider = load_memory_provider("lightrag")
    assert provider is not None
    assert provider.name == "lightrag"


def test_is_available_requires_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LIGHTRAG_ENDPOINT", raising=False)
    provider = LightRAGMemoryProvider()
    assert provider.is_available() is False


def test_workspace_defaults_from_profile(endpoint_env):
    cfg = _load_lightrag_config(str(endpoint_env), agent_identity="coder")
    assert cfg["workspace"] == "hermes_coder"
    assert _derive_workspace(None) == "hermes_default"
    assert _user_workspace("napcat", "10001") == "napcat_user_10001"
    assert _chat_workspace("napcat", "group", "100000001") == "napcat_group_100000001"


def test_system_prompt_distinguishes_recall_readonly_from_memory_writes(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")

    prompt = provider.system_prompt_block()

    assert "Injected LightRAG recall is read-only context for the current turn" in prompt
    assert "update durable memory through the Hermes memory tool" in prompt
    assert "To add, correct, or remove durable LightRAG-backed facts" in prompt
    assert "LightRAG recall is read-only context." not in prompt


def test_orchestrator_context_hides_lightrag_tool_instructions(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        agent_identity="coder",
        agent_context="orchestrator",
    )

    prompt = provider.system_prompt_block()

    assert "Injected LightRAG recall is read-only context for the current turn" in prompt
    assert "Use lightrag_search" not in prompt


def test_save_and_load_config_round_trip(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.save_config(
        {
            "endpoint": "http://override.local/",
            "workspace": "team-alpha",
            "memory_mode": "tools",
            "query_mode": "hybrid",
            "write_batch_turns": "2",
        },
        str(endpoint_env),
    )
    cfg = _load_lightrag_config(str(endpoint_env), agent_identity="coder")
    assert cfg["endpoint"] == "http://override.local"
    assert cfg["workspace"] == "team_alpha"
    assert cfg["memory_mode"] == "tools"
    assert cfg["query_mode"] == "hybrid"
    assert cfg["write_batch_turns"] == 2


def test_prefetch_formats_recall(monkeypatch, endpoint_env):
    captured = {}

    def fake_request(method, url, json=None, headers=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [
                        {
                            "entity_name": "Hermes",
                            "entity_type": "PROJECT",
                            "description": "Agent framework with memory plugins",
                            "file_path": "hermes_explicit_memory/hermes_coder/memory/doc.md",
                            "reference_id": "1",
                        }
                    ],
                    "relationships": [
                        {
                            "src_id": "Hermes",
                            "tgt_id": "LightRAG",
                            "description": "integrates via REST API",
                            "file_path": "hermes_explicit_memory/hermes_coder/memory/doc.md",
                            "reference_id": "1",
                        }
                    ],
                    "chunks": [
                        {
                            "content": "Hermes keeps long-term recall behind a memory provider abstraction.",
                            "file_path": "hermes_explicit_memory/hermes_coder/memory/doc.md",
                            "reference_id": "1",
                        }
                    ],
                    "references": [
                        {
                            "reference_id": "1",
                            "file_path": "hermes_explicit_memory/hermes_coder/memory/doc.md",
                        }
                    ],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    result = provider.prefetch("How does Hermes memory work?")

    assert "## LightRAG Memory" in result
    assert "### Facts" in result
    assert "### Relationships" in result
    assert "### Sources" in result
    assert "Hermes" in result
    assert captured["headers"]["LIGHTRAG-WORKSPACE"] == "hermes_coder"
    assert captured["json"]["mode"] == "mix"
    assert captured["json"]["top_k"] == 20


def test_prefetch_extracts_fact_content_from_envelope_chunks(monkeypatch, endpoint_env):
    def fake_request(method, url, json=None, headers=None, **kwargs):
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [],
                    "relationships": [],
                    "chunks": [
                        {
                            "content": (
                                "---\n"
                                "hermes_fact_version: 1\n"
                                "fact_type: user_profile\n"
                                "subject_scope: napcat_user_46895556\n"
                                "source_scope: napcat_user_46895556\n"
                                "source_kind: import\n"
                                "target: user\n"
                                "---\n"
                                "内容：46895556 的名字是 SIGTERM。\n"
                                "信源：导入自 napcat_user_46895556\n"
                            ),
                            "file_path": "hermes_facts/v1/platform=napcat/fact_type=user_profile/subject=napcat_user_46895556/source=napcat_user_46895556/source_kind=import/target=user/digest=a.md",
                            "reference_id": "19",
                        }
                    ],
                    "references": [
                        {
                            "reference_id": "19",
                            "file_path": "hermes_facts/v1/platform=napcat/fact_type=user_profile/subject=napcat_user_46895556/source=napcat_user_46895556/source_kind=import/target=user/digest=a.md",
                        }
                    ],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="46895556",
        chat_id="46895556",
        chat_type="dm",
    )

    result = provider.prefetch("记得我什么")

    assert "46895556 的名字是 SIGTERM。" in result
    assert "hermes_fact_version" not in result
    assert "内容：" not in result


def test_prefetch_exposes_debug_params(monkeypatch, endpoint_env):
    def fake_request(method, url, json=None, headers=None, **kwargs):
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [
                        {
                            "entity_name": "Hermes",
                            "file_path": "hermes_explicit_memory/hermes_coder/memory/doc.md",
                        }
                    ],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    provider.prefetch("How does Hermes memory work?")

    debug = provider.get_last_prefetch_debug_info()
    assert debug["provider"] == "lightrag"
    assert debug["lane"] == "mixed"
    assert debug["query_mode"] == "mix"
    assert debug["api_called"] is True
    assert debug["request_payload"]["query"] == "How does Hermes memory work?"
    assert debug["request_payload"]["top_k"] == 20


def test_load_napcat_authorized_groups(endpoint_env):
    gateway_dir = endpoint_env / "gateway"
    gateway_dir.mkdir()
    (gateway_dir / "napcat_cross_session_auth.json").write_text(
        json.dumps({"session-1": {"groups": {"100000001": {}, "123456": {}}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert _load_napcat_authorized_group_ids(str(endpoint_env), "session-1") == ["100000001", "123456"]


def test_napcat_group_prefetch_reads_user_and_group_workspaces(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append(headers["LIGHTRAG-WORKSPACE"])
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [
                        {
                            "entity_name": "user-scope",
                            "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md",
                        },
                        {
                            "entity_name": "group-scope",
                            "file_path": "hermes_explicit_memory/napcat_group_100000001/chat/b.md",
                        },
                    ],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )
    result = provider.prefetch("what do we know?")
    assert calls == ["hermes_default"]
    assert "napcat_user_10001" in result
    assert "napcat_group_100000001" in result


def test_prefetch_rejects_mixed_scope_items(monkeypatch, endpoint_env):
    def fake_request(method, url, json=None, headers=None, **kwargs):
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [
                        {
                            "entity_name": "current-user",
                            "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md",
                        },
                        {
                            "entity_name": "mixed-user",
                            "file_path": (
                                "hermes_explicit_memory/napcat_user_10001/user/a.md"
                                "<SEP>"
                                "hermes_explicit_memory/napcat_user_20002/user/b.md"
                            ),
                        },
                    ],
                    "relationships": [],
                    "chunks": [],
                    "references": [
                        {
                            "reference_id": "keep",
                            "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md",
                        },
                        {
                            "reference_id": "drop",
                            "file_path": (
                                "hermes_explicit_memory/napcat_user_10001/user/a.md"
                                "<SEP>"
                                "hermes_explicit_memory/napcat_user_20002/user/b.md"
                            ),
                        },
                    ],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="10001",
        chat_type="dm",
    )
    result = provider.prefetch("what do we know?")

    assert "current-user" in result
    assert "mixed-user" not in result
    assert "[keep]" in result
    assert "[drop]" not in result


def test_napcat_dm_prefetch_reads_user_and_authorized_groups(monkeypatch, endpoint_env):
    gateway_dir = endpoint_env / "gateway"
    gateway_dir.mkdir()
    (gateway_dir / "napcat_cross_session_auth.json").write_text(
        json.dumps({"session-1": {"groups": {"100000001": {}, "123456": {}}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append(headers["LIGHTRAG-WORKSPACE"])
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [
                        {"entity_name": "user-scope", "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md"},
                        {"entity_name": "group-1", "file_path": "hermes_explicit_memory/napcat_group_100000001/chat/b.md"},
                        {"entity_name": "group-2", "file_path": "hermes_explicit_memory/napcat_group_123456/chat/c.md"},
                    ],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="10001",
        chat_type="dm",
    )
    provider.prefetch("what do we know?")
    assert calls == ["hermes_default"]


def test_identity_prefetch_drops_wrong_subject_source_tag(monkeypatch, endpoint_env):
    def fake_request(method, url, json=None, headers=None, **kwargs):
        return FakeResponse(
            {
                "status": "success",
                "data": {
                    "entities": [
                        {
                            "entity_name": "SIGTERM",
                            "file_path": (
                                "hermes_facts/v1/platform=napcat/fact_type=user_profile/"
                                "subject=napcat_user_46895556/source=napcat_user_46895556/"
                                "source_kind=dm/target=user/digest=a.md"
                            ),
                        },
                        {
                            "entity_name": "头顶着火人",
                            "file_path": (
                                "hermes_facts/v1/platform=napcat/fact_type=user_profile/"
                                "subject=napcat_user_2478613423/source=napcat_user_2478613423/"
                                "source_kind=dm/target=user/digest=b.md"
                            ),
                        },
                    ],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="46895556",
        chat_id="46895556",
        chat_type="dm",
    )
    result = provider.prefetch("我是谁")
    assert "SIGTERM" in result
    assert "头顶着火人" not in result


def test_identity_prefetch_uses_exact_user_memory_before_lightrag(monkeypatch, endpoint_env):
    user_dir = endpoint_env / "hermes_explicit_memory" / "napcat_user_46895556" / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "name.md").write_text("# Hermes user memory\n46895556 的名字是 SIGTERM。", encoding="utf-8")

    def fake_request(*args, **kwargs):
        raise AssertionError("identity fallback should not need LightRAG")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="46895556",
        chat_id="46895556",
        chat_type="dm",
    )
    result = provider.prefetch("我是谁")
    assert "46895556 的名字是 SIGTERM" in result


def test_tools_mode_disables_prefetch(monkeypatch, endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.save_config({"memory_mode": "tools"}, str(endpoint_env))
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")

    called = {"count": 0}

    def fake_request(*args, **kwargs):
        called["count"] += 1
        return FakeResponse({})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    assert provider.prefetch("test query") == ""
    assert called["count"] == 0
    assert [item["name"] for item in provider.get_tool_schemas()] == [
        "lightrag_search",
        "lightrag_answer",
    ]


def test_context_mode_hides_tools(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.save_config({"memory_mode": "context"}, str(endpoint_env))
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    assert provider.get_tool_schemas() == []


def test_orchestrator_context_hides_tools_even_in_hybrid_mode(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        agent_identity="coder",
        agent_context="orchestrator",
    )
    assert provider.get_tool_schemas() == []


def test_sync_turn_batches_and_flushes(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return FakeResponse({"status": "success", "message": "queued", "track_id": "t1"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.save_config({"write_batch_turns": 2}, str(endpoint_env))
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    provider.sync_turn("user one", "assistant one")
    provider.sync_turn("user two", "assistant two")
    assert provider._flush_thread is not None
    provider._flush_thread.join(timeout=1)

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/documents/text")
    assert "Conversation digest" in calls[0]["json"]["text"]
    assert "user one" in calls[0]["json"]["text"]
    assert "batch:1" in calls[0]["json"]["file_source"]


def test_napcat_group_sync_turn_writes_only_group_workspace(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"workspace": headers["LIGHTRAG-WORKSPACE"], "file_source": json["file_source"]})
        return FakeResponse({"status": "success", "message": "queued", "track_id": "t1"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.save_config({"write_batch_turns": 1}, str(endpoint_env))
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )
    provider.sync_turn("user one", "assistant one")
    provider._flush_thread.join(timeout=1)
    assert calls[0]["workspace"] == "hermes_default"
    assert calls[0]["file_source"].startswith("hermes:lightrag:napcat_group_100000001:session:")


def test_on_session_end_flushes_remaining(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return FakeResponse({"status": "success", "message": "queued", "track_id": "t2"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.save_config({"write_batch_turns": 5}, str(endpoint_env))
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    provider.sync_turn("pending user", "pending assistant")
    provider.on_session_end([])

    assert len(calls) == 1
    assert "pending user" in calls[0]["json"]["text"]


def test_prefetch_cache_invalidation_stays_clean_until_lightrag_flush(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if url.endswith("/query/data"):
            return FakeResponse({"status": "success", "message": "ok", "data": {}})
        if url.endswith("/documents/text"):
            return FakeResponse({"status": "success", "message": "queued", "track_id": "t2"})
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.save_config({"write_batch_turns": 2}, str(endpoint_env))
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")

    provider.prefetch("记住我的偏好")
    assert provider.should_invalidate_cached_prefetch() is False

    provider.sync_turn("first user", "first assistant")
    assert provider.should_invalidate_cached_prefetch() is False

    provider.sync_turn("second user", "second assistant")
    assert provider.should_invalidate_cached_prefetch() is True
    assert provider._flush_thread is not None
    provider._flush_thread.join(timeout=1)

    provider.prefetch("再查一次")
    assert provider.should_invalidate_cached_prefetch() is False


def test_on_memory_write_mirrors_remove_as_tombstone(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return FakeResponse({"status": "success", "message": "queued", "track_id": "t3"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    provider.on_memory_write("remove", "memory", "User no longer uses the old endpoint")
    assert provider.should_invalidate_cached_prefetch() is True
    assert provider._memory_write_thread is not None
    provider._memory_write_thread.join(timeout=1)

    assert len(calls) == 1
    assert "fact_type: memory_tombstone" in calls[0]["json"]["text"]
    assert "hermes_facts/v1/" in calls[0]["json"]["file_source"]


def test_on_memory_write_routes_user_and_chat_targets(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"workspace": headers["LIGHTRAG-WORKSPACE"], "file_source": json["file_source"]})
        return FakeResponse({"status": "success", "message": "queued", "track_id": "t4"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )
    provider.on_memory_write("add", "user", "Alice likes tea")
    provider._memory_write_thread.join(timeout=1)
    provider.on_memory_write("add", "chat", "This group discusses Alpha")
    provider._memory_write_thread.join(timeout=1)

    assert calls[0]["workspace"] == "hermes_default"
    assert "fact_type=user_profile" in calls[0]["file_source"]
    assert "source=napcat_group_100000001" in calls[0]["file_source"]
    assert "target=user" in calls[0]["file_source"]
    assert calls[1]["workspace"] == "hermes_default"
    assert "fact_type=chat_profile" in calls[1]["file_source"]
    assert "source=napcat_group_100000001" in calls[1]["file_source"]
    assert "target=chat" in calls[1]["file_source"]


def test_handle_tool_call_search_and_answer(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if url.endswith("/query/data"):
            return FakeResponse(
                {
                    "status": "success",
                    "message": "ok",
                    "data": {
                        "entities": [{"entity_name": "Hermes", "file_path": "hermes_explicit_memory/hermes_coder/memory/a.md"}],
                        "relationships": [],
                        "chunks": [{"content": "Hermes integrates LightRAG", "file_path": "hermes_explicit_memory/hermes_coder/memory/a.md"}],
                        "references": [{"reference_id": "1", "file_path": "hermes_explicit_memory/hermes_coder/memory/a.md"}],
                    },
                    "metadata": {"mode": json.get("mode")},
                }
            )
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")

    search = json.loads(
        provider.handle_tool_call("lightrag_search", {"query": "Hermes LightRAG", "top_k": 3})
    )
    answer = json.loads(
        provider.handle_tool_call("lightrag_answer", {"query": "How does Hermes use LightRAG?"})
    )

    assert search["counts"]["entities"] == 1
    assert search["metadata"]["mode"] == "mix"
    assert answer["reference_count"] == 1
    assert "Hermes integrates LightRAG" in answer["response"]
    assert calls[0]["headers"]["LIGHTRAG-WORKSPACE"] == "hermes_coder"


def test_handle_tool_call_multi_workspace(monkeypatch, endpoint_env):
    gateway_dir = endpoint_env / "gateway"
    gateway_dir.mkdir()
    (gateway_dir / "napcat_cross_session_auth.json").write_text(
        json.dumps({"session-1": {"groups": {"100000001": {}, "123456": {}}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        workspace = headers["LIGHTRAG-WORKSPACE"]
        calls.append((url, workspace))
        if url.endswith("/query/data"):
            return FakeResponse(
                {
                    "status": "success",
                    "message": "ok",
                    "data": {
                        "entities": [
                            {"entity_name": "user-scope", "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md"},
                            {"entity_name": "group-1", "file_path": "hermes_explicit_memory/napcat_group_100000001/chat/b.md"},
                            {"entity_name": "group-2", "file_path": "hermes_explicit_memory/napcat_group_123456/chat/c.md"},
                        ],
                        "relationships": [],
                        "chunks": [],
                        "references": [
                            {"reference_id": "user", "file_path": "hermes_explicit_memory/napcat_user_10001/user/a.md"},
                            {"reference_id": "group1", "file_path": "hermes_explicit_memory/napcat_group_100000001/chat/b.md"},
                            {"reference_id": "group2", "file_path": "hermes_explicit_memory/napcat_group_123456/chat/c.md"},
                        ],
                    },
                    "metadata": {"mode": json.get("mode")},
                }
            )
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="10001",
        chat_type="dm",
    )
    search = json.loads(provider.handle_tool_call("lightrag_search", {"query": "hello world"}))
    answer = json.loads(provider.handle_tool_call("lightrag_answer", {"query": "hello world"}))

    assert search["counts"]["entities"] == 3
    assert search["workspaces"] == ["napcat_user_10001", "napcat_group_100000001", "napcat_group_123456"]
    assert len(answer["responses"]) == 3
    assert answer["reference_count"] == 3
    assert calls == [
        ("http://lightrag.local/query/data", "hermes_default"),
        ("http://lightrag.local/query/data", "hermes_default"),
    ]


def test_handle_tool_call_rejects_short_queries(endpoint_env):
    provider = LightRAGMemoryProvider()
    provider.initialize("session-1", hermes_home=str(endpoint_env), agent_identity="coder")
    result = json.loads(provider.handle_tool_call("lightrag_search", {"query": "hi"}))
    assert "error" in result


def test_handle_memory_write_crud_and_snapshot(monkeypatch, endpoint_env):
    docs_by_workspace = {}
    next_id = {"value": 1}

    def _workspace_docs(workspace):
        return docs_by_workspace.setdefault(workspace, [])

    def fake_request(method, url, json=None, headers=None, **kwargs):
        workspace = headers["LIGHTRAG-WORKSPACE"]
        docs = _workspace_docs(workspace)
        if method == "GET" and url.endswith("/documents"):
            return FakeResponse(
                {
                    "statuses": {
                        "processed": [
                            {
                                "id": doc["id"],
                                "content_summary": doc["text"],
                                "content_length": len(doc["text"]),
                                "status": "processed",
                                "created_at": "2026-04-16T00:00:00+00:00",
                                "updated_at": "2026-04-16T00:00:00+00:00",
                                "track_id": "track",
                                "chunks_count": 1,
                                "error_msg": None,
                                "metadata": {},
                                "file_path": doc["file_source"],
                            }
                            for doc in docs
                        ]
                    }
                }
            )
        if method == "POST" and url.endswith("/documents/text"):
            docs.append(
                {
                    "id": f"doc-{next_id['value']}",
                    "text": json["text"],
                    "file_source": json["file_source"],
                }
            )
            next_id["value"] += 1
            return FakeResponse({"status": "success", "track_id": "track"})
        if method == "DELETE" and url.endswith("/documents/delete_document"):
            doc_ids = set(json["doc_ids"])
            docs[:] = [doc for doc in docs if doc["id"] not in doc_ids]
            return FakeResponse({"status": "success", "deleted": list(doc_ids)})
        raise AssertionError(f"Unexpected request {method} {url}")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )

    added = json.loads(provider.handle_memory_write("add", "user", content="Alice likes tea"))
    replaced = json.loads(
        provider.handle_memory_write(
            "replace",
            "user",
            content="Alice likes green tea",
            old_text="likes tea",
        )
    )
    snapshot = provider.build_live_memory_snapshot()
    removed = json.loads(
        provider.handle_memory_write(
            "remove",
            "user",
            old_text="green tea",
        )
    )

    assert added["success"] is True
    assert replaced["success"] is True
    assert "Alice likes green tea" in snapshot
    assert removed["success"] is True
    assert provider.build_live_memory_snapshot() == ""


def test_handle_memory_write_posts_source_tagged_user_fact(monkeypatch, endpoint_env):
    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append(json)
        if method == "GET":
            return FakeResponse({"statuses": {"processed": []}})
        return FakeResponse({"status": "success", "track_id": "track"})

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="46895556",
        chat_id="46895556",
        chat_type="dm",
    )
    provider.handle_memory_write("add", "user", content="46895556 的名字是 SIGTERM。")

    posted = [call for call in calls if call and "file_source" in call][0]
    assert "hermes_facts/v1/" in posted["file_source"]
    assert "fact_type=user_profile" in posted["file_source"]
    assert "subject=napcat_user_46895556" in posted["file_source"]
    assert "source=napcat_user_46895556" in posted["file_source"]
    assert "信源：与 46895556 的私聊" in posted["text"]


def test_handle_memory_write_rejects_multi_fact_entry(monkeypatch, endpoint_env):
    def fake_request(method, url, json=None, headers=None, **kwargs):
        raise AssertionError("Should not hit LightRAG for invalid multi-fact memory entry")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )

    result = json.loads(
        provider.handle_memory_write(
            "add",
            "user",
            content="Alice likes tea；Alice uses vim",
        )
    )

    assert result["success"] is False
    assert "atomic fact" in result["error"]


def test_build_live_memory_snapshot_groups_targets(monkeypatch, endpoint_env):
    docs_by_workspace = {
        "hermes_coder": [
            {
                "id": "doc-memory",
                "text": "# Hermes memory memory\nRemember the main project codename is Atlas",
                "file_source": "hermes_explicit_memory/hermes_coder/memory/aaa.md",
            },
            {
                "id": "doc-user",
                "text": "# Hermes user memory\nAlice prefers concise replies",
                "file_source": "hermes_explicit_memory/napcat_user_10001/user/bbb.md",
            },
            {
                "id": "doc-chat",
                "text": "# Hermes chat memory\nThis group discusses Project Atlas",
                "file_source": "hermes_explicit_memory/napcat_group_100000001/chat/ccc.md",
            },
        ],
    }

    def fake_request(method, url, json=None, headers=None, **kwargs):
        workspace = headers["LIGHTRAG-WORKSPACE"]
        if method == "GET" and url.endswith("/documents"):
            return FakeResponse(
                {
                    "statuses": {
                        "processed": [
                            {
                                "id": doc["id"],
                                "content_summary": doc["text"],
                                "content_length": len(doc["text"]),
                                "status": "processed",
                                "created_at": "2026-04-16T00:00:00+00:00",
                                "updated_at": "2026-04-16T00:00:00+00:00",
                                "track_id": "track",
                                "chunks_count": 1,
                                "error_msg": None,
                                "metadata": {},
                                "file_path": doc["file_source"],
                            }
                            for doc in docs_by_workspace.get(workspace, [])
                        ]
                    }
                }
            )
        raise AssertionError(f"Unexpected request {method} {url}")

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)

    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        agent_identity="coder",
        platform="napcat",
        user_id="10001",
        chat_id="100000001",
        chat_type="group",
    )
    snapshot = provider.build_live_memory_snapshot()

    assert "Current MEMORY" in snapshot
    assert "Current USER PROFILE" in snapshot
    assert "Current CHAT PROFILE" in snapshot
    assert "Atlas" in snapshot


def test_group_prefetch_keeps_exact_chat_memory_when_query_results_are_empty(monkeypatch, endpoint_env):
    chat_dir = endpoint_env / "hermes_explicit_memory" / "napcat_group_100000001" / "chat"
    chat_dir.mkdir(parents=True)
    (chat_dir / "trigger.md").write_text(
        "# Hermes chat memory\n群里把 1 当成 Hermes 唤起口令。",
        encoding="utf-8",
    )

    calls = []

    def fake_request(method, url, json=None, headers=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return FakeResponse(
            {
                "status": "success",
                "message": "ok",
                "data": {
                    "entities": [],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
                "metadata": {"mode": "mix"},
            }
        )

    monkeypatch.setattr("plugins.memory.lightrag.httpx.request", fake_request)
    provider = LightRAGMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(endpoint_env),
        platform="napcat",
        user_id="46895556",
        chat_id="100000001",
        chat_type="group",
        agent_context="orchestrator",
    )

    result = provider.prefetch("[SIGTERM] 1")

    assert "群里把 1 当成 Hermes 唤起口令。" in result
    assert "## Shared Context [napcat_group_100000001]" in result
    assert len(calls) == 1
    debug = provider.get_last_prefetch_debug_info()
    assert debug["used_exact_chat_memory"] is True
