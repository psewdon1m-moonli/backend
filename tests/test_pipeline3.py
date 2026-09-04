from __future__ import annotations

import hashlib
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
from app.providers.image_generation.variants import build_image_variant_request
from app.settings import Settings
from app.storage.production_pipeline_config import PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION


def _client(tmp_path) -> TestClient:
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
    )
    components = build_components(settings)
    app = FastAPI()
    app.state.moonli_settings = components.settings
    app.state.client_authenticator = components.authenticator
    app.state.pipeline_profiles = components.profiles
    app.state.rate_limiter = components.rate_limiter
    app.state.generation_service = components.generation_service
    app.state.generation_services = components.generation_services
    app.state.pipeline3_service = components.pipeline3_service
    app.state.production_secret_store = components.production_secret_store
    app.state.production_pipeline_config_store = components.production_pipeline_config_store
    app.state.production_usage_store = components.production_usage_store
    app.state.device_registry = components.device_registry
    app.state.metrics = components.metrics
    app.state.audit_store = components.audit_store
    install_error_handlers(app)
    app.include_router(router)
    return TestClient(app)


def _headers(device_id: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer dev-moonli-client-key",
        "X-Moonli-Device-Id": device_id,
        "Idempotency-Key": idempotency_key,
    }


def test_pipeline_3_two_request_contract_returns_plain_text_then_three_jpegs(
    tmp_path,
) -> None:
    device_id = "td-02941846"
    normalize_key = str(uuid.uuid4())
    generate_key = str(uuid.uuid4())
    with _client(tmp_path) as client:
        normalized = client.post(
            "/v1/normalize",
            headers=_headers(device_id, normalize_key),
            data={"type": "audio", "pipeline": "pipeline-3"},
            files={"audio": ("voice.wav", b"RIFF" + b"\0" * 1500, "audio/wav")},
        )
        generated = client.post(
            "/v1/generate",
            headers=_headers(device_id, generate_key),
            json={
                "type": "text",
                "pipeline": "pipeline-3",
                "text": normalized.text,
            },
        )

    assert normalized.status_code == 200
    assert normalized.headers["content-type"].startswith("text/plain")
    assert normalized.text == "A calm moonlit landscape made from simple, clean color shapes"
    assert not normalized.text.startswith("{")
    assert generated.status_code == 200
    assert generated.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(generated.content)) as archive:
        assert archive.namelist() == ["image_1.jpg", "image_2.jpg", "image_3.jpg"]
        digests: set[str] = set()
        for name in archive.namelist():
            content = archive.read(name)
            digests.add(hashlib.sha256(content).hexdigest())
            with Image.open(BytesIO(content)) as image:
                assert image.format == "JPEG"
                assert image.mode == "RGB"
                assert image.size == (1024, 1024)
        assert len(digests) == 3

    devices, total = client.app.state.device_registry.list()
    assert total == 1
    assert devices[0].device_id == device_id
    assert devices[0].request_count == 2


def test_pipeline_3_operation_namespaces_allow_same_uuid_for_both_calls(tmp_path) -> None:
    operation_id = str(uuid.uuid4())
    headers = _headers("td-12345678", operation_id)
    with _client(tmp_path) as client:
        normalized = client.post(
            "/v1/normalize",
            headers=headers,
            data={"type": "audio", "pipeline": "pipeline-3"},
            files={"audio": ("voice.wav", b"RIFF" + b"\0" * 1500, "audio/wav")},
        )
        generated = client.post(
            "/v1/generate",
            headers=headers,
            json={"type": "text", "pipeline": "pipeline-3", "text": normalized.text},
        )
    assert normalized.status_code == generated.status_code == 200


def test_pipeline_3_google_payload_preserves_production_prompt_exactly() -> None:
    payload = build_image_variant_request(
        "cute penguin icon", PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION
    )
    assert payload["systemInstruction"] == {
        "parts": [{"text": PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION}]
    }
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "cute penguin icon"}]}
    ]
    assert payload["generationConfig"] == {
        "responseModalities": ["IMAGE", "TEXT"],
        "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
    }


def test_pipeline_configs_and_secrets_are_isolated(tmp_path) -> None:
    with _client(tmp_path) as client:
        store = client.app.state.production_pipeline_config_store
        pipeline_1 = store.get_pipeline("pipeline-1")
        pipeline_3 = store.get_pipeline("pipeline-3")
        pipeline_3["google_image_model"] = "models/custom-image-model"
        saved = store.set_pipeline("pipeline-3", pipeline_3)
        assert saved["google_image_model"] == "custom-image-model"
        assert store.get_pipeline("pipeline-1") == pipeline_1

        secrets = client.app.state.production_secret_store
        secrets.set_google_api_key("pipeline-one-google-key-123", "pipeline-1")
        secrets.set_google_api_key("pipeline-three-google-key-456", "pipeline-3")
        assert secrets.get_google_api_key("pipeline-1") != secrets.get_google_api_key(
            "pipeline-3"
        )
