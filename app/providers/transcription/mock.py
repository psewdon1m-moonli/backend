from __future__ import annotations

from app.domain.inputs import AudioInput


class MockTranscriber:
    name = "mock"

    async def transcribe(self, audio: AudioInput) -> str:
        return "A calm moonlit landscape made from simple, clean color shapes"
