from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

JobMode = Literal["generate", "library"]
GeneratorBackend = Literal["mock", "comfyui", "nano_banana_pro"]
JobStatus = Literal["created", "candidates_ready", "composition_selected", "palette_selected", "packed", "published", "failed"]
SessionMode = Literal["library", "generate"]
SessionStatus = Literal[
    "created",
    "mode_selected",
    "library_selected",
    "prompt_submitted",
    "candidates_generated",
    "candidate_selected",
    "vectorized",
    "segmented",
    "palette_variants_generated",
    "variant_selected",
    "ready_for_runtime",
    "failed",
]


class GenerationRequest(BaseModel):
    mode: JobMode = "generate"
    generator_backend: GeneratorBackend = "mock"
    theme: str = Field(min_length=1, max_length=200)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    max_colors: int = Field(default=6, ge=1, le=6)
    table_profile_id: str = "table_default"
    session_profile_id: str = "session_default"


class CandidateImage(BaseModel):
    candidate_id: str
    uri: str
    score: float = Field(ge=0.0, le=1.0)


class CandidateShortlist(BaseModel):
    items: list[CandidateImage]


class SelectCompositionRequest(BaseModel):
    candidate_id: str


class SelectPaletteRequest(BaseModel):
    colors: list[str] = Field(min_length=1, max_length=6)

    @field_validator("colors")
    @classmethod
    def validate_colors(cls, colors: list[str]) -> list[str]:
        seen: set[str] = set()
        for color in colors:
            if len(color) != 7 or not color.startswith("#"):
                raise ValueError("Each color must be in #RRGGBB format")
            try:
                int(color[1:], 16)
            except ValueError as exc:
                raise ValueError("Each color must be in #RRGGBB format") from exc
            norm = color.upper()
            if norm in seen:
                raise ValueError("Palette must not contain duplicate colors")
            seen.add(norm)
        return [color.upper() for color in colors]


class PackBuildResponse(BaseModel):
    pack_id: str
    pack_path: str
    manifest_path: str
    status: Literal["validated"]
    validation: ValidationReport


class PublishPackRequest(BaseModel):
    destination: Literal["runtime_cache", "library"] = "runtime_cache"


class PublishPackResponse(BaseModel):
    pack_id: str
    destination: str
    published_path: str


class LibraryItem(BaseModel):
    pack_id: str
    theme: str
    difficulty: Literal["easy", "medium", "hard"] | str
    colors: list[str] = Field(default_factory=list)
    manifest_path: str
    preview_uri: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    schema_name: str
    errors: list[str] = Field(default_factory=list)


class SessionRecord(BaseModel):
    session_id: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    mode: SessionMode | None = None
    user_prompt: str | None = None
    min_colors: int = 2
    max_colors: int = 6
    candidates: list[CandidateImage] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    selected_candidate_uri: str | None = None
    prepared_image_path: str | None = None
    vector_preview_uri: str | None = None
    palette_variants: list[dict[str, str]] = Field(default_factory=list)
    selected_pack_id: str | None = None
    runtime_pack_path: str | None = None
    error: str | None = None

    @classmethod
    def new(cls, session_id: str) -> SessionRecord:
        now = datetime.now(UTC)
        return cls(
            session_id=session_id,
            status="created",
            created_at=now,
            updated_at=now,
        )


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    request: GenerationRequest
    shortlist: list[CandidateImage] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    selected_candidate_uri: str | None = None
    selected_palette: list[str] = Field(default_factory=list)
    pack_id: str | None = None
    error: str | None = None

    @classmethod
    def new(cls, job_id: str, request: GenerationRequest) -> JobRecord:
        now = datetime.now(UTC)
        return cls(
            job_id=job_id,
            status="created",
            created_at=now,
            updated_at=now,
            request=request,
        )
