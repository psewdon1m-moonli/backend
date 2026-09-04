from __future__ import annotations

import re
import unicodedata

from app.api.errors import MoonliError
from app.domain.inputs import GenerationInput, NormalizedText
from app.providers.errors import ProviderError
from app.providers.transcription.base import Transcriber

WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    return WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


class InputResolver:
    def __init__(self, transcriber: Transcriber, max_text_length: int) -> None:
        self._transcriber = transcriber
        self._max_text_length = max_text_length

    @property
    def transcriber_name(self) -> str:
        return self._transcriber.name

    async def resolve(self, generation_input: GenerationInput) -> NormalizedText:
        if generation_input.type == "text":
            if generation_input.text is None:
                raise MoonliError("INVALID_INPUT", "Text input is missing.", 422)
            normalized = normalize_text(generation_input.text.text)
            self._validate_text(normalized)
            return NormalizedText(text=normalized, source_type="text")

        if generation_input.audio is None:
            raise MoonliError("INVALID_INPUT", "Audio input is missing.", 422)
        try:
            transcription = await self._transcriber.transcribe(generation_input.audio)
        except ProviderError as exc:
            raise MoonliError("TRANSCRIPTION_FAILED", "Unable to transcribe the audio.", 502) from exc
        normalized = normalize_text(transcription)
        self._validate_text(normalized)
        return NormalizedText(text=normalized, source_type="audio", transcription=transcription)

    def _validate_text(self, text: str) -> None:
        if not text:
            raise MoonliError("INVALID_INPUT", "Text must not be empty.", 422)
        if len(text) > self._max_text_length:
            raise MoonliError("INVALID_INPUT", "Text exceeds the configured maximum length.", 413)
