from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from app.api.routes.operator import router
from app.composition import build_components
from app.settings import Settings


def _client(tmp_path) -> TestClient:
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        operator_access_key="operator-access-key-1234",
        api_keys=("client-api-key-5678",),
    )
    components = build_components(settings)
    app = FastAPI()
    app.state.moonli_settings = settings
    app.state.operator_auth_store = components.operator_auth_store
    app.state.login_rate_limiter = components.login_rate_limiter
    app.state.audit_store = components.audit_store
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


def test_login_uses_httponly_session_and_csrf(tmp_path) -> None:
    with _client(tmp_path) as client:
        denied = client.post("/internal/auth/session", json={"access_key": "wrong-access-key-000"})
        login = client.post(
            "/internal/auth/session", json={"access_key": "operator-access-key-1234"}
        )
        status = client.get("/internal/auth/session")
        csrf_missing = client.delete("/internal/auth/session")
        logout = client.delete(
            "/internal/auth/session", headers={"X-CSRF-Token": login.json()["csrf_token"]}
        )
        after = client.get("/internal/auth/session")

    assert denied.status_code == 401
    assert login.status_code == 200
    assert "httponly" in login.headers["set-cookie"].lower()
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    assert "max-age" not in login.headers["set-cookie"].lower()
    assert "operator-access-key" not in login.text
    assert status.status_code == 200
    assert csrf_missing.status_code == 403
    assert logout.status_code == 200
    assert after.status_code == 401


def test_client_key_cannot_create_operator_session(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/internal/auth/session", json={"access_key": "client-api-key-5678"}
        )
    assert response.status_code == 401


def test_access_key_rotation_revokes_all_sessions(tmp_path) -> None:
    with _client(tmp_path) as client:
        login = client.post(
            "/internal/auth/session", json={"access_key": "operator-access-key-1234"}
        )
        rotated = client.post(
            "/internal/auth/rotate-access-key",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "current_access_key": "operator-access-key-1234",
                "new_access_key": "replacement-access-key-5678",
            },
        )
        old_session = client.get("/internal/auth/session")
        old_key = client.post(
            "/internal/auth/session", json={"access_key": "operator-access-key-1234"}
        )
        new_key = client.post(
            "/internal/auth/session", json={"access_key": "replacement-access-key-5678"}
        )

    assert rotated.status_code == 200
    assert old_session.status_code == 401
    assert old_key.status_code == 401
    assert new_key.status_code == 200
