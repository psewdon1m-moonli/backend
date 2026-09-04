from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InputType = Literal["text", "audio"]


@dataclass(frozen=True)
class TextInput:
    text: str


@dataclass(frozen=True)
class AudioInput:
    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class GenerationInput:
    type: InputType
    text: TextInput | None = None
    audio: AudioInput | None = None


@dataclass(frozen=True)
class NormalizedText:
    text: str
    source_type: InputType
    transcription: str | None = None
