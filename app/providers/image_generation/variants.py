from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Callable
from io import BytesIO
from typing import Protocol

import httpx
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from app.providers.credentials import (
    GoogleApiKeySource,
    resolve_google_api_key,
    validate_google_api_key_source,
)
from app.providers.errors import ProviderError
from app.providers.proxy import ProxyUrlSource, resolve_proxy_url

logger = logging.getLogger("moonli.google.image_variants")
VARIANT_COUNT = 3
TARGET_SIZE = (1024, 1024)


class ImageVariantGenerator(Protocol):
    name: str

    async def generate(self, prompt: str) -> tuple[bytes, bytes, bytes]: ...


def build_image_variant_request(
    prompt: str,
    system_instruction: str,
) -> dict[str, object]:
    return {
        "systemInstruction": {
            "parts": [{"text": system_instruction}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {
                "aspectRatio": "1:1",
                "imageSize": "1K",
            },
        },
    }


def normalize_jpeg(content: bytes) -> bytes:
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            image = ImageOps.fit(
                source.convert("RGB"),
                TARGET_SIZE,
                method=Image.Resampling.LANCZOS,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ProviderError("Google image response could not be decoded") from exc
    output = BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=95,
        subsampling=0,
        optimize=True,
        exif=b"",
    )
    return output.getvalue()


class GoogleImageVariantGenerator:
    name = "google"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: GoogleApiKeySource,
        model: str,
        timeout_seconds: float,
        system_instruction: str,
        usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
        proxy_url: ProxyUrlSource = None,
    ) -> None:
        validate_google_api_key_source(api_key)
        if not model or "/" in model or any(character in model for character in "\r\n"):
            raise ValueError("Invalid Google image model name")
        if not system_instruction.strip():
            raise ValueError("Image system instruction is required")
        self._endpoint = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._system_instruction = system_instruction
        self._usage_recorder = usage_recorder
        self._proxy_url = proxy_url

    async def _generate_one(self, prompt: str) -> bytes:
        payload = build_image_variant_request(prompt, self._system_instruction)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                proxy=resolve_proxy_url(self._proxy_url),
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._endpoint,
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
                        return normalize_jpeg(base64.b64decode(encoded, validate=True))
                    except ValueError as exc:
                        raise ProviderError(
                            "Google image response contained invalid base64"
                        ) from exc
        raise ProviderError("Google image response contained no inline image")

    async def generate(self, prompt: str) -> tuple[bytes, bytes, bytes]:
        images = list(await asyncio.gather(*(self._generate_one(prompt) for _ in range(3))))
        for _ in range(2):
            seen: set[str] = set()
            duplicates: list[int] = []
            for index, content in enumerate(images):
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen:
                    duplicates.append(index)
                else:
                    seen.add(digest)
            if not duplicates:
                return images[0], images[1], images[2]
            replacements = await asyncio.gather(
                *(self._generate_one(prompt) for _ in duplicates)
            )
            for index, content in zip(duplicates, replacements, strict=True):
                images[index] = content
        if len({hashlib.sha256(item).digest() for item in images}) != VARIANT_COUNT:
            raise ProviderError("Google returned duplicate image variants")
        return images[0], images[1], images[2]


class MockImageVariantGenerator:
    name = "mock"

    async def generate(self, prompt: str) -> tuple[bytes, bytes, bytes]:
        result: list[bytes] = []
        digest = hashlib.sha256(prompt.encode("utf-8")).digest()
        for index in range(1, VARIANT_COUNT + 1):
            background = (
                (digest[index] + index * 41) % 256,
                (digest[index + 3] + index * 67) % 256,
                (digest[index + 7] + index * 89) % 256,
            )
            image = Image.new("RGB", TARGET_SIZE, background)
            draw = ImageDraw.Draw(image)
            inset = 110 + index * 45
            draw.ellipse(
                (inset, inset, 1024 - inset, 1024 - inset),
                fill=(255 - background[0], 255 - background[1], 255 - background[2]),
                outline=(0, 0, 0),
                width=18,
            )
            output = BytesIO()
            image.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
            result.append(output.getvalue())
        return result[0], result[1], result[2]
