from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.services.production_requests import build_production_request_templates
from app.settings import Settings

router = APIRouter(prefix="/internal/production", include_in_schema=False)


class ProductionGoogleKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    google_api_key: SecretStr = Field(min_length=16, max_length=512)


def _authorize(request: Request, *, mutate: bool = False) -> Settings:
    request.app.state.operator_auth_store.authenticate_request(
        request, require_csrf=mutate
    )
    return request.app.state.moonli_settings


def _payload(request: Request, settings: Settings) -> dict[str, object]:
    profiles = request.app.state.pipeline_profiles
    secret_store = request.app.state.production_secret_store
    server_configuration = request.app.state.server_settings_store.get()
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
        },
    }


@router.get("/config")
def production_config(
    request: Request,
    response: Response,
) -> dict[str, object]:
    settings = _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return _payload(request, settings)


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
    await request.app.state.google_key_validator.validate(value)
    request.app.state.production_secret_store.set_google_api_key(value)
    request.app.state.audit_store.append(
        action="production.google_key.update",
        outcome="success",
        summary="Production Google API key was validated and replaced.",
        target_type="credential",
        target_id="google-api-key",
        request_id=getattr(request.state, "request_id", None),
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "google_key": request.app.state.production_secret_store.status(
            settings.google_api_key
        )
    }


@router.delete("/google-key")
def clear_production_google_key(request: Request, response: Response) -> dict[str, object]:
    settings = _authorize(request, mutate=True)
    request.app.state.production_secret_store.clear_google_api_key()
    request.app.state.audit_store.append(
        action="production.google_key.clear",
        outcome="success",
        summary="Production Google API key was removed.",
        target_type="credential",
        target_id="google-api-key",
        request_id=getattr(request.state, "request_id", None),
    )
    response.headers["Cache-Control"] = "no-store"
    return {"google_key": request.app.state.production_secret_store.status(settings.google_api_key)}
