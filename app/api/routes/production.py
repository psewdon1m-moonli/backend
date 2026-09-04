from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.errors import MoonliError
from app.api.routes.routing import save_routing_configuration
from app.composition import apply_production_pipeline_configuration
from app.services.google_key_validator import GoogleKeyValidator
from app.services.production_requests import build_production_request_templates
from app.settings import Settings
from app.storage.production_pipeline_config import (
    PIPELINE_IDS,
    ProductionPipelineConfigStore,
)

router = APIRouter(prefix="/internal/production", include_in_schema=False)
INTEGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[3] / "integrations" / "touchdesigner"
)
PIPELINE_3_NORMALIZE_REQUEST = """POST /v1/generate HTTP/1.1
Host: moonli.shmoza.net
Authorization: Bearer <MOONLI_ACCESS_KEY>
X-Moonli-Device-Id: td-########
Idempotency-Key: <NEW_UUID_FOR_THIS_OPERATION>
Accept: text/plain
Content-Type: multipart/form-data; boundary=<CLIENT_GENERATED_BOUNDARY>

--<CLIENT_GENERATED_BOUNDARY>
Content-Disposition: form-data; name="type"

audio
--<CLIENT_GENERATED_BOUNDARY>
Content-Disposition: form-data; name="pipeline"

pipeline-3
--<CLIENT_GENERATED_BOUNDARY>
Content-Disposition: form-data; name="audio"; filename="voice.wav"
Content-Type: audio/wav

<BINARY CONTENT OF voice.wav>
--<CLIENT_GENERATED_BOUNDARY>--

Expected success response:
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8
Cache-Control: no-store

<NORMALIZED_RUSSIAN_TEXT_ONLY>"""
PIPELINE_3_GENERATE_REQUEST = """POST /v1/generate HTTP/1.1
Host: moonli.shmoza.net
Authorization: Bearer <MOONLI_ACCESS_KEY>
X-Moonli-Device-Id: <SAME_TD_DEVICE_ID>
Idempotency-Key: <NEW_UUID_FOR_THIS_OPERATION>
Accept: application/zip
Content-Type: application/json; charset=utf-8

{
  "type": "text",
  "pipeline": "pipeline-3",
  "text": "<NORMALIZED_RUSSIAN_TEXT_FROM_THE_FIRST_REQUEST>"
}

Moonli translates this text into a concise English image prompt before generation.

Expected success response:
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename="moonli-images.zip"
Cache-Control: no-store

ZIP members (exactly these three files):
image_1.jpg  # JPEG, RGB, 1024x1024
image_2.jpg  # JPEG, RGB, 1024x1024
image_3.jpg  # JPEG, RGB, 1024x1024"""


class ProductionGoogleKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    google_api_key: SecretStr = Field(min_length=16, max_length=512)
    pipeline: str | None = None


class ProductionPipelineControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pipeline"]
    pipeline: str
    config: dict[str, object]


class ProductionRoutingControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["routing"]
    enabled: bool
    vless_uri: SecretStr | None = Field(default=None, max_length=4096)


ProductionControlUpdate = Annotated[
    ProductionPipelineControlUpdate | ProductionRoutingControlUpdate,
    Field(discriminator="action"),
]


def _authorize(request: Request, *, mutate: bool = False) -> Settings:
    request.app.state.operator_auth_store.authenticate_request(
        request, require_csrf=mutate
    )
    return request.app.state.moonli_settings


def _pipeline_config_store(request: Request) -> ProductionPipelineConfigStore:
    existing = getattr(request.app.state, "production_pipeline_config_store", None)
    if existing is not None:
        return existing
    settings: Settings = request.app.state.moonli_settings
    server_configuration = request.app.state.server_settings_store.get()
    store = ProductionPipelineConfigStore(
        settings.data_dir / "production-pipelines.json",
        settings,
        server_configuration["prompt_templates"],
    )
    request.app.state.production_pipeline_config_store = store
    return store


def _pipeline_3_integration_payload() -> dict[str, object]:
    try:
        transcription_script = (
            INTEGRATION_DIRECTORY / "pipeline3_transcription.py"
        ).read_text(encoding="utf-8")
        generation_script = (
            INTEGRATION_DIRECTORY / "pipeline3_generation.py"
        ).read_text(encoding="utf-8")
    except OSError as exc:
        raise MoonliError(
            "INTEGRATION_ASSET_UNAVAILABLE",
            "The TouchDesigner integration assets are unavailable.",
            500,
        ) from exc
    return {
        "pipeline": "pipeline-3",
        "requests": [
            {
                "id": "normalize-audio",
                "title": "1 · Audio transcription and normalization",
                "content": PIPELINE_3_NORMALIZE_REQUEST,
            },
            {
                "id": "generate-images",
                "title": "2 · Translate prompt and generate three images",
                "content": PIPELINE_3_GENERATE_REQUEST,
            },
        ],
        "scripts": [
            {
                "id": "touchdesigner-transcription",
                "title": "1 · TouchDesigner transcription script",
                "filename": "pipeline3_transcription.py",
                "content": transcription_script,
            },
            {
                "id": "touchdesigner-generation",
                "title": "2 · TouchDesigner generation script",
                "filename": "pipeline3_generation.py",
                "content": generation_script,
            },
        ],
        "installation": {
            "required_change": (
                "Both scripts include the configured Moonli domain and client Access Key "
                "and are ready to paste into TouchDesigner."
            ),
            "shared_device_identity": ".moonli/device_id.txt",
            "domain": "https://moonli.shmoza.net",
        },
    }


def _payload(request: Request, settings: Settings) -> dict[str, object]:
    profiles = request.app.state.pipeline_profiles
    secret_store = request.app.state.production_secret_store
    server_configuration = request.app.state.server_settings_store.get()
    pipeline_config_store = _pipeline_config_store(request)
    pipeline_configurations = pipeline_config_store.get()["pipelines"]
    assert isinstance(pipeline_configurations, dict)
    production_pipelines: dict[str, object] = {}
    for pipeline_id in PIPELINE_IDS:
        configuration = dict(pipeline_configurations[pipeline_id])
        configuration["google_key"] = secret_store.status(
            settings.google_api_key, pipeline_id
        )
        configuration["output"] = (
            {
                "type": "jpeg-set",
                "count": 3,
                "width": 1024,
                "height": 1024,
                "files": ["image_1.jpg", "image_2.jpg", "image_3.jpg"],
                "post_processing": False,
            }
            if pipeline_id == "pipeline-3"
            else {
                "type": profiles[pipeline_id].output_mode,
                "width": profiles[pipeline_id].width,
                "height": profiles[pipeline_id].height,
            }
        )
        production_pipelines[pipeline_id] = configuration
    return {
        "service": "Moonli",
        "environment": settings.environment,
        "providers": {
            "image": settings.image_provider,
            "transcription": settings.transcription_provider,
            "normalization": settings.normalization_provider,
            "google_key_configured_on_server": secret_store.status(
                settings.google_api_key
            )["configured"],
        },
        "google": {
            "base_url": settings.google_api_base_url,
            "image_model": settings.google_image_model,
            "transcription_model": settings.google_transcription_model,
            "normalization_model": settings.google_normalization_model,
            "translation_model": settings.google_translation_model,
            "timeout_seconds": settings.google_timeout_seconds,
            "aspect_ratio": settings.google_image_aspect_ratio,
            "image_size": settings.google_image_size,
        },
        "pipelines": {
            profile_id: {
                "output_mode": profile.output_mode,
                "palette_version": profile.palette.id,
                "palette": list(profile.palette.colors),
                "canvas": {"width": profile.width, "height": profile.height},
            }
            for profile_id, profile in profiles.items()
        }
        | {
            "pipeline-3": {
                "output_mode": "jpeg_set",
                "palette_version": None,
                "palette": [],
                "canvas": {"width": 1024, "height": 1024},
            }
        },
        "prompt_templates": server_configuration["prompt_templates"],
        "prompt_template_fields": [
            "input_text",
            "subject",
            "must_include",
            "must_avoid",
            "supporting_details",
            "constraints",
            "palette",
            "width",
            "height",
            "pipeline",
        ],
        "palette_processing": {
            "quantization_space": "CIE Lab (CIE76)",
            "cleanup_passes": settings.palette_cleanup_passes,
            "generation_attempts": settings.palette_generation_attempts,
            "strict_validation_snap_distance": 0,
        },
        "production": {
            "google_key": secret_store.status(settings.google_api_key),
            "storage": "docker-volume",
            "requests": build_production_request_templates(settings, profiles),
            "pipelines": production_pipelines,
            "pipeline_3_integration": _pipeline_3_integration_payload(),
        },
        "routing": request.app.state.routing_config_store.status(),
    }


def _require_pipeline(pipeline: str) -> str:
    if pipeline not in PIPELINE_IDS:
        raise MoonliError("INVALID_PIPELINE", "Unknown production pipeline.", 404)
    return pipeline


def _save_pipeline_config(
    request: Request, pipeline: str, payload: dict[str, object]
) -> dict[str, object]:
    pipeline = _require_pipeline(pipeline)
    try:
        stored = _pipeline_config_store(request).set_pipeline(pipeline, payload)
        apply_production_pipeline_configuration(request.app)
    except (TypeError, ValueError) as exc:
        raise MoonliError("INVALID_SETTINGS", str(exc), 422) from exc
    request.app.state.audit_store.append(
        action="production.pipeline.update",
        outcome="success",
        summary="Production pipeline configuration was updated.",
        target_type="pipeline",
        target_id=pipeline,
        request_id=getattr(request.state, "request_id", None),
    )
    return {"pipeline": pipeline, "config": stored}


async def _save_google_key(
    request: Request,
    settings: Settings,
    value: str,
    pipeline: str | None,
) -> dict[str, object]:
    if pipeline is None:
        validator = request.app.state.google_key_validator
    else:
        pipeline = _require_pipeline(pipeline)
        configuration = _pipeline_config_store(request).get_pipeline(pipeline)
        validator = GoogleKeyValidator(
            str(configuration["google_api_base_url"]),
            float(configuration["google_timeout_seconds"]),
            request.app.state.routing_config_store.proxy_url,
        )
    await validator.validate(value)
    request.app.state.production_secret_store.set_google_api_key(value, pipeline)
    request.app.state.audit_store.append(
        action="production.google_key.update",
        outcome="success",
        summary=(
            "A pipeline Google API key was validated and replaced."
            if pipeline
            else "Production Google API key was validated and replaced."
        ),
        target_type="credential",
        target_id=(f"google-api-key:{pipeline}" if pipeline else "google-api-key"),
        request_id=getattr(request.state, "request_id", None),
    )
    result = {
        "google_key": request.app.state.production_secret_store.status(
            settings.google_api_key, pipeline
        )
    }
    if pipeline:
        result["pipeline"] = pipeline
    return result


def _clear_google_key(
    request: Request, settings: Settings, pipeline: str | None
) -> dict[str, object]:
    if pipeline is not None:
        pipeline = _require_pipeline(pipeline)
    request.app.state.production_secret_store.clear_google_api_key(pipeline)
    request.app.state.audit_store.append(
        action="production.google_key.clear",
        outcome="success",
        summary=(
            "A pipeline Google API key was removed."
            if pipeline
            else "Production Google API key was removed."
        ),
        target_type="credential",
        target_id=(f"google-api-key:{pipeline}" if pipeline else "google-api-key"),
        request_id=getattr(request.state, "request_id", None),
    )
    result = {
        "google_key": request.app.state.production_secret_store.status(
            settings.google_api_key, pipeline
        )
    }
    if pipeline:
        result["pipeline"] = pipeline
    return result


@router.get("/config")
def production_config(
    request: Request,
    response: Response,
) -> dict[str, object]:
    settings = _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return _payload(request, settings)


@router.put("/config")
def update_production_control(
    payload: ProductionControlUpdate,
    request: Request,
    response: Response,
) -> dict[str, object]:
    _authorize(request, mutate=True)
    if isinstance(payload, ProductionPipelineControlUpdate):
        result = _save_pipeline_config(request, payload.pipeline, payload.config)
    else:
        result = save_routing_configuration(
            request,
            enabled=payload.enabled,
            vless_uri=(
                payload.vless_uri.get_secret_value()
                if payload.vless_uri is not None
                else None
            ),
        )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/pipelines/pipeline-3/integration")
def pipeline_3_integration_kit(
    request: Request,
    response: Response,
) -> dict[str, object]:
    _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return _pipeline_3_integration_payload()


@router.get("/stats")
def production_stats(
    request: Request,
    response: Response,
) -> dict[str, object]:
    _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "system": request.app.state.system_monitor.snapshot(),
        "production_api": request.app.state.production_usage_store.summary(hours=24),
    }


@router.put("/google-key")
async def update_production_google_key(
    payload: ProductionGoogleKeyUpdate,
    request: Request,
    response: Response,
) -> dict[str, object]:
    settings = _authorize(request, mutate=True)
    value = payload.google_api_key.get_secret_value()
    result = await _save_google_key(request, settings, value, payload.pipeline)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/google-key")
def clear_production_google_key(
    request: Request,
    response: Response,
    pipeline: str | None = Query(default=None),
) -> dict[str, object]:
    settings = _authorize(request, mutate=True)
    result = _clear_google_key(request, settings, pipeline)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.put("/pipelines/{pipeline}/config")
def update_pipeline_config(
    pipeline: str,
    payload: dict[str, object],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _authorize(request, mutate=True)
    result = _save_pipeline_config(request, pipeline, payload)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.put("/pipelines/{pipeline}/google-key")
async def update_pipeline_google_key(
    pipeline: str,
    payload: ProductionGoogleKeyUpdate,
    request: Request,
    response: Response,
) -> dict[str, object]:
    settings = _authorize(request, mutate=True)
    if payload.pipeline is not None and payload.pipeline != pipeline:
        raise MoonliError("INVALID_PIPELINE", "Pipeline fields do not match.", 422)
    value = payload.google_api_key.get_secret_value()
    result = await _save_google_key(request, settings, value, pipeline)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/pipelines/{pipeline}/google-key")
def clear_pipeline_google_key(
    pipeline: str, request: Request, response: Response
) -> dict[str, object]:
    settings = _authorize(request, mutate=True)
    result = _clear_google_key(request, settings, pipeline)
    response.headers["Cache-Control"] = "no-store"
    return result
