from __future__ import annotations

import json
import sqlite3
import uuid
import zipfile
from dataclasses import replace
from io import BytesIO
from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.errors import install_error_handlers
from app.api.routes import lab as lab_routes
from app.api.routes.lab import router
from app.composition import build_components
from app.providers.errors import NoVisualSubjectError
from app.providers.image_generation.variants import MockImageVariantGenerator
from app.providers.prompt_normalization import google as normalization_google
from app.providers.prompt_normalization.mock import MockPromptNormalizer
from app.settings import Settings


class _NormalizationResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"normalized_prompt":"apple tree with red apples"}'}
                        ]
                    }
                }
            ]
        }


class _NormalizationClient:
    captured: ClassVar[dict] = {}

    def __init__(self, **kwargs) -> None:
        self.captured["timeout"] = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, json: dict, headers: dict) -> _NormalizationResponse:
        self.captured.update({"url": url, "json": json, "headers": headers})
        return _NormalizationResponse()


class _ProductionNormalizer(MockPromptNormalizer):
    name = "google"


class _NoVisualSubjectNormalizer(MockPromptNormalizer):
    name = "google"

    async def normalize(self, text: str) -> str:
        raise NoVisualSubjectError("The request contains no visual subject")


class _ProductionImageVariants(MockImageVariantGenerator):
    name = "google"


class _ForbiddenProductionStore:
    def __getattr__(self, name: str):
        raise AssertionError(f"test route accessed production state: {name}")


def _client(tmp_path, *, environment: str = "test") -> TestClient:
    settings = replace(
        Settings.from_env(),
        environment=environment,
        data_dir=tmp_path / "lab-data",
        secrets_dir=tmp_path / "lab-secrets",
        api_keys=("lab-access-key",),
        operator_access_key="lab-operator-access-key-01",
    )
    if environment == "production":
        settings = replace(
            settings,
            image_provider="google",
            transcription_provider="google",
            normalization_provider="google",
            google_image_model="test-image-model",
            google_transcription_model="test-transcription-model",
            google_normalization_model="test-normalization-model",
            google_api_key="",
        )
    components = build_components(settings)
    app = FastAPI()
    app.state.moonli_settings = settings
    app.state.client_authenticator = components.authenticator
    app.state.pipeline_profiles = components.profiles
    app.state.rate_limiter = components.rate_limiter
    app.state.artifact_store = components.artifact_store
    app.state.production_secret_store = components.production_secret_store
    app.state.production_pipeline_config_store = (
        components.production_pipeline_config_store
    )
    app.state.run_repository = components.run_repository
    app.state.metrics = components.metrics
    app.state.operator_auth_store = components.operator_auth_store
    app.state.server_settings_store = components.server_settings_store
    install_error_handlers(app)
    app.include_router(router)
    client = TestClient(app)
    session = components.operator_auth_store.create_session("lab-operator-access-key-01")
    client.cookies.set(settings.operator_cookie_name, session.token)
    client.headers.update({"X-CSRF-Token": session.csrf_token})
    return client


def _headers(*, idempotency: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if idempotency:
        headers["Idempotency-Key"] = f"lab-{uuid.uuid4().hex}"
    return headers


def test_lab_config_and_custom_prompt_are_authenticated(tmp_path) -> None:
    with _client(tmp_path) as client:
        client.cookies.clear()
        unauthorized = client.get("/internal/test/config")
        login = client.post(
            "/internal/auth/session",
            json={"access_key": "lab-operator-access-key-01"},
        )
        # This focused app mounts only the lab router, so recreate a session directly.
        if login.status_code == 404:
            session = client.app.state.operator_auth_store.create_session(
                "lab-operator-access-key-01"
            )
            client.cookies.set(
                client.app.state.moonli_settings.operator_cookie_name, session.token
            )
            client.headers.update({"X-CSRF-Token": session.csrf_token})
        config = client.get("/internal/test/config", headers=_headers())
        prompt = client.post(
            "/internal/test/prompt",
            headers=_headers(),
            json={
                "pipeline": "pipeline-1",
                "text": "A red tree without people",
                "prompt_template": "{pipeline}\n{subject}\n{must_avoid}\n{palette}\n{width}x{height}",
            },
        )
        invalid_template = client.post(
            "/internal/test/prompt",
            headers=_headers(),
            json={
                "pipeline": "pipeline-1",
                "text": "Moon",
                "prompt_template": "{unknown_field}",
            },
        )
    assert unauthorized.status_code == 401
    assert config.status_code == 200
    assert set(config.json()["pipelines"]) == {
        "pipeline-1",
        "pipeline-2",
        "pipeline-3",
    }
    assert "api_key" not in json.dumps(config.json()).lower()
    assert prompt.status_code == 200
    assert prompt.json()["prompt"].startswith("pipeline-1")
    assert "#" in prompt.json()["prompt"]
    assert invalid_template.status_code == 422
    assert invalid_template.json()["error"]["code"] == "PROMPT_BUILD_FAILED"


def test_lab_mock_transcription_and_image_stages(tmp_path) -> None:
    with _client(tmp_path) as client:
        transcription = client.post(
            "/internal/test/transcribe",
            headers=_headers(),
            data={"provider": "mock"},
            files={"audio": ("voice.wav", b"RIFF-lab-audio", "audio/wav")},
        )
        image = client.post(
            "/internal/test/image",
            headers=_headers(),
            json={
                "pipeline": "pipeline-2",
                "provider": "mock",
                "prompt": "A flat moon",
                "validate_palette": True,
            },
        )
    assert transcription.status_code == 200
    assert transcription.json()["provider"] == "mock"
    assert transcription.json()["transcription"]
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-moonli-palette-valid"] == "true"
    decoded = Image.open(BytesIO(image.content))
    assert decoded.size == (1024, 1024)


def test_lab_prompt_normalization_is_a_separate_authenticated_stage(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/internal/test/normalize",
            headers=_headers(),
            json={
                "provider": "mock",
                "text": "Привет, сделай яблоню с красными яблоками",
            },
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "mock"
    assert response.json()["normalized_text"] == "Привет, сделай яблоню с красными яблоками"


def test_lab_prompt_normalization_reports_missing_visual_subject(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        lab_routes,
        "_make_prompt_normalizer",
        lambda *args, **kwargs: _NoVisualSubjectNormalizer(),
    )
    with _client(tmp_path) as client:
        response = client.post(
            "/internal/test/normalize",
            headers=_headers(),
            json={"provider": "mock", "text": "Там впереди направо."},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "NO_VISUAL_SUBJECT",
            "message": "Say what you want to draw.",
        }
    }


def test_lab_palette_and_layer_package_stages(tmp_path) -> None:
    with _client(tmp_path) as client:
        source = client.post(
            "/internal/test/image",
            headers=_headers(),
            json={"pipeline": "pipeline-2", "provider": "mock", "prompt": "Moon"},
        )
        report = client.post(
            "/internal/test/palette",
            headers=_headers(),
            data={"pipeline": "pipeline-2", "snap_distance": "12"},
            files={"image": ("source.png", source.content, "image/png")},
        )
        package = client.post(
            "/internal/test/package",
            headers=_headers(),
            data={"pipeline": "pipeline-2", "snap_distance": "12"},
            files={"image": ("source.png", source.content, "image/png")},
        )
    assert report.status_code == 200
    assert report.json()["valid"] is True
    assert package.status_code == 200
    with zipfile.ZipFile(BytesIO(package.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["layers"]) == 12


def test_lab_quantization_stage_returns_strict_palette_png(tmp_path) -> None:
    source = Image.new("RGBA", (1024, 1024), (76, 150, 197, 255))
    source.putpixel((512, 512), (134, 173, 2, 255))
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    with _client(tmp_path) as client:
        quantized = client.post(
            "/internal/test/quantize",
            headers=_headers(),
            data={"pipeline": "pipeline-1", "cleanup_passes": "1"},
            files={"image": ("source.png", buffer.getvalue(), "image/png")},
        )
        report = client.post(
            "/internal/test/palette",
            headers=_headers(),
            data={"pipeline": "pipeline-1", "snap_distance": "0"},
            files={"image": ("quantized.png", quantized.content, "image/png")},
        )
    assert quantized.status_code == 200
    assert quantized.headers["content-type"] == "image/png"
    assert quantized.headers["x-moonli-quantized"] == "true"
    assert int(quantized.headers["x-moonli-changed-pixels"]) > 0
    assert int(quantized.headers["x-moonli-unique-colors-after"]) <= 6
    assert report.status_code == 200
    assert report.json()["valid"] is True


def test_lab_vectorization_and_segmentation_stages_form_png_svg_zip_chain(tmp_path) -> None:
    with _client(tmp_path) as client:
        source = client.post(
            "/internal/test/image",
            headers=_headers(),
            json={"pipeline": "pipeline-2", "provider": "mock", "prompt": "Moon"},
        )
        vector = client.post(
            "/internal/test/vectorize",
            headers=_headers(),
            data={"pipeline": "pipeline-2"},
            files={"image": ("source.png", source.content, "image/png")},
        )
        segmented = client.post(
            "/internal/test/segment",
            headers=_headers(),
            data={"pipeline": "pipeline-2"},
            files={"vector": ("moonli-vector.svg", vector.content, "image/svg+xml")},
        )
    assert vector.status_code == 200
    assert vector.headers["content-type"].startswith("image/svg+xml")
    assert vector.headers["x-moonli-vectorized"] == "true"
    assert b'data-moonli-vector-contract="1.0"' in vector.content
    assert segmented.status_code == 200
    assert segmented.headers["x-moonli-segmented"] == "true"
    assert int(segmented.headers["x-moonli-total-layers"]) == 12
    with zipfile.ZipFile(BytesIO(segmented.content)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "master.svg", "layers/00.svg", "layers/11.svg"} <= names
    assert len(manifest["layers"]) == 12


def test_lab_full_pipeline_supports_text_and_audio_with_explicit_tags(tmp_path) -> None:
    with _client(tmp_path) as client:
        text = client.post(
            "/internal/test/pipeline",
            headers=_headers(idempotency=True),
            data={
                "type": "text",
                "pipeline": "pipeline-1",
                "text": "A clean geometric moon",
                "image_provider": "mock",
                "transcription_provider": "mock",
                "normalization_provider": "mock",
                "prompt_template": "{input_text}\n{constraints}\n{palette}",
            },
        )
        audio = client.post(
            "/internal/test/pipeline",
            headers=_headers(idempotency=True),
            data={
                "type": "audio",
                "pipeline": "pipeline-2",
                "image_provider": "mock",
                "transcription_provider": "mock",
                "normalization_provider": "mock",
            },
            files={"audio": ("voice.ogg", b"OggS-lab-audio", "audio/ogg")},
        )
    assert text.status_code == 200
    assert text.headers["content-type"] == "application/vnd.moonli.run-artifacts+zip"
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "application/vnd.moonli.run-artifacts+zip"
    assert text.headers["x-moonli-image-provider"] == "mock"
    assert audio.headers["x-moonli-transcription-provider"] == "mock"
    assert text.headers["x-moonli-pipeline"] == "pipeline-1"
    assert text.headers["x-moonli-input-type"] == "text"
    assert audio.headers["x-moonli-pipeline"] == "pipeline-2"
    assert audio.headers["x-moonli-input-type"] == "audio"

    with zipfile.ZipFile(BytesIO(text.content)) as archive:
        text_names = set(archive.namelist())
        text_manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("input/request.txt").decode() == "A clean geometric moon"
    assert {
        "text/normalized.txt",
        "prompt/prompt.txt",
        "prompt/visual-brief.json",
        "images/generated.png",
        "images/quantized.png",
        "images/validated.png",
        "reports/quantization.json",
        "reports/palette-validation.json",
        "reports/execution-trace.json",
        "output/final.png",
    } <= text_names
    assert "text/transcription.txt" not in text_names
    assert "vector/master.svg" not in text_names
    assert not any(name.startswith("layers/") for name in text_names)
    assert text_manifest["pipeline"] == "pipeline-1"
    assert text_manifest["input_type"] == "text"
    text_stages = {stage["name"]: stage for stage in text_manifest["stages"]}
    assert text_stages["transcription"]["status"] == "not_applicable"
    assert text_stages["vectorization"]["status"] == "not_applicable"
    assert text_stages["segmentation"]["status"] == "not_applicable"

    with zipfile.ZipFile(BytesIO(audio.content)) as archive:
        audio_names = set(archive.namelist())
        audio_manifest = json.loads(archive.read("manifest.json"))
        raster_manifest = json.loads(archive.read("layers/raster/manifest.json"))
        vector_manifest = json.loads(archive.read("layers/vector/manifest.json"))
        assert archive.read("input/voice.ogg") == b"OggS-lab-audio"
        validation = json.loads(archive.read("reports/palette-validation.json"))
    assert {
        "text/transcription.txt",
        "text/normalized.txt",
        "prompt/prompt.txt",
        "images/generated.png",
        "images/quantized.png",
        "reports/palette-validation.json",
        "vector/master.svg",
        "layers/vector/00.svg",
        "layers/vector/11.svg",
        "layers/raster/00.png",
        "layers/raster/11.png",
        "output/final-layer-package.zip",
    } <= audio_names
    assert audio_manifest["pipeline"] == "pipeline-2"
    assert audio_manifest["input_type"] == "audio"
    assert validation["valid"] is True
    assert raster_manifest["composite"] in audio_names
    assert all(layer["image"] in audio_names for layer in raster_manifest["layers"])
    assert vector_manifest["master"] in audio_names
    assert all(layer["image"] in audio_names for layer in vector_manifest["layers"])
    audio_stages = {stage["name"]: stage for stage in audio_manifest["stages"]}
    assert all(stage["status"] == "included" for stage in audio_stages.values())


def test_lab_full_pipeline_normalizes_text_before_prompt_builder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(normalization_google.httpx, "AsyncClient", _NormalizationClient)
    headers = _headers(idempotency=True)
    headers["X-Google-API-Key"] = "google-test-key"
    with _client(tmp_path) as client:
        response = client.post(
            "/internal/test/pipeline",
            headers=headers,
            data={
                "type": "text",
                "pipeline": "pipeline-1",
                "text": (
                    "Привет, это тестовый запуск. А, сделай мне, пожалуйста, "
                    "яблоню с красными яблоками."
                ),
                "image_provider": "mock",
                "transcription_provider": "mock",
                "normalization_provider": "google",
                "google_normalization_model": "normalization-model",
            },
        )
    assert response.status_code == 200
    assert response.headers["x-moonli-normalization-provider"] == "google"
    with sqlite3.connect(tmp_path / "lab-data" / "runs.sqlite3") as connection:
        normalized_text, prompt = connection.execute(
            "SELECT normalized_text, prompt FROM generation_runs"
        ).fetchone()
    assert normalized_text == "apple tree with red apples"
    assert "Visual subject: apple tree with red apples" in prompt
    assert "тестовый запуск" not in prompt


def test_lab_full_pipeline_supports_pipeline_3_text_and_audio(tmp_path) -> None:
    with _client(tmp_path) as client:
        client.app.state.production_secret_store = _ForbiddenProductionStore()
        client.app.state.production_pipeline_config_store = _ForbiddenProductionStore()
        text = client.post(
            "/internal/test/pipeline",
            headers=_headers(idempotency=True),
            data={
                "type": "text",
                "pipeline": "pipeline-3",
                "text": "cute penguin icon",
            },
        )
        audio = client.post(
            "/internal/test/pipeline",
            headers=_headers(idempotency=True),
            data={"type": "audio", "pipeline": "pipeline-3"},
            files={"audio": ("voice.wav", b"RIFF-pipeline-3-audio", "audio/wav")},
        )

    assert text.status_code == 200
    assert audio.status_code == 200
    assert text.headers["content-type"] == "application/vnd.moonli.run-artifacts+zip"
    assert text.headers["x-moonli-pipeline"] == "pipeline-3"
    assert text.headers["x-moonli-input-type"] == "text"
    assert text.headers["x-moonli-palette-quantized"] == "false"
    assert audio.headers["x-moonli-input-type"] == "audio"
    assert text.headers["x-moonli-run-id"] != audio.headers["x-moonli-run-id"]

    with zipfile.ZipFile(BytesIO(text.content)) as archive:
        text_names = set(archive.namelist())
        text_manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("input/request.txt") == b"cute penguin icon"
        with zipfile.ZipFile(BytesIO(archive.read("output/moonli-images.zip"))) as nested:
            assert nested.namelist() == ["image_1.jpg", "image_2.jpg", "image_3.jpg"]
            for name in nested.namelist():
                image = Image.open(BytesIO(nested.read(name)))
                assert image.size == (1024, 1024)
                assert image.mode == "RGB"
                assert image.format == "JPEG"
    assert {
        "text/normalized.txt",
        "prompt/prompt.txt",
        "images/image_1.jpg",
        "images/image_2.jpg",
        "images/image_3.jpg",
        "reports/execution-trace.json",
        "output/moonli-images.zip",
    } <= text_names
    assert "text/transcription.txt" not in text_names
    assert text_manifest["pipeline"] == "pipeline-3"
    assert text_manifest["output_mode"] == "jpeg_set"
    assert text_manifest["palette_version"] is None
    text_stages = {stage["name"]: stage for stage in text_manifest["stages"]}
    assert text_stages["normalization"]["status"] == "included"
    assert text_stages["prompt_building"]["status"] == "included"
    assert text_stages["image_generation"]["status"] == "included"
    for stage in (
        "transcription",
        "quantization",
        "validation",
        "vectorization",
        "segmentation",
    ):
        assert text_stages[stage]["status"] == "not_applicable"

    with zipfile.ZipFile(BytesIO(audio.content)) as archive:
        audio_names = set(archive.namelist())
        audio_manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("input/voice.wav") == b"RIFF-pipeline-3-audio"
        assert archive.read("text/transcription.txt")
    assert "text/transcription.txt" in audio_names
    assert audio_manifest["pipeline"] == "pipeline-3"
    assert audio_manifest["input_type"] == "audio"
    audio_stages = {stage["name"]: stage for stage in audio_manifest["stages"]}
    assert audio_stages["transcription"]["status"] == "included"


def test_google_test_call_requires_browser_key_without_production_fallback(
    tmp_path,
) -> None:
    with _client(tmp_path) as client:
        client.app.state.moonli_settings = replace(
            client.app.state.moonli_settings,
            google_api_key="must-not-be-used-by-test-routes",
        )
        client.app.state.production_secret_store = _ForbiddenProductionStore()
        response = client.post(
            "/internal/test/normalize",
            headers=_headers(),
            json={
                "provider": "google",
                "text": "cute fox",
                "google_normalization_model": "gemini-test-model",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "INVALID_INPUT",
        "message": "A Google API key is required for a Google-provider test.",
    }


def test_lab_is_available_to_authenticated_operator_in_production(tmp_path) -> None:
    with _client(tmp_path, environment="production") as client:
        config = client.get("/internal/test/config", headers=_headers())
        mock_call = client.post(
            "/internal/test/normalize",
            headers=_headers(),
            json={"provider": "mock", "text": "moon"},
        )
        client.cookies.clear()
        unauthorized = client.get("/internal/test/config", headers=_headers())
    assert config.status_code == 200
    assert config.json()["environment"] == "production"
    assert mock_call.status_code == 200
    assert mock_call.json()["normalized_text"] == "moon"
    assert unauthorized.status_code == 401


def test_pipeline_3_test_run_uses_request_config_in_production(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        lab_routes,
        "_make_prompt_normalizer",
        lambda *args, **kwargs: _ProductionNormalizer(),
    )
    monkeypatch.setattr(
        lab_routes,
        "_make_image_variant_generator",
        lambda *args, **kwargs: _ProductionImageVariants(),
    )
    with _client(tmp_path, environment="production") as client:
        client.app.state.production_secret_store = _ForbiddenProductionStore()
        client.app.state.production_pipeline_config_store = _ForbiddenProductionStore()
        headers = _headers(idempotency=True)
        headers["X-Google-API-Key"] = "browser-test-key"
        response = client.post(
            "/internal/test/pipeline",
            headers=headers,
            data={
                "type": "text",
                "pipeline": "pipeline-3",
                "text": "cute fox icon",
                "image_provider": "google",
                "transcription_provider": "google",
                "normalization_provider": "google",
                "google_image_model": "test-image-model",
                "google_transcription_model": "test-transcription-model",
                "google_normalization_model": "test-normalization-model",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-moonli-pipeline"] == "pipeline-3"
    assert response.headers["x-moonli-image-provider"] == "google"
    assert response.headers["x-moonli-normalization-provider"] == "google"
