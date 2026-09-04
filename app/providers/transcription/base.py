from __future__ import annotations

from typing import Protocol

from app.domain.inputs import AudioInput


class Transcriber(Protocol):
    name: str

    async def transcribe(self, audio: AudioInput) -> str: ...
