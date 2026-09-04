from __future__ import annotations

import json
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from app.api.routes.routing import router
from app.composition import build_components
from app.settings import Settings
from app.storage.routing_config import PROXY_URL, RoutingConfigStore

VLESS_FIXTURE = (
    "vless://11111111-1111-4111-8111-111111111111@proxy.example.com:2443"
    "?encryption=none&flow=xtls-rprx-vision&security=reality"
    "&sni=www.example.com&fp=firefox"
    "&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "&sid=0123456789abcdef&type=tcp&headerType=none#Example"
)


def _client(tmp_path) -> tuple[TestClient, RoutingConfigStore]:
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        operator_access_key="test-operator-access-key",
    )
    components = build_components(settings)
    application = FastAPI()
    application.state.moonli_settings = settings
    application.state.operator_auth_store = components.operator_auth_store
    application.state.routing_config_store = components.routing_config_store
    application.state.routing_config_store.proxy_available = lambda: True
    application.state.audit_store = components.audit_store
    install_error_handlers(application)
    application.include_router(router)
    client = TestClient(application)
    session = components.operator_auth_store.create_session("test-operator-access-key")
    client.cookies.set(settings.operator_cookie_name, session.token)
    client.headers.update({"X-CSRF-Token": session.csrf_token})
    return client, components.routing_config_store


def test_routing_secret_is_persisted_but_never_returned(tmp_path) -> None:
    client, store = _client(tmp_path)

    with client:
        before = client.get("/internal/routing")
        saved = client.put(
            "/internal/routing",
            json={"enabled": True, "vless_uri": VLESS_FIXTURE},
        )
        after = client.get("/internal/routing")

    assert before.json() == {"enabled": False, "configured": False, "mode": "direct"}
    expected = {"enabled": True, "configured": True, "mode": "vless"}
    assert saved.status_code == after.status_code == 200
    assert saved.json() == after.json() == expected
    assert VLESS_FIXTURE not in saved.text + after.text
    assert VLESS_FIXTURE in store.settings_path.read_text(encoding="utf-8")
    assert store.proxy_url() == PROXY_URL

    xray = json.loads(store.xray_path.read_text(encoding="utf-8"))
    outbound = xray["outbounds"][0]
    assert outbound["protocol"] == "vless"
    assert outbound["settings"]["vnext"][0]["address"] == "proxy.example.com"
    assert outbound["streamSettings"]["security"] == "reality"


def test_disabling_route_keeps_private_connection_and_materializes_direct_config(
    tmp_path,
) -> None:
    store = RoutingConfigStore(tmp_path / "secrets")
    store.update(enabled=True, vless_uri=VLESS_FIXTURE)

    status = store.update(enabled=False)

    assert status == {"enabled": False, "configured": True, "mode": "direct"}
    assert store.proxy_url() is None
    xray = json.loads(store.xray_path.read_text(encoding="utf-8"))
    assert xray["outbounds"][0]["protocol"] == "freedom"
    assert VLESS_FIXTURE in store.settings_path.read_text(encoding="utf-8")


def test_invalid_vless_connection_is_rejected_without_replacing_saved_route(
    tmp_path,
) -> None:
    client, store = _client(tmp_path)
    store.update(enabled=True, vless_uri=VLESS_FIXTURE)

    with client:
        response = client.put(
            "/internal/routing",
            json={
                "enabled": True,
                "vless_uri": (
                    "vless://11111111-1111-4111-8111-111111111111@proxy.example.com:2443"
                    "?security=none"
                ),
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ROUTING_CONFIGURATION"
    assert store.proxy_url() == PROXY_URL
    assert VLESS_FIXTURE in store.settings_path.read_text(encoding="utf-8")
    assert "security=none" not in store.settings_path.read_text(encoding="utf-8")


def test_routing_requires_operator_authentication(tmp_path) -> None:
    client, _ = _client(tmp_path)
    client.cookies.clear()

    with client:
        response = client.get("/internal/routing")

    assert response.status_code == 401


def test_proxy_cannot_be_enabled_before_xray_sidecar_is_available(tmp_path) -> None:
    client, store = _client(tmp_path)
    client.app.state.routing_config_store.proxy_available = lambda: False

    with client:
        response = client.put(
            "/internal/routing",
            json={"enabled": True, "vless_uri": VLESS_FIXTURE},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ROUTING_PROXY_UNAVAILABLE"
    assert store.status() == {"enabled": False, "configured": False, "mode": "direct"}
