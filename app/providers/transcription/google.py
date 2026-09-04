from __future__ import annotations

import base64
import logging
import re
from collections.abc import Callable

import httpx

from app.domain.inputs import AudioInput
from app.providers.credentials import (
    GoogleApiKeySource,
    resolve_google_api_key,
    validate_google_api_key_source,
)
from app.providers.errors import ProviderError
from app.providers.proxy import ProxyUrlSource, resolve_proxy_url

MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
TRANSCRIPTION_INSTRUCTION = (
    "Transcribe this audio faithfully as normal human text. "
    "Return only the transcription, without commentary or prompt rewriting."
)
logger = logging.getLogger("moonli.google.transcription")


def build_transcription_request(
    mime_type: str,
    encoded_audio: str,
    instruction: str = TRANSCRIPTION_INSTRUCTION,
) -> dict[str, object]:
    return {
        "contents": [
            {
                "parts": [
                    {"text": instruction},
                    {"inlineData": {"mimeType": mime_type, "data": encoded_audio}},
                ]
            }
        ]
    }


class GoogleTranscriber:
    name = "google"

    def __init__(
        self,
        base_url: str,
        api_key: GoogleApiKeySource,
        model: str,
        timeout_seconds: float,
        usage_recorder: Callable[[str, dict[str, object]], None] | None = None,
        instruction: str = TRANSCRIPTION_INSTRUCTION,
        proxy_url: ProxyUrlSource = None,
    ) -> None:
        validate_google_api_key_source(api_key)
        if not model:
            raise ValueError("Google transcription model is required")
        if not MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid Google transcription model name")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._usage_recorder = usage_recorder
        self._instruction = instruction
        self._proxy_url = proxy_url

    async def transcribe(self, audio: AudioInput) -> str:
        endpoint = f"{self._base_url}/models/{self._model}:generateContent"
        payload = build_transcription_request(
            audio.content_type,
            base64.b64encode(audio.content).decode("ascii"),
            self._instruction,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                proxy=resolve_proxy_url(self._proxy_url),
                trust_env=False,
            ) as client:
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
            raise ProviderError("Google transcription request failed") from exc

        if self._usage_recorder is not None:
            try:
                self._usage_recorder("transcription", body)
            except Exception:
                logger.exception("Unable to persist Google transcription token usage")

        texts: list[str] = []
        for candidate in body.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        transcription = "\n".join(texts).strip()
        if not transcription:
            raise ProviderError("Google transcription response contained no text")
        return transcription
