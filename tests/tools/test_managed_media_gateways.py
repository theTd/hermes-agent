import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


def _load_tool_module(module_name: str, filename: str):
    spec = spec_from_file_location(module_name, TOOLS_DIR / filename)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_tool_and_agent_modules():
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools"
        or name.startswith("tools.")
        or name == "agent"
        or name.startswith("agent.")
        or name in {"fal_client", "openai"}
    }
    try:
        yield
    finally:
        for name in list(sys.modules):
            if (
                name == "tools"
                or name.startswith("tools.")
                or name == "agent"
                or name.startswith("agent.")
                or name in {"fal_client", "openai"}
            ):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)


@pytest.fixture(autouse=True)
def _enable_managed_nous_tools(monkeypatch):
    """Patch the source modules so managed_nous_tools_enabled() returns True
    even after tool modules are dynamically reloaded."""
    monkeypatch.setattr("hermes_cli.auth.get_nous_auth_status", lambda: {"logged_in": True})
    monkeypatch.setattr("hermes_cli.models.check_nous_free_tier", lambda: False)


def _install_fake_tools_package():
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
    sys.modules["tools"] = tools_package
    sys.modules["tools.debug_helpers"] = types.SimpleNamespace(
        DebugSession=lambda *args, **kwargs: types.SimpleNamespace(
            active=False,
            session_id="debug-session",
            log_call=lambda *a, **k: None,
            save=lambda: None,
            get_session_info=lambda: {},
        )
    )
    sys.modules["tools.managed_tool_gateway"] = _load_tool_module(
        "tools.managed_tool_gateway",
        "managed_tool_gateway.py",
    )


def _install_fake_fal_client(captured):
    def submit(model, arguments=None, headers=None):
        raise AssertionError("managed FAL gateway mode should use fal_client.SyncClient")

    class FakeResponse:
        def json(self):
            return {
                "request_id": "req-123",
                "response_url": "http://127.0.0.1:3009/requests/req-123",
                "status_url": "http://127.0.0.1:3009/requests/req-123/status",
                "cancel_url": "http://127.0.0.1:3009/requests/req-123/cancel",
            }

    def _maybe_retry_request(client, method, url, json=None, timeout=None, headers=None):
        captured["submit_via"] = "managed_client"
        captured["http_client"] = client
        captured["method"] = method
        captured["submit_url"] = url
        captured["arguments"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return FakeResponse()

    class SyncRequestHandle:
        def __init__(self, request_id, response_url, status_url, cancel_url, client):
            captured["request_id"] = request_id
            captured["response_url"] = response_url
            captured["status_url"] = status_url
            captured["cancel_url"] = cancel_url
            captured["handle_client"] = client

    class SyncClient:
        def __init__(self, key=None, default_timeout=120.0):
            captured["sync_client_inits"] = captured.get("sync_client_inits", 0) + 1
            captured["client_key"] = key
            captured["client_timeout"] = default_timeout
            self.default_timeout = default_timeout
            self._client = object()

    fal_client_module = types.SimpleNamespace(
        submit=submit,
        SyncClient=SyncClient,
        client=types.SimpleNamespace(
            _maybe_retry_request=_maybe_retry_request,
            _raise_for_status=lambda response: None,
            SyncRequestHandle=SyncRequestHandle,
        ),
    )
    sys.modules["fal_client"] = fal_client_module
    return fal_client_module


def _install_fake_openai_module(captured, transcription_response=None, image_response=None):
    class FakeSpeechResponse:
        def stream_to_file(self, output_path):
            captured["stream_to_file"] = output_path

    class FakeOpenAI:
        def __init__(self, api_key, base_url, **kwargs):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["client_kwargs"] = kwargs
            captured["close_calls"] = captured.get("close_calls", 0)

            def create_speech(**kwargs):
                captured["speech_kwargs"] = kwargs
                return FakeSpeechResponse()

            def create_transcription(**kwargs):
                captured["transcription_kwargs"] = kwargs
                return transcription_response

            def create_image(**kwargs):
                captured["image_kwargs"] = kwargs
                return image_response

            self.audio = types.SimpleNamespace(
                speech=types.SimpleNamespace(
                    create=create_speech
                ),
                transcriptions=types.SimpleNamespace(
                    create=create_transcription
                ),
            )
            self.images = types.SimpleNamespace(
                generate=create_image
            )

        def close(self):
            captured["close_calls"] += 1

    fake_module = types.SimpleNamespace(
        OpenAI=FakeOpenAI,
        APIError=Exception,
        APIConnectionError=Exception,
        APITimeoutError=Exception,
    )
    sys.modules["openai"] = fake_module


def test_managed_fal_submit_uses_gateway_origin_and_nous_token(monkeypatch):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client(captured)
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("FAL_QUEUE_GATEWAY_URL", "http://127.0.0.1:3009")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )
    monkeypatch.setattr(image_generation_tool.uuid, "uuid4", lambda: "fal-submit-123")
    
    image_generation_tool._submit_fal_request(
        "fal-ai/flux-2-pro",
        {"prompt": "test prompt", "num_images": 1},
    )

    assert captured["submit_via"] == "managed_client"
    assert captured["client_key"] == "nous-token"
    assert captured["submit_url"] == "http://127.0.0.1:3009/fal-ai/flux-2-pro"
    assert captured["method"] == "POST"
    assert captured["arguments"] == {"prompt": "test prompt", "num_images": 1}
    assert captured["headers"] == {"x-idempotency-key": "fal-submit-123"}
    assert captured["sync_client_inits"] == 1


def test_managed_fal_submit_reuses_cached_sync_client(monkeypatch):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client(captured)
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("FAL_QUEUE_GATEWAY_URL", "http://127.0.0.1:3009")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )

    image_generation_tool._submit_fal_request("fal-ai/flux-2-pro", {"prompt": "first"})
    first_client = captured["http_client"]
    image_generation_tool._submit_fal_request("fal-ai/flux-2-pro", {"prompt": "second"})

    assert captured["sync_client_inits"] == 1
    assert captured["http_client"] is first_client


def test_image_generate_uses_custom_openai_compatible_backend(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client({})
    _install_fake_openai_module(
        captured,
        image_response=types.SimpleNamespace(
            data=[types.SimpleNamespace(url="https://example.com/generated.png")]
        ),
    )
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "image_generation:",
                "  provider: custom",
                "  model: doubao-seedream-5-0-260128",
                "  base_url: https://ark.cn-beijing.volces.com/api/v3",
                "  api_key: test-ark-key",
                "  timeout: 45",
            ]
        ),
        encoding="utf-8",
    )

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )
    monkeypatch.setattr(image_generation_tool.uuid, "uuid4", lambda: "image-call-123")
    import tools.image_providers.openai_compatible as _openai_compat
    monkeypatch.setattr(
        _openai_compat,
        "_cache_generated_remote_image",
        lambda image_url, fallback_ext=".png": str(tmp_path / "cache" / "generated.png"),
    )

    result = image_generation_tool.image_generate_tool(
        prompt="a red apple on a marble table",
        aspect_ratio="portrait",
        num_images=1,
        output_format="png",
    )

    assert image_generation_tool.check_image_generation_requirements() is True
    assert '"success": true' in result.lower()
    assert str(tmp_path / "cache" / "generated.png") in result
    assert "https://example.com/generated.png" in result
    assert captured["api_key"] == "test-ark-key"
    assert captured["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert captured["client_kwargs"]["timeout"] == 45
    assert captured["client_kwargs"]["max_retries"] == 0
    assert captured["image_kwargs"]["model"] == "doubao-seedream-5-0-260128"
    assert captured["image_kwargs"]["size"] == "1792x2304"
    assert captured["image_kwargs"]["response_format"] == "url"
    assert captured["image_kwargs"]["output_format"] == "png"
    assert captured["image_kwargs"]["extra_headers"] == {"x-idempotency-key": "image-call-123"}
    assert captured["close_calls"] == 1


def test_image_generate_converts_local_reference_image_to_data_url(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client({})
    _install_fake_openai_module(
        captured,
        image_response=types.SimpleNamespace(
            data=[types.SimpleNamespace(url="https://example.com/edited.png")]
        ),
    )
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "image_generation:",
                "  provider: custom",
                "  model: doubao-seedream-5-0-260128",
                "  base_url: https://ark.cn-beijing.volces.com/api/v3",
                "  api_key: test-ark-key",
            ]
        ),
        encoding="utf-8",
    )
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    )

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )
    import tools.image_providers.openai_compatible as _openai_compat
    monkeypatch.setattr(
        _openai_compat,
        "_cache_generated_remote_image",
        lambda image_url, fallback_ext=".png": str(tmp_path / "cache" / "edited.png"),
    )

    result = image_generation_tool.image_generate_tool(
        prompt="turn this into an oil painting",
        image=str(reference_image),
    )

    assert '"success": true' in result.lower()
    assert str(tmp_path / "cache" / "edited.png") in result
    assert captured["image_kwargs"]["extra_body"]["image"].startswith("data:image/png;base64,")


def test_image_generate_omits_output_format_for_seedream_4_5(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_fal_client({})
    _install_fake_openai_module(
        captured,
        image_response=types.SimpleNamespace(
            data=[types.SimpleNamespace(url="https://example.com/generated.jpg")]
        ),
    )
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "image_generation:",
                "  provider: custom",
                "  model: doubao-seedream-4-5-251128",
                "  base_url: https://ark.cn-beijing.volces.com/api/v3",
                "  api_key: test-ark-key",
            ]
        ),
        encoding="utf-8",
    )

    image_generation_tool = _load_tool_module(
        "tools.image_generation_tool",
        "image_generation_tool.py",
    )
    import tools.image_providers.openai_compatible as _openai_compat
    monkeypatch.setattr(
        _openai_compat,
        "_cache_generated_remote_image",
        lambda image_url, fallback_ext=".png": str(tmp_path / "cache" / "generated.jpg"),
    )

    result = image_generation_tool.image_generate_tool(
        prompt="a red apple on a marble table",
        aspect_ratio="portrait",
        num_images=1,
        output_format="png",
    )

    assert '"success": true' in result.lower()
    assert str(tmp_path / "cache" / "generated.jpg") in result
    assert captured["image_kwargs"]["model"] == "doubao-seedream-4-5-251128"
    assert "output_format" not in captured["image_kwargs"]


def test_openai_tts_uses_managed_audio_gateway_when_direct_key_absent(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_openai_module(captured)
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "nousresearch.com")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    tts_tool = _load_tool_module("tools.tts_tool", "tts_tool.py")
    monkeypatch.setattr(tts_tool.uuid, "uuid4", lambda: "tts-call-123")
    output_path = tmp_path / "speech.mp3"
    tts_tool._generate_openai_tts("hello world", str(output_path), {"openai": {}})

    assert captured["api_key"] == "nous-token"
    assert captured["base_url"] == "https://openai-audio-gateway.nousresearch.com/v1"
    assert captured["speech_kwargs"]["model"] == "gpt-4o-mini-tts"
    assert captured["speech_kwargs"]["extra_headers"] == {"x-idempotency-key": "tts-call-123"}
    assert captured["stream_to_file"] == str(output_path)
    assert captured["close_calls"] == 1


def test_openai_tts_accepts_openai_api_key_as_direct_fallback(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_openai_module(captured)
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-direct-key")
    monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "nousresearch.com")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    tts_tool = _load_tool_module("tools.tts_tool", "tts_tool.py")
    output_path = tmp_path / "speech.mp3"
    tts_tool._generate_openai_tts("hello world", str(output_path), {"openai": {}})

    assert captured["api_key"] == "openai-direct-key"
    assert captured["base_url"] == "https://api.openai.com/v1"
    assert captured["close_calls"] == 1


def test_transcription_uses_model_specific_response_formats(monkeypatch, tmp_path):
    whisper_capture = {}
    _install_fake_tools_package()
    _install_fake_openai_module(whisper_capture, transcription_response="hello from whisper")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("stt:\n  provider: openai\n")
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "nousresearch.com")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")

    transcription_tools = _load_tool_module(
        "tools.transcription_tools",
        "transcription_tools.py",
    )
    transcription_tools._load_stt_config = lambda: {"provider": "openai"}
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF0000WAVEfmt ")

    whisper_result = transcription_tools.transcribe_audio(str(audio_path), model="whisper-1")
    assert whisper_result["success"] is True
    assert whisper_capture["base_url"] == "https://openai-audio-gateway.nousresearch.com/v1"
    assert whisper_capture["transcription_kwargs"]["response_format"] == "text"
    assert whisper_capture["close_calls"] == 1

    json_capture = {}
    _install_fake_openai_module(
        json_capture,
        transcription_response=types.SimpleNamespace(text="hello from gpt-4o"),
    )
    transcription_tools = _load_tool_module(
        "tools.transcription_tools",
        "transcription_tools.py",
    )

    json_result = transcription_tools.transcribe_audio(
        str(audio_path),
        model="gpt-4o-mini-transcribe",
    )
    assert json_result["success"] is True
    assert json_result["transcript"] == "hello from gpt-4o"
    assert json_capture["transcription_kwargs"]["response_format"] == "json"
    assert json_capture["close_calls"] == 1
