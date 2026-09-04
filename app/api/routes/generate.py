from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.errors import MoonliError
from app.api.models import PipelineTag, TextGenerateRequest
from app.domain.inputs import AudioInput, GenerationInput, TextInput
from app.settings import Settings

router = APIRouter()
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
AUDIO_ALIASES = {
    "application/ogg": "audio/ogg",
    "audio/x-wav": "audio/wav",
    "audio/x-aac": "audio/aac",
    "audio/x-flac": "audio/flac",
    "audio/mp3": "audio/mpeg",
}


def _audio_type(filename: str, content_type: str | None) -> str:
    raw = (content_type or "").split(";", 1)[0].strip().lower()
    raw = AUDIO_ALIASES.get(raw, raw)
    if raw and raw != "application/octet-stream":
        return raw
    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or "application/octet-stream").lower()


async def _parse_input(
    request: Request, settings: Settings
) -> tuple[GenerationInput, bytes, PipelineTag]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise MoonliError("INVALID_INPUT", "Invalid Content-Length header.", 400) from exc
        max_request_size = settings.max_audio_size + 1024 * 1024
        if declared_size > max_request_size:
            raise MoonliError("AUDIO_TOO_LARGE", "Audio upload exceeds the configured size limit.", 413)

    if content_type == "application/json":
        raw = await request.body()
        if len(raw) > settings.max_text_length * 4 + 4096:
            raise MoonliError("INVALID_INPUT", "JSON request is too large.", 413)
        try:
            model = TextGenerateRequest.model_validate_json(raw)
        except ValidationError as exc:
            raise MoonliError(
                "INVALID_INPUT",
                "Expected JSON with type='text', pipeline, and a text field.",
                422,
            ) from exc
        generation_input = GenerationInput(type="text", text=TextInput(model.text))
        fingerprint = hashlib.sha256(b"text\0" + model.text.encode("utf-8")).digest()
        return generation_input, fingerprint, model.pipeline

    if content_type == "multipart/form-data":
        try:
            form = await request.form(max_files=1, max_fields=4, max_part_size=settings.max_audio_size)
        except Exception as exc:
            raise MoonliError("INVALID_INPUT", "Invalid multipart request.", 422) from exc
        if form.get("type") != "audio":
            raise MoonliError("INVALID_INPUT", "Multipart type must be 'audio'.", 422)
        pipeline = form.get("pipeline")
        if pipeline not in {"pipeline-1", "pipeline-2", "pipeline-3"}:
            raise MoonliError(
                "INVALID_INPUT",
                "Multipart pipeline must be pipeline-1, pipeline-2, or pipeline-3.",
                422,
            )
        upload = form.get("audio")
        if not isinstance(upload, UploadFile):
            raise MoonliError("INVALID_INPUT", "Multipart audio file is missing.", 422)
        filename = Path(upload.filename or "audio.bin").name
        normalized_type = _audio_type(filename, upload.content_type)
        if normalized_type not in settings.supported_audio_types:
            await upload.close()
            raise MoonliError("INVALID_INPUT", "Unsupported audio content type.", 415)
        content = bytearray()
        try:
            while chunk := await upload.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > settings.max_audio_size:
                    raise MoonliError(
                        "AUDIO_TOO_LARGE", "Audio upload exceeds the configured size limit.", 413
                    )
        finally:
            await upload.close()
        if not content:
            raise MoonliError("INVALID_INPUT", "Audio file must not be empty.", 422)
        raw_content = bytes(content)
        generation_input = GenerationInput(
            type="audio",
            audio=AudioInput(content=raw_content, content_type=normalized_type, filename=filename),
        )
        fingerprint = hashlib.sha256(
            b"audio\0" + normalized_type.encode("ascii") + b"\0" + raw_content
        ).digest()
        return generation_input, fingerprint, pipeline

    raise MoonliError(
        "INVALID_INPUT",
        "Content-Type must be application/json or multipart/form-data.",
        415,
    )


def _authenticate_device(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
    device_id: str | None,
    *,
    target_path: str,
):
    authenticated = request.app.state.client_authenticator.authenticate(
        authorization, x_api_key
    )
    device, registered = request.app.state.device_registry.record_request(device_id)
    audit_store = getattr(request.app.state, "audit_store", None)
    if registered and audit_store is not None:
        audit_store.append(
            action="device.register",
            outcome="success",
            summary="A client-generated device identity was registered.",
            actor_type="device",
            actor_id=device.device_id,
            target_type="device",
            target_id=device.device_id,
            request_id=getattr(request.state, "request_id", None),
            context={"connection_type": device.connection_type},
        )
    if device.blocked:
        if audit_store is not None:
            audit_store.append(
                action="device.request.blocked",
                outcome="denied",
                severity="warning",
                summary="A blocked device attempted to call the production API.",
                actor_type="device",
                actor_id=device.device_id,
                target_type="route",
                target_id=target_path,
                request_id=getattr(request.state, "request_id", None),
                context={"connection_type": device.connection_type},
            )
        raise MoonliError("DEVICE_BLOCKED", "This device is blocked.", 403)
    return authenticated, device


def _ensure_pipeline_credential(
    request: Request,
    pipeline: str,
    *,
    provider_fields: tuple[str, ...] = (
        "image_provider",
        "transcription_provider",
        "normalization_provider",
    ),
) -> None:
    settings: Settings = request.app.state.moonli_settings
    config_store = getattr(request.app.state, "production_pipeline_config_store", None)
    if config_store is None:
        google_is_required = "google" in {
            settings.image_provider,
            settings.transcription_provider,
            settings.normalization_provider,
        }
        stored_key = request.app.state.production_secret_store.get_google_api_key()
    else:
        configuration = config_store.get_pipeline(pipeline)
        google_is_required = any(
            configuration[field] == "google" for field in provider_fields
        )
        stored_key = request.app.state.production_secret_store.get_google_api_key(
            pipeline
        )
    if google_is_required and not (stored_key or settings.google_api_key):
        raise MoonliError(
            "GOOGLE_KEY_NOT_CONFIGURED",
            f"The production Google API key for {pipeline} has not been configured.",
            503,
        )


@router.post("/v1/generate")
async def generate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_moonli_device_id: str | None = Header(default=None, alias="X-Moonli-Device-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> FileResponse:
    if not idempotency_key or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise MoonliError(
            "INVALID_INPUT",
            "Idempotency-Key is required and must contain 8-128 safe characters.",
            422,
        )
    authenticated, device = _authenticate_device(
        request,
        authorization,
        x_api_key,
        x_moonli_device_id,
        target_path="/v1/generate",
    )
    audit_store = getattr(request.app.state, "audit_store", None)
    settings: Settings = request.app.state.moonli_settings
    async with request.app.state.rate_limiter.limit(device.device_id):
        generation_input, fingerprint, pipeline = await _parse_input(request, settings)
        _ensure_pipeline_credential(
            request,
            pipeline,
            provider_fields=("image_provider",)
            if pipeline == "pipeline-3"
            else (
                "image_provider",
                "transcription_provider",
                "normalization_provider",
            ),
        )
        request.app.state.production_usage_store.record_request(
            pipeline=pipeline, input_type=generation_input.type
        )
        if pipeline == "pipeline-3":
            if generation_input.type != "text" or generation_input.text is None:
                raise MoonliError(
                    "INVALID_INPUT",
                    "pipeline-3 generation requires normalized text input.",
                    422,
                )
            request_hash = hashlib.sha256(
                device.device_id.encode("ascii")
                + b"\0pipeline-3\0generate\0"
                + fingerprint
            ).hexdigest()
            result = await request.app.state.pipeline3_service.generate(
                normalized_text=generation_input.text.text.strip(),
                idempotency_key=(
                    f"{device.device_id}:pipeline-3:generate:{idempotency_key}"
                ),
                request_hash=request_hash,
            )
        else:
            profile = request.app.state.pipeline_profiles[pipeline]
            request_hash = hashlib.sha256(
                device.device_id.encode("ascii")
                + b"\0"
                + profile.id.encode("ascii")
                + b"\0"
                + fingerprint
            ).hexdigest()
            services = getattr(request.app.state, "generation_services", None)
            service = services[pipeline] if services else request.app.state.generation_service
            result = await service.generate(
                generation_input=generation_input,
                profile=profile,
                idempotency_key=f"{device.device_id}:{idempotency_key}",
                request_hash=request_hash,
            )
    if audit_store is not None:
        audit_store.append(
            action="production.generate",
            outcome="success",
            summary="Production pipeline completed.",
            actor_type="device",
            actor_id=device.device_id,
            target_type="generation_run",
            target_id=result.run_id,
            request_id=getattr(request.state, "request_id", None),
            context={
                "pipeline": pipeline,
                "input_type": generation_input.type,
                "replayed": result.replayed,
                "result_sha256": result.sha256,
                "connection_type": device.connection_type,
                "client_credential_id": authenticated.client_id,
            },
        )
    if pipeline == "pipeline-3":
        return FileResponse(
            result.path,
            media_type="application/zip",
            filename="moonli-images.zip",
            content_disposition_type="attachment",
            headers={"Cache-Control": "no-store"},
        )
    filename = "moonli.png" if result.media_type == "image/png" else "moonli-layers.zip"
    disposition = "inline" if result.media_type == "image/png" else "attachment"
    return FileResponse(
        result.path,
        media_type=result.media_type,
        filename=filename,
        content_disposition_type=disposition,
        headers={
            "X-Moonli-Run-Id": result.run_id,
            "X-Moonli-Result-SHA256": result.sha256,
            "X-Idempotent-Replay": "true" if result.replayed else "false",
            "X-Moonli-Device-Id": device.device_id,
            "Cache-Control": "no-store",
        },
    )


@router.post("/v1/normalize")
async def normalize_pipeline_3(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_moonli_device_id: str | None = Header(default=None, alias="X-Moonli-Device-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PlainTextResponse:
    if not idempotency_key or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise MoonliError(
            "INVALID_INPUT",
            "Idempotency-Key is required and must contain 8-128 safe characters.",
            422,
        )
    authenticated, device = _authenticate_device(
        request,
        authorization,
        x_api_key,
        x_moonli_device_id,
        target_path="/v1/normalize",
    )
    settings: Settings = request.app.state.moonli_settings
    async with request.app.state.rate_limiter.limit(device.device_id):
        generation_input, fingerprint, pipeline = await _parse_input(request, settings)
        if pipeline != "pipeline-3" or generation_input.type != "audio":
            raise MoonliError(
                "INVALID_INPUT",
                "/v1/normalize requires a pipeline-3 audio request.",
                422,
            )
        _ensure_pipeline_credential(
            request,
            pipeline,
            provider_fields=("transcription_provider", "normalization_provider"),
        )
        request.app.state.production_usage_store.record_request(
            pipeline=pipeline, input_type="audio-normalization"
        )
        request_hash = hashlib.sha256(
            device.device_id.encode("ascii")
            + b"\0pipeline-3\0normalize\0"
            + fingerprint
        ).hexdigest()
        result = await request.app.state.pipeline3_service.normalize(
            generation_input=generation_input,
            idempotency_key=f"{device.device_id}:pipeline-3:normalize:{idempotency_key}",
            request_hash=request_hash,
        )
    audit_store = getattr(request.app.state, "audit_store", None)
    if audit_store is not None:
        audit_store.append(
            action="production.normalize",
            outcome="success",
            summary="Production transcription and normalization completed.",
            actor_type="device",
            actor_id=device.device_id,
            target_type="generation_run",
            target_id=result.run_id,
            request_id=getattr(request.state, "request_id", None),
            context={
                "pipeline": pipeline,
                "connection_type": device.connection_type,
                "client_credential_id": authenticated.client_id,
            },
        )
    return PlainTextResponse(
        result.path.read_text(encoding="utf-8"),
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        request.app.state.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
