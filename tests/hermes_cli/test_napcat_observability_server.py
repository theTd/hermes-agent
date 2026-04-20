from pathlib import Path

import pytest

from gateway.napcat_observability.store import ObservabilityStore


def _build_test_app(tmp_path: Path, monkeypatch, *, access_token: str):
    try:
        import fastapi  # noqa: F401
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_cli.napcat_observability_server as obs_server

    monkeypatch.setattr(obs_server, "_ensure_napcat_web_dist", lambda: False)
    store = ObservabilityStore(tmp_path / "napcat_observability.db")
    app = obs_server.create_app(
        store=store,
        obs_cfg={
            "allow_control_actions": False,
            "access_token": access_token,
        },
        close_store_on_shutdown=False,
    )
    return TestClient(app), store


def test_access_token_guard_requires_login(tmp_path, monkeypatch):
    client, store = _build_test_app(tmp_path, monkeypatch, access_token="my-secret-token")
    try:
        # Protected API should be blocked before login.
        assert client.get("/api/napcat/runtime").status_code == 401

        # Public observability endpoints remain reachable.
        assert client.get("/api/napcat/health").status_code == 200
        status_before = client.get("/api/napcat/auth/status")
        assert status_before.status_code == 200
        assert status_before.json() == {"enabled": True, "authenticated": False}

        # Wrong token should fail.
        assert client.post("/api/napcat/auth/login", json={"token": "wrong"}).status_code == 401

        # Correct token should establish an authenticated cookie session.
        login = client.post("/api/napcat/auth/login", json={"token": "my-secret-token"})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        assert client.get("/api/napcat/runtime").status_code == 200

        # Logout should invalidate the session again.
        logout = client.post("/api/napcat/auth/logout")
        assert logout.status_code == 200
        assert logout.json()["authenticated"] is False
        assert client.get("/api/napcat/runtime").status_code == 401
    finally:
        client.close()
        store.close()


def test_access_token_guard_for_websocket(tmp_path, monkeypatch):
    try:
        from starlette.websockets import WebSocketDisconnect
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    client, store = _build_test_app(tmp_path, monkeypatch, access_token="socket-token")
    try:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/napcat/stream"):
                pass
        assert exc.value.code == 4401

        login = client.post("/api/napcat/auth/login", json={"token": "socket-token"})
        assert login.status_code == 200

        with client.websocket_connect("/ws/napcat/stream") as websocket:
            message = websocket.receive_json()
            assert message["type"] == "snapshot.init"
    finally:
        client.close()
        store.close()


def test_access_token_disabled_keeps_observability_open(tmp_path, monkeypatch):
    client, store = _build_test_app(tmp_path, monkeypatch, access_token="")
    try:
        assert client.get("/api/napcat/runtime").status_code == 200
        status = client.get("/api/napcat/auth/status")
        assert status.status_code == 200
        assert status.json() == {"enabled": False, "authenticated": True}
    finally:
        client.close()
        store.close()
