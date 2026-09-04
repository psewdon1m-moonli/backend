from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable
from io import BytesIO

import httpx
from PIL import Image, UnidentifiedImageError

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.domain.prompts import GenerationPrompt
from app.providers.credentials import (
    GoogleApiKeySource,
    resolve_google_api_key,
    validate_google_api_key_source,
)
from app.providers.errors import ProviderError

MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
FLAT_RASTER_INSTRUCTION = """

Critical production constraints:
- Build the illustration from large connected regions of solid flat color.
- Use hard clean boundaries and uniform fills.
- Do not use gradients, lighting, shadows, glow, texture, grain, stippling,
  scattered dots, painterly detail, or photographic effects.
- Avoid tiny isolated marks and near-duplicate colors; prefer fewer, larger shapes.
These constraints are required because the result will be palette-quantized and
converted into separate vector layers.
"""
logger = logging.getLogger("moonli.google.image_generation")


def build_image_generation_request(
    prompt_text: str, aspect_ratio: str, image_size: str, attempt: int = 1
) -> dict[str, object]:
    retry_instruction = ""
    if attempt > 1:
        retry_instruction = (
            "\nA previous result violated the exact palette. Be especially strict: "
            "no blended, anti-aliased, gradient, shaded, or additional RGB colors."
        )
    return {
        "contents": [
            {
                "parts": [
                    {"text": prompt_text + FLAT_RASTER_INSTRUCTION + retry_instruction}
                ]
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size,
            },
        },
    }


def _normalize_to_png(content: bytes) -> bytes:
    """Return a decoded Google image as a real PNG, preserving alpha when present."""
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            if source.format == "PNG":
                return content
            converted = source.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ProviderError("Google image response could not be decoded") from exc

    output = BytesIO()
    converted.save(output, format="PNG", optimize=False)
    return output.getvalue()


class GoogleImageGenerator:
    name = "google"

    def __init__(
        self,
        base_url: str,
        api_key: GoogleApiKeySource,
        model: str,
        timeout_seconds: float,
        aspect_ratio: str,
        image_size: str,
        usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        validate_google_api_key_source(api_key)
        if not model:
            raise ValueError("Google image model is required")
        if not MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid Google image model name")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._aspect_ratio = aspect_ratio
        self._image_size = image_size
        self._usage_recorder = usage_recorder

    async def generate(
        self,
        prompt: GenerationPrompt,
        palette: Palette,
        profile: PipelineProfile,
        attempt: int,
    ) -> GeneratedImage:
        endpoint = f"{self._base_url}/models/{self._model}:generateContent"
        payload = build_image_generation_request(
            prompt.text, self._aspect_ratio, self._image_size, attempt
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": resolve_google_api_key(self._api_key),
                    },
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("Google image generation request failed") from exc

        if self._usage_recorder is not None:
            try:
                self._usage_recorder("image_generation", body)
            except Exception:
                logger.exception("Unable to persist Google image-generation token usage")

        for candidate in body.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                encoded = inline.get("data")
                if isinstance(encoded, str) and encoded:
                    try:
                        content = base64.b64decode(encoded, validate=True)
                        return GeneratedImage(
                            content=_normalize_to_png(content),
                            media_type="image/png",
                            provider=self.name,
                        )
                    except ValueError as exc:
                        raise ProviderError("Google image response contained invalid base64") from exc
        raise ProviderError("Google image response contained no inline image")
