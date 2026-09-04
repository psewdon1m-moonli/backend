from __future__ import annotations

from typing import Protocol

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.domain.prompts import GenerationPrompt


class ImageGenerator(Protocol):
    name: str

    async def generate(
        self,
        prompt: GenerationPrompt,
        palette: Palette,
        profile: PipelineProfile,
        attempt: int,
    ) -> GeneratedImage: ...
