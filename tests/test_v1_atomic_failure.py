from __future__ import annotations

import asyncio
import uuid
from io import BytesIO

import pytest
from PIL import Image

from app.api.errors import MoonliError
from app.domain.images import GeneratedImage
from app.domain.inputs import GenerationInput, TextInput
from app.domain.profiles import Palette, PipelineProfile
from app.providers.prompt_normalization.mock import MockPromptNormalizer
from app.providers.transcription.mock import MockTranscriber
from app.services.generation_service import GenerationService
from app.services.input_resolver import InputResolver
from app.services.outputs import RuntimeValidator
from app.services.processing.palette_quantizer import PaletteQuantizationResult
from app.services.processing.palette_validator import PaletteValidator
from app.services.prompts import PromptBuilder
from app.storage.artifact_store import LocalArtifactStore
from app.storage.run_repository import SqliteRunRepository
from app.telemetry import MetricsRegistry


class InvalidColorGenerator:
    name = "test-invalid"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt, palette, profile, attempt) -> GeneratedImage:
        self.calls += 1
        image = Image.new("RGBA", (profile.width, profile.height), (0, 0, 255, 255))
        output = BytesIO()
        image.save(output, format="PNG")
        return GeneratedImage(output.getvalue(), "image/png", self.name)


class BrokenQuantizer:
    """Test double proving strict validation still protects the publish boundary."""

    def quantize(self, generated, palette, profile) -> PaletteQuantizationResult:
        return PaletteQuantizationResult(
            image=generated,
            changed_pixels=0,
            cleanup_changed_pixels=0,
            opaque_pixels=profile.width * profile.height,
            transparent_pixels=0,
            unique_colors_before=1,
            unique_colors_after=1,
            palette_counts=(0,),
        )


def test_palette_failure_retries_and_never_publishes_partial_result(tmp_path) -> None:
    profile = PipelineProfile(
        id="pipeline-1",
        output_mode="full_image",
        palette=Palette("test_v1", 1, ("#FF0000",)),
        width=8,
        height=8,
        visual_constraints=(),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    generator = InvalidColorGenerator()
    service = GenerationService(
        input_resolver=InputResolver(MockTranscriber(), 1000),
        prompt_normalizer=MockPromptNormalizer(),
        prompt_builder=PromptBuilder(),
        image_generator=generator,
        palette_quantizer=BrokenQuantizer(),
        palette_validator=PaletteValidator(3),
        artifact_store=store,
        run_repository=SqliteRunRepository(tmp_path / "runs.sqlite3"),
        runtime_validator=RuntimeValidator(),
        metrics=MetricsRegistry(),
        generation_attempts=2,
    )
    with pytest.raises(MoonliError, match="allowed palette") as captured:
        asyncio.run(
            service.generate(
                GenerationInput(type="text", text=TextInput("red moon")),
                profile,
                f"test-{uuid.uuid4().hex}",
                "request-hash",
            )
        )
    assert captured.value.code == "PALETTE_VALIDATION_FAILED"
    assert generator.calls == 2
    assert list(store.completed_root.iterdir()) == []
