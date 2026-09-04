from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from app.api.errors import MoonliError
from app.api.lab_models import (
    GoogleOptions,
    ImageLabRequest,
    PaletteLabOptions,
    PaletteQuantizationLabOptions,
    PipelineLabOptions,
    PromptLabRequest,
    PromptNormalizationLabRequest,
    TranscriptionLabOptions,
    VectorLabOptions,
)
from app.api.routes.generate import AUDIO_ALIASES, IDEMPOTENCY_KEY
from app.domain.images import GeneratedImage
from app.domain.inputs import AudioInput, GenerationInput, TextInput
from app.domain.prompts import GenerationPrompt, VisualBrief
from app.providers.errors import NoVisualSubjectError, ProviderError
from app.providers.image_generation.google import GoogleImageGenerator
from app.providers.image_generation.mock import MockImageGenerator
from app.providers.image_generation.variants import (
    GoogleImageVariantGenerator,
    MockImageVariantGenerator,
)
from app.providers.prompt_normalization.google import (
    GooglePromptNormalizer,
)
from app.providers.prompt_normalization.mock import MockPromptNormalizer
from app.providers.prompt_translation import (
    GooglePromptTranslator,
    MockPromptTranslator,
)
from app.providers.prompt_translation.google import TRANSLATION_INSTRUCTION
from app.providers.proxy import ProxyUrlSource
from app.providers.transcription.google import GoogleTranscriber
from app.providers.transcription.mock import MockTranscriber
from app.services.generation_service import GenerationService
from app.services.input_resolver import InputResolver
from app.services.outputs import LayeredImageOutputBuilder, RuntimeValidator
from app.services.pipeline3 import Pipeline3Service
from app.services.processing.layers import LayerProcessor
from app.services.processing.palette_quantizer import (
    PaletteQuantizationError,
    PaletteQuantizer,
)
from app.services.processing.palette_validator import PaletteValidator
from app.services.processing.palette_vectorizer import (
    PaletteVectorizationError,
    PaletteVectorizer,
    segment_palette_svg,
)
from app.services.prompts import PromptBuilder
from app.services.run_archive import build_pipeline3_run_archive, build_run_archive
from app.settings import Settings
from app.storage.production_pipeline_config import (
    PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION,
    PIPELINE_3_NORMALIZATION_INSTRUCTION,
    PIPELINE_3_TRANSCRIPTION_INSTRUCTION,
)

router = APIRouter(prefix="/internal/test", include_in_schema=False)
GOOGLE_HOST = re.compile(r"(^|\.)googleapis\.com$", re.IGNORECASE)
ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}
IMAGE_SIZES = {"1K", "2K", "4K"}


def _routing_proxy(request: Request) -> ProxyUrlSource:
    store = getattr(request.app.state, "routing_config_store", None)
    return store.proxy_url if store is not None else None


def _authorize(
    request: Request, authorization: str | None, x_api_key: str | None
) -> tuple[Settings, str]:
    operator = request.app.state.operator_auth_store.authenticate_request(
        request,
        require_csrf=request.method not in {"GET", "HEAD", "OPTIONS"},
    )
    test_settings = request.app.state.server_settings_store.effective_settings()
    return test_settings, operator.actor_id


def _google_base_url(requested: str, settings: Settings) -> str:
    value = (requested or settings.google_api_base_url).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not GOOGLE_HOST.search(parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MoonliError(
            "INVALID_INPUT", "Google base URL must be an HTTPS googleapis.com endpoint.", 422
        )
    return value


def _google_key(header_value: str | None) -> str:
    value = (header_value or "").strip()
    if not value:
        raise MoonliError(
            "INVALID_INPUT",
            "A Google API key is required for a Google-provider test.",
            422,
        )
    return value


def _google_options(options: GoogleOptions, settings: Settings) -> tuple[str, float, str, str]:
    aspect_ratio = options.aspect_ratio.upper().replace(" ", "")
    image_size = options.image_size.upper().replace(" ", "")
    if aspect_ratio not in ASPECT_RATIOS:
        raise MoonliError("INVALID_INPUT", "Unsupported image aspect ratio.", 422)
    if image_size not in IMAGE_SIZES:
        raise MoonliError("INVALID_INPUT", "Image size must be 1K, 2K, or 4K.", 422)
    return (
        _google_base_url(options.google_base_url, settings),
        options.timeout_seconds,
        aspect_ratio,
        image_size,
    )


def _make_transcriber(
    options: GoogleOptions,
    provider: str,
    google_api_key: str | None,
    settings: Settings,
    instruction: str | None = None,
    proxy_url: ProxyUrlSource = None,
):
    if provider == "mock":
        return MockTranscriber()
    base_url, timeout, _, _ = _google_options(options, settings)
    model = (options.google_transcription_model or settings.google_transcription_model).strip()
    try:
        kwargs = {"instruction": instruction} if instruction is not None else {}
        return GoogleTranscriber(
            base_url,
            _google_key(google_api_key),
            model,
            timeout,
            proxy_url=proxy_url,
            **kwargs,
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc


def _make_image_generator(
    options: GoogleOptions,
    provider: str,
    google_api_key: str | None,
    settings: Settings,
    proxy_url: ProxyUrlSource = None,
):
    if provider == "mock":
        return MockImageGenerator()
    base_url, timeout, aspect_ratio, image_size = _google_options(options, settings)
    model = (options.google_image_model or settings.google_image_model).strip()
    try:
        return GoogleImageGenerator(
            base_url,
            _google_key(google_api_key),
            model,
            timeout,
            aspect_ratio,
            image_size,
            proxy_url=proxy_url,
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc


def _make_prompt_normalizer(
    options: GoogleOptions,
    provider: str,
    google_api_key: str | None,
    settings: Settings,
    instruction: str | None = None,
    output_language: str = "english",
    proxy_url: ProxyUrlSource = None,
):
    if provider == "mock":
        return MockPromptNormalizer()
    base_url, timeout, _, _ = _google_options(options, settings)
    model = (options.google_normalization_model or settings.google_normalization_model).strip()
    try:
        kwargs = {"instruction": instruction} if instruction is not None else {}
        return GooglePromptNormalizer(
            base_url,
            _google_key(google_api_key),
            model,
            timeout,
            output_language=output_language,
            proxy_url=proxy_url,
            **kwargs,
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc


def _make_prompt_translator(
    options: GoogleOptions,
    provider: str,
    google_api_key: str | None,
    settings: Settings,
    instruction: str,
    proxy_url: ProxyUrlSource = None,
):
    if provider == "mock":
        return MockPromptTranslator()
    base_url, timeout, _, _ = _google_options(options, settings)
    model = (
        options.google_translation_model or settings.google_translation_model
    ).strip()
    try:
        return GooglePromptTranslator(
            base_url=base_url,
            api_key=_google_key(google_api_key),
            model=model,
            timeout_seconds=timeout,
            instruction=instruction,
            proxy_url=proxy_url,
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc


def _make_image_variant_generator(
    options: GoogleOptions,
    provider: str,
    google_api_key: str | None,
    settings: Settings,
    system_instruction: str,
    proxy_url: ProxyUrlSource = None,
):
    if provider == "mock":
        return MockImageVariantGenerator()
    base_url, timeout, _, _ = _google_options(options, settings)
    model = (options.google_image_model or settings.google_image_model).strip()
    try:
        return GoogleImageVariantGenerator(
            base_url=base_url,
            api_key=_google_key(google_api_key),
            model=model,
            timeout_seconds=timeout,
            system_instruction=system_instruction,
            proxy_url=proxy_url,
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc


async def _form(request: Request, max_fields: int = 24) -> FormData:
    settings: Settings = request.app.state.moonli_settings
    try:
        return await request.form(
            max_files=1,
            max_fields=max_fields,
            max_part_size=settings.max_audio_size,
        )
    except Exception as exc:
        raise MoonliError("INVALID_INPUT", "Invalid multipart request.", 422) from exc


def _form_values(form: FormData) -> dict[str, str]:
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


async def _read_upload(upload: Any, limit: int, empty_message: str) -> tuple[bytes, str, str]:
    if not isinstance(upload, UploadFile):
        raise MoonliError("INVALID_INPUT", empty_message, 422)
    filename = Path(upload.filename or "upload.bin").name
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if not content_type or content_type == "application/octet-stream":
        content_type = (mimetypes.guess_type(filename)[0] or "application/octet-stream").lower()
    content = bytearray()
    try:
        while chunk := await upload.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > limit:
                raise MoonliError("INVALID_INPUT", "Uploaded file exceeds the configured limit.", 413)
    finally:
        await upload.close()
    if not content:
        raise MoonliError("INVALID_INPUT", empty_message, 422)
    return bytes(content), filename, content_type


async def _audio_from_form(form: FormData, settings: Settings) -> AudioInput:
    content, filename, content_type = await _read_upload(
        form.get("audio"), settings.max_audio_size, "Audio file is required."
    )
    content_type = AUDIO_ALIASES.get(content_type, content_type)
    if content_type not in settings.supported_audio_types:
        raise MoonliError("INVALID_INPUT", "Unsupported audio content type.", 415)
    return AudioInput(content=content, content_type=content_type, filename=filename)


async def _image_from_form(form: FormData, settings: Settings) -> tuple[bytes, str]:
    content, _, content_type = await _read_upload(
        form.get("image"), settings.max_audio_size, "PNG image is required."
    )
    if content_type != "image/png":
        raise MoonliError("INVALID_INPUT", "Only PNG images are accepted.", 415)
    return content, content_type


async def _vector_from_form(form: FormData, settings: Settings) -> bytes:
    content, _, content_type = await _read_upload(
        form.get("vector"), settings.max_audio_size, "SVG vector is required."
    )
    if content_type != "image/svg+xml":
        raise MoonliError("INVALID_INPUT", "Only SVG vectors are accepted.", 415)
    return content


def _model_from_form(model_type, form: FormData):
    try:
        return model_type.model_validate(_form_values(form))
    except ValidationError as exc:
        message = str(exc.errors()[0].get("msg", "Invalid test options"))
        raise MoonliError("INVALID_INPUT", message, 422) from exc


@router.get("/config")
def config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, object]:
    settings, _ = _authorize(request, authorization, x_api_key)
    profiles = request.app.state.pipeline_profiles
    server_configuration = request.app.state.server_settings_store.get()
    return {
        "service": "Moonli",
        "environment": settings.environment,
        "providers": {
            "image": settings.image_provider,
            "transcription": settings.transcription_provider,
            "normalization": settings.normalization_provider,
            "google_key_configured_on_server": False,
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
    }


@router.post("/normalize")
async def normalize_prompt(
    payload: PromptNormalizationLabRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_google_api_key: str | None = Header(default=None, alias="X-Google-API-Key"),
) -> dict[str, object]:
    settings, _ = _authorize(request, authorization, x_api_key)
    normalizer = _make_prompt_normalizer(
        payload,
        payload.provider,
        x_google_api_key,
        settings,
        proxy_url=_routing_proxy(request),
    )
    started = time.perf_counter()
    try:
        normalized = await normalizer.normalize(payload.text)
    except NoVisualSubjectError as exc:
        raise MoonliError(
            "NO_VISUAL_SUBJECT", "Say what you want to draw.", 422
        ) from exc
    except ProviderError as exc:
        raise MoonliError(
            "PROMPT_NORMALIZATION_FAILED", str(exc), 502
        ) from exc
    return {
        "provider": normalizer.name,
        "original_text": payload.text,
        "normalized_text": normalized,
        "word_count": len(normalized.split()),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@router.post("/prompt")
def build_prompt(
    payload: PromptLabRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, object]:
    _authorize(request, authorization, x_api_key)
    profile = request.app.state.pipeline_profiles[payload.pipeline]
    try:
        result = PromptBuilder(template=payload.prompt_template or None).build(payload.text, profile)
    except ValueError as exc:
        raise MoonliError("PROMPT_BUILD_FAILED", str(exc), 422) from exc
    return {
        "pipeline": profile.id,
        "template_version": result.template_version,
        "visual_brief": result.brief.as_dict(),
        "prompt": result.text,
        "character_count": len(result.text),
    }


@router.post("/transcribe")
async def transcribe(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_google_api_key: str | None = Header(default=None, alias="X-Google-API-Key"),
) -> dict[str, object]:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request)
    options: TranscriptionLabOptions = _model_from_form(TranscriptionLabOptions, form)
    audio = await _audio_from_form(form, settings)
    transcriber = _make_transcriber(
        options,
        options.provider,
        x_google_api_key,
        settings,
        proxy_url=_routing_proxy(request),
    )
    started = time.perf_counter()
    try:
        text = await transcriber.transcribe(audio)
    except ProviderError as exc:
        raise MoonliError("TRANSCRIPTION_FAILED", "Transcription provider request failed.", 502) from exc
    return {
        "provider": transcriber.name,
        "transcription": text,
        "source": {
            "filename": audio.filename,
            "content_type": audio.content_type,
            "bytes": len(audio.content),
        },
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@router.post("/image")
async def generate_image(
    payload: ImageLabRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_google_api_key: str | None = Header(default=None, alias="X-Google-API-Key"),
) -> Response:
    settings, _ = _authorize(request, authorization, x_api_key)
    profile = request.app.state.pipeline_profiles[payload.pipeline]
    generator = _make_image_generator(
        payload,
        payload.provider,
        x_google_api_key,
        settings,
        proxy_url=_routing_proxy(request),
    )
    prompt = GenerationPrompt(
        text=payload.prompt,
        template_version="lab_raw_v1",
        brief=VisualBrief(payload.prompt[:600], (), (), (), False),
    )
    started = time.perf_counter()
    try:
        generated = await generator.generate(prompt, profile.palette, profile, payload.attempt)
    except ProviderError as exc:
        raise MoonliError("IMAGE_GENERATION_FAILED", "Image provider request failed.", 502) from exc
    headers = {
        "Content-Disposition": 'inline; filename="moonli-lab.png"',
        "X-Moonli-Provider": generator.name,
        "X-Moonli-Duration-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
    }
    if payload.validate_palette:
        check = PaletteValidator(payload.snap_distance).validate(generated, profile.palette, profile)
        headers.update(
            {
                "X-Moonli-Palette-Valid": str(check.valid).lower(),
                "X-Moonli-Invalid-Pixels": str(check.invalid_pixels),
                "X-Moonli-Invalid-Colors": ",".join(check.invalid_colors),
                "X-Moonli-Snapped-Pixels": str(
                    check.image.snapped_pixels if check.image is not None else 0
                ),
            }
        )
    return Response(content=generated.content, media_type=generated.media_type, headers=headers)


@router.post("/palette")
async def validate_palette(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, object]:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request, max_fields=4)
    options: PaletteLabOptions = _model_from_form(PaletteLabOptions, form)
    content, content_type = await _image_from_form(form, settings)
    profile = request.app.state.pipeline_profiles[options.pipeline]
    result = PaletteValidator(options.snap_distance).validate(
        GeneratedImage(content, content_type, "upload"), profile.palette, profile
    )
    return {
        "pipeline": profile.id,
        "palette_version": profile.palette.id,
        "valid": result.valid,
        "invalid_pixels": result.invalid_pixels,
        "invalid_colors": list(result.invalid_colors),
        "reason": result.reason,
        "snapped_pixels": result.snapped_pixels,
        "opaque_pixels": result.opaque_pixels,
    }


@router.post("/quantize")
async def quantize_palette(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request, max_fields=4)
    options: PaletteQuantizationLabOptions = _model_from_form(
        PaletteQuantizationLabOptions, form
    )
    content, content_type = await _image_from_form(form, settings)
    profile = request.app.state.pipeline_profiles[options.pipeline]
    started = time.perf_counter()
    try:
        result = PaletteQuantizer(options.cleanup_passes).quantize(
            GeneratedImage(content, content_type, "upload"), profile.palette, profile
        )
    except PaletteQuantizationError as exc:
        raise MoonliError("PALETTE_QUANTIZATION_FAILED", str(exc), 422) from exc
    return Response(
        content=result.image.content,
        media_type="image/png",
        headers={
            "Content-Disposition": 'inline; filename="moonli-quantized.png"',
            "X-Moonli-Quantized": "true",
            "X-Moonli-Changed-Pixels": str(result.changed_pixels),
            "X-Moonli-Cleanup-Changed-Pixels": str(result.cleanup_changed_pixels),
            "X-Moonli-Cleanup-Removed-Components": str(
                result.cleanup_removed_components
            ),
            "X-Moonli-Unique-Colors-Before": str(result.unique_colors_before),
            "X-Moonli-Unique-Colors-After": str(result.unique_colors_after),
            "X-Moonli-Opaque-Pixels": str(result.opaque_pixels),
            "X-Moonli-Duration-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
        },
    )


@router.post("/vectorize")
async def vectorize_palette_image(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request, max_fields=3)
    options: VectorLabOptions = _model_from_form(VectorLabOptions, form)
    content, content_type = await _image_from_form(form, settings)
    profile = request.app.state.pipeline_profiles[options.pipeline]
    validation = PaletteValidator(0).validate(
        GeneratedImage(content, content_type, "upload"), profile.palette, profile
    )
    if not validation.valid or validation.image is None:
        raise MoonliError(
            "PALETTE_VALIDATION_FAILED",
            "Vectorization requires a strictly palette-valid PNG.",
            422,
        )
    started = time.perf_counter()
    try:
        result = PaletteVectorizer().vectorize(validation.image, profile)
    except PaletteVectorizationError as exc:
        raise MoonliError("VECTORIZATION_FAILED", str(exc), 422) from exc
    return Response(
        content=result.content,
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": 'inline; filename="moonli-vector.svg"',
            "X-Moonli-Vectorized": "true",
            "X-Moonli-Vector-Runs": str(result.run_count),
            "X-Moonli-Used-Colors": str(result.used_colors),
            "X-Moonli-Opaque-Pixels": str(result.opaque_pixels),
            "X-Moonli-Duration-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
        },
    )


@router.post("/segment")
async def segment_palette_vector(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request, max_fields=3)
    options: VectorLabOptions = _model_from_form(VectorLabOptions, form)
    content = await _vector_from_form(form, settings)
    profile = request.app.state.pipeline_profiles[options.pipeline]
    started = time.perf_counter()
    try:
        result = segment_palette_svg(content, profile)
    except PaletteVectorizationError as exc:
        raise MoonliError("SEGMENTATION_FAILED", str(exc), 422) from exc
    return Response(
        content=result.content,
        media_type="application/vnd.moonli.vector-layers+zip",
        headers={
            "Content-Disposition": 'attachment; filename="moonli-vector-layers.zip"',
            "X-Moonli-Segmented": "true",
            "X-Moonli-Used-Layers": str(result.used_layers),
            "X-Moonli-Total-Layers": str(result.total_layers),
            "X-Moonli-Result-SHA256": hashlib.sha256(result.content).hexdigest(),
            "X-Moonli-Duration-Ms": str(round((time.perf_counter() - started) * 1000, 2)),
        },
    )


@router.post("/package")
async def build_package(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    settings, _ = _authorize(request, authorization, x_api_key)
    form = await _form(request, max_fields=4)
    options: PaletteLabOptions = _model_from_form(PaletteLabOptions, form)
    if options.pipeline != "pipeline-2":
        raise MoonliError("INVALID_INPUT", "Layer packaging is available only for pipeline-2.", 422)
    content, content_type = await _image_from_form(form, settings)
    profile = request.app.state.pipeline_profiles[options.pipeline]
    result = PaletteValidator(options.snap_distance).validate(
        GeneratedImage(content, content_type, "upload"), profile.palette, profile
    )
    if not result.valid or result.image is None:
        raise MoonliError(
            "PALETTE_VALIDATION_FAILED",
            result.reason or "Uploaded image does not match the selected palette.",
            422,
        )
    with tempfile.TemporaryDirectory(prefix="moonli-lab-") as raw_dir:
        run_dir = Path(raw_dir)
        built = LayeredImageOutputBuilder(LayerProcessor(), RuntimeValidator()).build(
            result.image, profile, f"lab_{uuid.uuid4().hex}", run_dir
        )
        package = built.path.read_bytes()
    return Response(
        content=package,
        media_type="application/vnd.moonli.layers+zip",
        headers={
            "Content-Disposition": 'attachment; filename="moonli-lab-layers.zip"',
            "X-Moonli-Result-SHA256": hashlib.sha256(package).hexdigest(),
        },
    )


@router.post("/pipeline")
async def run_pipeline(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_google_api_key: str | None = Header(default=None, alias="X-Google-API-Key"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    settings, client_id = _authorize(request, authorization, x_api_key)
    if not idempotency_key or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise MoonliError(
            "INVALID_INPUT", "Idempotency-Key must contain 8-128 safe characters.", 422
        )
    form = await _form(request)
    options: PipelineLabOptions = _model_from_form(PipelineLabOptions, form)
    if options.type == "text":
        if not options.text:
            raise MoonliError("INVALID_INPUT", "Text is required for a text pipeline run.", 422)
        generation_input = GenerationInput(type="text", text=TextInput(options.text))
        input_fingerprint = hashlib.sha256(options.text.encode("utf-8")).hexdigest()
        transcriber = MockTranscriber()
    else:
        audio = await _audio_from_form(form, settings)
        generation_input = GenerationInput(type="audio", audio=audio)
        input_fingerprint = hashlib.sha256(audio.content).hexdigest()

    if options.pipeline == "pipeline-3":
        if generation_input.type == "audio":
            transcriber = _make_transcriber(
                options,
                options.transcription_provider,
                x_google_api_key,
                settings,
                PIPELINE_3_TRANSCRIPTION_INSTRUCTION,
                proxy_url=_routing_proxy(request),
            )
        else:
            transcriber = MockTranscriber()
        transcription_provider_name = options.transcription_provider
        normalizer = _make_prompt_normalizer(
            options,
            options.normalization_provider,
            x_google_api_key,
            settings,
            PIPELINE_3_NORMALIZATION_INSTRUCTION,
            output_language="russian",
            proxy_url=_routing_proxy(request),
        )
        translator = _make_prompt_translator(
            options,
            options.normalization_provider,
            x_google_api_key,
            settings,
            TRANSLATION_INSTRUCTION,
            proxy_url=_routing_proxy(request),
        )
        generator = _make_image_variant_generator(
            options,
            options.image_provider,
            x_google_api_key,
            settings,
            PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION,
            proxy_url=_routing_proxy(request),
        )
        service = Pipeline3Service(
            input_resolver=InputResolver(transcriber, settings.max_text_length),
            prompt_normalizer=normalizer,
            prompt_translator=translator,
            image_generator=generator,
            artifact_store=request.app.state.artifact_store,
            run_repository=request.app.state.run_repository,
            metrics=request.app.state.metrics,
        )
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "pipeline": "pipeline-3",
                    "input": input_fingerprint,
                    "options": options.model_dump(exclude={"text"}),
                },
                sort_keys=True,
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        async with request.app.state.rate_limiter.limit(client_id):
            result = await service.full_run(
                generation_input=generation_input,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        trace = request.app.state.run_repository.get_artifact_trace(result.run_id)
        archive = build_pipeline3_run_archive(
            run_id=result.run_id,
            completed_dir=result.path.parent,
            generation_input=generation_input,
            trace=trace,
            image_provider=generator.name,
            transcription_provider=transcription_provider_name,
            normalization_provider=normalizer.name,
            translation_provider=translator.name,
        )
        return Response(
            content=archive.content,
            media_type=archive.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{archive.filename}"',
                "X-Moonli-Run-Id": result.run_id,
                "X-Moonli-Result-SHA256": archive.sha256,
                "X-Moonli-Final-Output-SHA256": result.sha256,
                "X-Moonli-Archive-Contract": "moonli-run-artifacts.v1",
                "X-Moonli-Pipeline": "pipeline-3",
                "X-Moonli-Input-Type": generation_input.type,
                "X-Idempotent-Replay": str(result.replayed).lower(),
                "X-Moonli-Image-Provider": generator.name,
                "X-Moonli-Transcription-Provider": transcription_provider_name,
                "X-Moonli-Normalization-Provider": normalizer.name,
                "X-Moonli-Translation-Provider": translator.name,
                "X-Moonli-Palette-Quantized": "false",
                "Cache-Control": "no-store",
            },
        )

    profile = request.app.state.pipeline_profiles[options.pipeline]
    if options.type == "audio":
        transcriber = _make_transcriber(
            options,
            options.transcription_provider,
            x_google_api_key,
            settings,
            proxy_url=_routing_proxy(request),
        )
    generator = _make_image_generator(
        options,
        options.image_provider,
        x_google_api_key,
        settings,
        proxy_url=_routing_proxy(request),
    )
    normalizer = _make_prompt_normalizer(
        options,
        options.normalization_provider,
        x_google_api_key,
        settings,
        proxy_url=_routing_proxy(request),
    )
    service = GenerationService(
        input_resolver=InputResolver(transcriber, settings.max_text_length),
        prompt_normalizer=normalizer,
        prompt_builder=PromptBuilder(template=options.prompt_template or None),
        image_generator=generator,
        palette_quantizer=PaletteQuantizer(options.quantization_cleanup_passes),
        palette_validator=PaletteValidator(0),
        artifact_store=request.app.state.artifact_store,
        run_repository=request.app.state.run_repository,
        runtime_validator=RuntimeValidator(),
        metrics=request.app.state.metrics,
        generation_attempts=options.generation_attempts,
    )
    public_options = options.model_dump(exclude={"text"})
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "pipeline": options.pipeline,
                "input": input_fingerprint,
                "options": public_options,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    async with request.app.state.rate_limiter.limit(client_id):
        result = await service.generate(
            generation_input=generation_input,
            profile=profile,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    trace = request.app.state.run_repository.get_artifact_trace(result.run_id)
    archive = build_run_archive(
        run_id=result.run_id,
        completed_dir=result.path.parent,
        profile=profile,
        generation_input=generation_input,
        trace=trace,
        image_provider=generator.name,
        transcription_provider=transcriber.name,
        normalization_provider=normalizer.name,
        final_media_type=result.media_type,
    )
    return Response(
        content=archive.content,
        media_type=archive.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{archive.filename}"',
            "X-Moonli-Run-Id": result.run_id,
            "X-Moonli-Result-SHA256": archive.sha256,
            "X-Moonli-Final-Output-SHA256": result.sha256,
            "X-Moonli-Archive-Contract": "moonli-run-artifacts.v1",
            "X-Moonli-Pipeline": profile.id,
            "X-Moonli-Input-Type": generation_input.type,
            "X-Idempotent-Replay": str(result.replayed).lower(),
            "X-Moonli-Image-Provider": generator.name,
            "X-Moonli-Transcription-Provider": transcriber.name,
            "X-Moonli-Normalization-Provider": normalizer.name,
            "X-Moonli-Palette-Quantized": "true",
            "Cache-Control": "no-store",
        },
    )
