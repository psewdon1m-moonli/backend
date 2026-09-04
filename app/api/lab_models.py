from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.models import PipelineTag, ProcessedPipelineTag

ProviderName = Literal["mock", "google"]


class LabModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptLabRequest(LabModel):
    pipeline: ProcessedPipelineTag
    text: str = Field(min_length=1, max_length=12000)
    prompt_template: str | None = Field(default=None, max_length=30000)

    @field_validator("text", "prompt_template")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class GoogleOptions(LabModel):
    google_base_url: str = Field(default="", max_length=500)
    google_image_model: str = Field(default="", max_length=160)
    google_transcription_model: str = Field(default="", max_length=160)
    google_normalization_model: str = Field(default="", max_length=160)
    google_translation_model: str = Field(
        default="gemini-2.5-flash", max_length=160
    )
    timeout_seconds: float = Field(default=180, ge=1, le=300)
    aspect_ratio: str = Field(default="1:1", max_length=16)
    image_size: str = Field(default="1K", max_length=8)


class TranscriptionLabOptions(GoogleOptions):
    provider: ProviderName = "mock"


class PromptNormalizationLabRequest(GoogleOptions):
    provider: ProviderName = "mock"
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def strip_normalization_text(cls, value: str) -> str:
        return value.strip()


class ImageLabRequest(GoogleOptions):
    pipeline: ProcessedPipelineTag
    provider: ProviderName = "mock"
    prompt: str = Field(min_length=1, max_length=50000)
    attempt: int = Field(default=1, ge=1, le=5)
    validate_palette: bool = True
    snap_distance: float = Field(default=12, ge=0, le=100)

    @field_validator("prompt")
    @classmethod
    def strip_prompt(cls, value: str) -> str:
        return value.strip()


class PaletteLabOptions(LabModel):
    pipeline: ProcessedPipelineTag
    snap_distance: float = Field(default=12, ge=0, le=100)


class PaletteQuantizationLabOptions(LabModel):
    pipeline: ProcessedPipelineTag
    cleanup_passes: int = Field(default=1, ge=0, le=3)


class VectorLabOptions(LabModel):
    pipeline: ProcessedPipelineTag


class PipelineLabOptions(GoogleOptions):
    type: Literal["text", "audio"]
    pipeline: PipelineTag
    text: str = Field(default="", max_length=12000)
    image_provider: ProviderName = "mock"
    transcription_provider: ProviderName = "mock"
    normalization_provider: ProviderName = "mock"
    prompt_template: str | None = Field(default=None, max_length=30000)
    snap_distance: float = Field(default=12, ge=0, le=100)
    quantization_cleanup_passes: int = Field(default=1, ge=0, le=3)
    generation_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("text", "prompt_template")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
