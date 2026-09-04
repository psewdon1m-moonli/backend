from __future__ import annotations

import json
import sqlite3
import uuid
import zipfile
from dataclasses import replace
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.errors import install_error_handlers
from app.api.routes.generate import router
from app.composition import build_components
from app.providers.errors import NoVisualSubjectError
from app.settings import Settings
from app.storage.device_registry import DeviceRegistry


def _client(tmp_path) -> TestClient:
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "v1",
        secrets_dir=tmp_path / "secrets",
    )
    components = build_components(settings)
    app = FastAPI()
    app.state.moonli_settings = settings
    app.state.client_authenticator = components.authenticator
    app.state.pipeline_profiles = components.profiles
    app.state.rate_limiter = components.rate_limiter
    app.state.generation_service = components.generation_service
    app.state.production_secret_store = components.production_secret_store
    app.state.production_usage_store = components.production_usage_store
    app.state.device_registry = components.device_registry
    app.state.metrics = components.metrics
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


def _headers(
    key: str,
    idempotency: str | None = None,
    device_id: str = "td-02941846",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "X-Moonli-Device-Id": device_id,
        "Idempotency-Key": idempotency or f"test-{uuid.uuid4().hex}",
    }


class _NoVisualSubjectNormalizer:
    name = "google"

    async def normalize(self, text: str) -> str:
        raise NoVisualSubjectError("The request contains no visual subject")


def test_pipeline_1_text_is_one_post_returning_final_png(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            json={
                "type": "text",
                "pipeline": "pipeline-1",
                "text": "Ночной город в форме сада",
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-idempotent-replay"] == "false"
    image = Image.open(BytesIO(response.content))
    assert image.format == "PNG"
    assert image.size == (1024, 1024)
    with sqlite3.connect(tmp_path / "v1" / "runs.sqlite3") as connection:
        row = connection.execute(
            "SELECT status, original_text, normalized_text, visual_brief_json, prompt, result_asset_key "
            "FROM generation_runs"
        ).fetchone()
    assert row is not None
    assert row[0] == "COMPLETE"
    assert row[1] == row[2] == "Ночной город в форме сада"
    assert json.loads(row[3])["subject"]
    assert "Exact palette constraint" in row[4]
    assert row[5].startswith("completed/")
    with sqlite3.connect(tmp_path / "v1" / "production-usage.sqlite3") as connection:
        request_count = connection.execute(
            "SELECT COUNT(*) FROM production_api_requests"
        ).fetchone()[0]
    assert request_count == 1


def test_pipeline_1_returns_actionable_error_for_non_visual_text(tmp_path) -> None:
    with _client(tmp_path) as client:
        client.app.state.generation_service._prompt_normalizer = (
            _NoVisualSubjectNormalizer()
        )
        response = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            json={
                "type": "text",
                "pipeline": "pipeline-1",
                "text": "Там впереди направо.",
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "NO_VISUAL_SUBJECT",
            "message": "Say what you want to draw.",
        }
    }


def test_pipeline_2_text_with_same_key_returns_recomposable_layer_zip(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            json={
                "type": "text",
                "pipeline": "pipeline-2",
                "text": "Большое красное дерево на острове, без людей",
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.moonli.layers+zip"
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["output_mode"] == "layered_image"
        assert len(manifest["palette"]) == 12
        assert len(manifest["layers"]) == 12
        composite = Image.open(BytesIO(archive.read("composite.png"))).convert("RGBA")
        rebuilt = Image.new("RGBA", composite.size, (0, 0, 0, 0))
        for layer in manifest["layers"]:
            rebuilt = Image.alpha_composite(
                rebuilt, Image.open(BytesIO(archive.read(layer["image"]))).convert("RGBA")
            )
        assert rebuilt.tobytes() == composite.tobytes()


def test_audio_uses_same_one_request_contract(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            data={"type": "audio", "pipeline": "pipeline-1"},
            files={"audio": ("request.wav", b"RIFF-mock-audio", "audio/wav")},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert len(list((tmp_path / "v1" / "artifacts" / "inputs").glob("*/input_audio.wav"))) == 1


def test_pipeline_2_audio_returns_final_layer_zip_in_the_same_response(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            data={"type": "audio", "pipeline": "pipeline-2"},
            files={"audio": ("request.ogg", b"OggS-mock-audio", "audio/ogg")},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.moonli.layers+zip"
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["layers"]) == 12


def test_idempotency_replays_result_and_rejects_payload_reuse(tmp_path) -> None:
    key = f"test-{uuid.uuid4().hex}"
    headers = _headers("dev-moonli-client-key", key)
    with _client(tmp_path) as client:
        first = client.post(
            "/v1/generate",
            headers=headers,
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
        replay = client.post(
            "/v1/generate",
            headers=headers,
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
        conflict = client.post(
            "/v1/generate",
            headers=headers,
            json={"type": "text", "pipeline": "pipeline-1", "text": "Sun"},
        )
    assert first.status_code == replay.status_code == 200
    assert replay.headers["x-idempotent-replay"] == "true"
    assert replay.headers["x-moonli-run-id"] == first.headers["x-moonli-run-id"]
    assert replay.content == first.content
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_only_canonical_pipeline_tag_is_accepted_and_internal_storage_is_private(tmp_path) -> None:
    with _client(tmp_path) as client:
        invalid = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            json={
                "type": "text",
                "pipeline": "pipeline-1",
                "text": "Moon",
                "table_id": "table_2",
            },
        )
        typo = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key"),
            json={"type": "text", "pipeline": "pipelien-2", "text": "Moon"},
        )
        data_route = client.get("/data/runs.sqlite3")
        proxy_route = client.get("/proxy/image", params={"url": "https://example.com"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_INPUT"
    assert typo.status_code == 422
    assert typo.json()["error"]["code"] == "INVALID_INPUT"
    assert data_route.status_code == 404
    assert proxy_route.status_code == 404


def test_auth_and_idempotency_are_required(tmp_path) -> None:
    with _client(tmp_path) as client:
        no_auth = client.post(
            "/v1/generate",
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
        no_idempotency = client.post(
            "/v1/generate",
            headers={"Authorization": "Bearer dev-moonli-client-key"},
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
    assert no_auth.status_code == 401
    assert no_idempotency.status_code == 422


def test_device_identity_is_required_and_validated(tmp_path) -> None:
    with _client(tmp_path) as client:
        missing = client.post(
            "/v1/generate",
            headers={
                "Authorization": "Bearer dev-moonli-client-key",
                "Idempotency-Key": f"test-{uuid.uuid4().hex}",
            },
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
        invalid = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key", device_id="android-123"),
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )

    assert missing.status_code == invalid.status_code == 422
    assert missing.json()["error"]["code"] == "INVALID_DEVICE_ID"
    assert invalid.json()["error"]["code"] == "INVALID_DEVICE_ID"


def test_device_can_use_both_pipelines_and_blocking_is_enforced(tmp_path) -> None:
    device_id = "aa-26093758"
    with _client(tmp_path) as client:
        first = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key", device_id=device_id),
            json={"type": "text", "pipeline": "pipeline-1", "text": "Moon"},
        )
        registry = DeviceRegistry(tmp_path / "v1" / "devices.sqlite3")
        registered, total = registry.list()
        registry.set_blocked(device_id, True)
        denied = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key", device_id=device_id),
            json={"type": "text", "pipeline": "pipeline-2", "text": "Sun"},
        )
        registry.set_blocked(device_id, False)
        second = client.post(
            "/v1/generate",
            headers=_headers("dev-moonli-client-key", device_id=device_id),
            json={"type": "text", "pipeline": "pipeline-2", "text": "Sun"},
        )

    assert first.status_code == second.status_code == 200
    assert first.headers["x-moonli-device-id"] == device_id
    assert second.headers["content-type"] == "application/vnd.moonli.layers+zip"
    assert total == 1
    assert registered[0].connection_type == "android_app"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "DEVICE_BLOCKED"
    current, _ = registry.list()
    assert current[0].request_count == 3
    assert current[0].blocked is False
