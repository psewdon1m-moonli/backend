from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from app.api.routes.devices import router as devices_router
from app.composition import build_components
from app.settings import Settings


def _application(tmp_path):
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        operator_access_key="operator-access-key-1234",
    )
    components = build_components(settings)
    application = FastAPI()
    application.state.moonli_settings = components.settings
    application.state.operator_auth_store = components.operator_auth_store
    application.state.device_registry = components.device_registry
    application.state.audit_store = components.audit_store
    install_error_handlers(application)
    application.include_router(devices_router)
    return settings, components, application


def test_operator_lists_blocks_and_unblocks_devices(tmp_path) -> None:
    settings, components, application = _application(tmp_path)
    td, td_created = components.device_registry.record_request("td-02941846")
    aa, aa_created = components.device_registry.record_request("aa-26093758")
    assert td_created and aa_created
    assert td.type_label == "touch designer client"
    assert aa.type_label == "android app client"

    session = components.operator_auth_store.create_session(settings.operator_access_key)
    with TestClient(application) as client:
        denied = client.get("/internal/devices")
        client.cookies.set(settings.operator_cookie_name, session.token)
        listed = client.get("/internal/devices")
        missing_csrf = client.patch(
            "/internal/devices/td-02941846", json={"blocked": True}
        )
        blocked = client.patch(
            "/internal/devices/td-02941846",
            headers={"X-CSRF-Token": session.csrf_token},
            json={"blocked": True},
        )
        unblocked = client.patch(
            "/internal/devices/td-02941846",
            headers={"X-CSRF-Token": session.csrf_token},
            json={"blocked": False},
        )

    assert denied.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert {device["device_id"] for device in listed.json()["devices"]} == {
        "td-02941846",
        "aa-26093758",
    }
    assert missing_csrf.status_code == 403
    assert blocked.status_code == 200
    assert blocked.json()["device"]["blocked"] is True
    assert unblocked.status_code == 200
    assert unblocked.json()["device"]["blocked"] is False


def test_unknown_device_cannot_be_preemptively_blocked(tmp_path) -> None:
    settings, components, application = _application(tmp_path)
    session = components.operator_auth_store.create_session(settings.operator_access_key)
    with TestClient(application) as client:
        client.cookies.set(settings.operator_cookie_name, session.token)
        response = client.patch(
            "/internal/devices/aa-00000001",
            headers={"X-CSRF-Token": session.csrf_token},
            json={"blocked": True},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEVICE_NOT_FOUND"
