from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from app.providers.prompt_normalization.google import (
    INSTRUCTION as NORMALIZATION_INSTRUCTION,
)
from app.providers.transcription.google import TRANSCRIPTION_INSTRUCTION
from app.services.prompts import PromptBuilder
from app.settings import Settings

PIPELINE_IDS = ("pipeline-1", "pipeline-2", "pipeline-3")
MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

PIPELINE_3_TRANSCRIPTION_INSTRUCTION = (
    "Sen gelişmiş bir ses tanıma sistemisin. "
    "Türkçe, Rusça veya İngilizce konuşmaları algıla ve metne dök. "
    "Sadece duyduğun metni yaz, açıklama yapma."
)

PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION = """All generated images must follow the same visual style.

REFERENCE RULE

The reference image defines only the visual style:
- stroke thickness
- eye design
- face proportions
- simplicity of shapes

Do NOT copy:
- colors
- character identity
- composition
- exact shapes

Each request must generate a completely new character.

STYLE

flat SVG icon style
simple geometric shapes
clean vector shapes
minimal details
centered composition
white background

VECTOR RULE (strict)

The image must look like a simple SVG icon.

Shapes must be flat and uniform.

No gradients
No shading
No lighting
No color blending
No soft edges
No transparency
No blur
No glow

Each shape must use a single solid color.

STROKE RULE (strict)

All outlines must be black.

Outline color:
black #000000

Use a uniform thick SVG stroke.

The outline must not simulate lighting or shadow.

COLOR PALETTE (strict)

Only the following colors are allowed:

blue   #4A9AD4
red    #FF1F2D
pink   #EC6A9E
yellow #F5E617
green  #4CAF50
white  #FFFFFF
black  #000000

No other colors are allowed.

FILL RULE

All fills must be solid flat colors.

Each shape must use exactly one color from the palette.

No gradients.
No darker or lighter versions.
No shading.

FACE STRUCTURE (universal)

All characters share the same face structure.

large rounded head
two simple eyes
small nose
simple smiling mouth

optional round cheeks

CHARACTER TYPE RULE

Animals:
small rounded triangle nose pointing down
two-curve mouth

Humans:
small round nose (dot or oval)
simple curved smile
no animal muzzle

DESIGN RULES

large rounded head
simple shapes
vector friendly
minimal details

GENERATION RULE

Every request must generate a completely new illustration.

Never modify a previous image.
Never reproduce the reference character.

Always keep the same visual style across all characters.

The result must look like a simple exportable SVG icon created with solid fills and black outlines only."""


def normalize_model_name(value: object) -> str:
    model = str(value).strip()
    if model.startswith("models/"):
        model = model.removeprefix("models/")
    if not model or len(model) > 160 or not MODEL_NAME.fullmatch(model):
        raise ValueError("Google model names must be bare model IDs or use one models/ prefix")
    return model


class ProductionPipelineConfigStore:
    """Versioned non-secret production configuration for each pipeline."""

    def __init__(
        self,
        path: Path,
        defaults: Settings,
        prompt_templates: dict[str, str] | None = None,
    ) -> None:
        self.path = path.resolve()
        self._settings = defaults
        self._prompt_templates = dict(prompt_templates or {})
        self._lock = threading.RLock()

    def _default_pipeline(self, pipeline: str) -> dict[str, object]:
        image_model = self._settings.google_image_model
        if pipeline == "pipeline-3":
            image_model = "gemini-3-pro-image-preview"
        production_provider = "google" if self._settings.environment == "production" else None
        transcription_model = self._settings.google_transcription_model or "gemini-2.5-flash"
        normalization_model = self._settings.google_normalization_model or "gemini-2.5-flash"
        return {
            "image_provider": production_provider or self._settings.image_provider,
            "transcription_provider": production_provider
            or self._settings.transcription_provider,
            "normalization_provider": production_provider
            or self._settings.normalization_provider,
            "google_api_base_url": self._settings.google_api_base_url,
            "google_image_model": image_model or "gemini-3-pro-image-preview",
            "google_transcription_model": transcription_model,
            "google_normalization_model": normalization_model,
            "google_timeout_seconds": self._settings.google_timeout_seconds,
            "google_image_aspect_ratio": "1:1"
            if pipeline == "pipeline-3"
            else self._settings.google_image_aspect_ratio,
            "google_image_size": "1K"
            if pipeline == "pipeline-3"
            else self._settings.google_image_size,
            "palette_cleanup_passes": self._settings.palette_cleanup_passes,
            "palette_generation_attempts": self._settings.palette_generation_attempts,
            "prompt_template": self._prompt_templates.get(
                pipeline, PromptBuilder.editable_template()
            )
            if pipeline != "pipeline-3"
            else "",
            "transcription_instruction": PIPELINE_3_TRANSCRIPTION_INSTRUCTION
            if pipeline == "pipeline-3"
            else TRANSCRIPTION_INSTRUCTION,
            "normalization_instruction": NORMALIZATION_INSTRUCTION,
            "image_system_instruction": PIPELINE_3_IMAGE_SYSTEM_INSTRUCTION
            if pipeline == "pipeline-3"
            else "",
        }

    def defaults(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pipelines": {
                pipeline: self._default_pipeline(pipeline) for pipeline in PIPELINE_IDS
            },
        }

    def get(self) -> dict[str, object]:
        values = self.defaults()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            stored = {}
        if isinstance(stored, dict) and isinstance(stored.get("pipelines"), dict):
            merged = values["pipelines"]
            assert isinstance(merged, dict)
            for pipeline in PIPELINE_IDS:
                candidate = stored["pipelines"].get(pipeline)
                if isinstance(candidate, dict):
                    current = merged[pipeline]
                    assert isinstance(current, dict)
                    current.update(candidate)
        return self.validate(values)

    def get_pipeline(self, pipeline: str) -> dict[str, object]:
        payload = self.get()
        pipelines = payload["pipelines"]
        assert isinstance(pipelines, dict)
        return dict(pipelines[pipeline])

    def set_pipeline(self, pipeline: str, values: dict[str, object]) -> dict[str, object]:
        if pipeline not in PIPELINE_IDS:
            raise ValueError("Unknown pipeline")
        payload = self.get()
        pipelines = payload["pipelines"]
        assert isinstance(pipelines, dict)
        pipelines[pipeline] = values
        validated = self.validate(payload)
        self._write(validated)
        stored = validated["pipelines"]
        assert isinstance(stored, dict)
        return dict(stored[pipeline])

    def _write(self, values: dict[str, object]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_text(
                    json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def settings_for(self, pipeline: str) -> Settings:
        values = self.get_pipeline(pipeline)
        return replace(
            self._settings,
            image_provider=str(values["image_provider"]),
            transcription_provider=str(values["transcription_provider"]),
            normalization_provider=str(values["normalization_provider"]),
            google_api_base_url=str(values["google_api_base_url"]),
            google_image_model=str(values["google_image_model"]),
            google_transcription_model=str(values["google_transcription_model"]),
            google_normalization_model=str(values["google_normalization_model"]),
            google_timeout_seconds=float(values["google_timeout_seconds"]),
            google_image_aspect_ratio=str(values["google_image_aspect_ratio"]),
            google_image_size=str(values["google_image_size"]),
            palette_cleanup_passes=int(values["palette_cleanup_passes"]),
            palette_generation_attempts=int(values["palette_generation_attempts"]),
        )

    def validate(self, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) != {"schema_version", "pipelines"} or payload["schema_version"] != 1:
            raise ValueError("Invalid production pipeline configuration version")
        pipelines = payload["pipelines"]
        if not isinstance(pipelines, dict) or set(pipelines) != set(PIPELINE_IDS):
            raise ValueError("Exactly three production pipelines are required")
        validated: dict[str, dict[str, object]] = {}
        for pipeline in PIPELINE_IDS:
            raw = pipelines[pipeline]
            if not isinstance(raw, dict):
                raise TypeError(f"Invalid {pipeline} configuration")
            validated[pipeline] = self._validate_pipeline(pipeline, raw)
        return {"schema_version": 1, "pipelines": validated}

    def _validate_pipeline(self, pipeline: str, raw: dict[str, object]) -> dict[str, object]:
        required = set(self._default_pipeline(pipeline))
        if set(raw) != required:
            raise ValueError(f"{pipeline} fields are incomplete or unknown")
        result = dict(raw)
        for field in ("image_provider", "transcription_provider", "normalization_provider"):
            if result[field] not in {"mock", "google"}:
                raise ValueError(f"Invalid {pipeline} {field}")
            if self._settings.environment == "production" and result[field] == "mock":
                raise ValueError("Mock providers are forbidden in production")
        parsed = urlparse(str(result["google_api_base_url"]))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (
                parsed.hostname == "googleapis.com"
                or parsed.hostname.endswith(".googleapis.com")
            )
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Google base URL must be an HTTPS googleapis.com endpoint")
        for field in (
            "google_image_model",
            "google_transcription_model",
            "google_normalization_model",
        ):
            result[field] = normalize_model_name(result[field])
        timeout = float(result["google_timeout_seconds"])
        cleanup = int(result["palette_cleanup_passes"])
        attempts = int(result["palette_generation_attempts"])
        if not 1 <= timeout <= 300 or not 0 <= cleanup <= 3 or not 1 <= attempts <= 5:
            raise ValueError("Production pipeline runtime limits are invalid")
        result["google_timeout_seconds"] = timeout
        result["palette_cleanup_passes"] = cleanup
        result["palette_generation_attempts"] = attempts
        if result["google_image_aspect_ratio"] not in {
            "1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"
        }:
            raise ValueError("Invalid image aspect ratio")
        if result["google_image_size"] not in {"1K", "2K", "4K"}:
            raise ValueError("Invalid image size")
        for field in (
            "prompt_template",
            "transcription_instruction",
            "normalization_instruction",
            "image_system_instruction",
        ):
            value = str(result[field])
            if len(value) > 30_000:
                raise ValueError(f"{field} is too long")
            result[field] = value
        if pipeline != "pipeline-3" and not str(result["prompt_template"]).strip():
            raise ValueError("Processed pipelines require a prompt template")
        if pipeline == "pipeline-3":
            result["google_image_aspect_ratio"] = "1:1"
            result["google_image_size"] = "1K"
            if not str(result["image_system_instruction"]).strip():
                raise ValueError("pipeline-3 requires an image system instruction")
        return result
