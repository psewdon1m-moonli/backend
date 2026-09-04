from __future__ import annotations

from collections.abc import Callable

from app.providers.credentials import GoogleApiKeySource
from app.providers.image_generation import (
    GoogleImageGenerator,
    ImageGenerator,
    MockImageGenerator,
)
from app.providers.prompt_normalization import (
    GooglePromptNormalizer,
    MockPromptNormalizer,
    PromptNormalizer,
)
from app.providers.proxy import ProxyUrlSource
from app.providers.transcription import GoogleTranscriber, MockTranscriber, Transcriber
from app.settings import Settings


def create_image_generator(
    settings: Settings,
    api_key: GoogleApiKeySource | None = None,
    usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
    proxy_url: ProxyUrlSource = None,
) -> ImageGenerator:
    if settings.image_provider == "mock":
        return MockImageGenerator()
    return GoogleImageGenerator(
        base_url=settings.google_api_base_url,
        api_key=api_key or settings.google_api_key,
        model=settings.google_image_model,
        timeout_seconds=settings.google_timeout_seconds,
        aspect_ratio=settings.google_image_aspect_ratio,
        image_size=settings.google_image_size,
        usage_recorder=usage_recorder,
        proxy_url=proxy_url,
    )


def create_transcriber(
    settings: Settings,
    api_key: GoogleApiKeySource | None = None,
    usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
    instruction: str | None = None,
    proxy_url: ProxyUrlSource = None,
) -> Transcriber:
    if settings.transcription_provider == "mock":
        return MockTranscriber()
    return GoogleTranscriber(
        base_url=settings.google_api_base_url,
        api_key=api_key or settings.google_api_key,
        model=settings.google_transcription_model,
        timeout_seconds=settings.google_timeout_seconds,
        usage_recorder=usage_recorder,
        proxy_url=proxy_url,
        **({"instruction": instruction} if instruction is not None else {}),
    )


def create_prompt_normalizer(
    settings: Settings,
    api_key: GoogleApiKeySource | None = None,
    usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
    instruction: str | None = None,
    output_language: str = "english",
    proxy_url: ProxyUrlSource = None,
) -> PromptNormalizer:
    if settings.normalization_provider == "mock":
        return MockPromptNormalizer()
    return GooglePromptNormalizer(
        base_url=settings.google_api_base_url,
        api_key=api_key or settings.google_api_key,
        model=settings.google_normalization_model,
        timeout_seconds=settings.google_timeout_seconds,
        usage_recorder=usage_recorder,
        output_language=output_language,
        proxy_url=proxy_url,
        **({"instruction": instruction} if instruction is not None else {}),
    )
