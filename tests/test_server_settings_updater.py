from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from app.api.routes.settings import router as settings_router
from app.api.routes.updater import router as updater_router
from app.composition import build_components
from app.settings import Settings


def _production_components(tmp_path):
    settings = replace(
        Settings.from_env(),
        environment="production",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        image_provider="google",
        transcription_provider="google",
        normalization_provider="google",
        google_image_model="image-model",
        google_transcription_model="transcription-model",
        google_normalization_model="normalization-model",
        api_keys=("production-client-api-key-01",),
        operator_access_key="production-operator-access-key",
        updater_catalog_token="catalog-token-1234567890123456",
    )
    return settings, build_components(settings)


def test_mock_test_settings_are_isolated_from_production_runtime(tmp_path) -> None:
    settings, components = _production_components(tmp_path)
    application = FastAPI()
    application.state.moonli_settings = components.settings
    application.state.operator_auth_store = components.operator_auth_store
    application.state.server_settings_store = components.server_settings_store
    application.state.audit_store = components.audit_store
    install_error_handlers(application)
    application.include_router(settings_router)
    session = components.operator_auth_store.create_session(settings.operator_access_key)
    client = TestClient(application)
    client.cookies.set(settings.operator_cookie_name, session.token)
    client.headers.update({"X-CSRF-Token": session.csrf_token})
    updated = dict(components.server_settings_store.get())
    updated["image_provider"] = "mock"
    updated["transcription_provider"] = "mock"
    updated["normalization_provider"] = "mock"

    with client:
        response = client.put("/internal/settings", json=updated)

    assert response.status_code == 200
    assert components.server_settings_store.get() == updated
    assert application.state.moonli_settings.image_provider == "google"
    restarted = build_components(settings)
    assert restarted.settings.image_provider == "google"
    assert restarted.server_settings_store.get()["image_provider"] == "mock"


def test_updater_catalog_uses_separate_token_and_verified_checksum(tmp_path) -> None:
    settings, components = _production_components(tmp_path)
    application = FastAPI()
    application.state.moonli_settings = components.settings
    install_error_handlers(application)
    application.include_router(updater_router)

    with TestClient(application) as client:
        denied = client.get("/api/v1/register/snapshot")
        response = client.get(
            "/api/v1/register/snapshot",
            headers={"Authorization": f"Bearer {settings.updater_catalog_token}"},
        )

    assert denied.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    canonical = json.dumps(
        {"values": payload["values"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert payload["schema"] == "exocortex.register.snapshot.v1"
    assert payload["checksum"] == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert payload["values"]["repositories"]["moonli"]["url"].endswith("/backend.git")
