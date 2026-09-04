from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from app.services.prompts import PromptBuilder
from app.settings import Settings


class ServerSettingsStore:
    """Atomic, non-secret runtime configuration persisted in the data volume."""

    def __init__(self, path: Path, defaults: Settings) -> None:
        self.path = path.resolve()
        self._defaults = defaults
        self._lock = threading.RLock()

    def defaults(self) -> dict[str, object]:
        return {
            "image_provider": self._defaults.image_provider,
            "transcription_provider": self._defaults.transcription_provider,
            "normalization_provider": self._defaults.normalization_provider,
            "google_api_base_url": self._defaults.google_api_base_url,
            "google_image_model": self._defaults.google_image_model,
            "google_transcription_model": self._defaults.google_transcription_model,
            "google_normalization_model": self._defaults.google_normalization_model,
            "google_translation_model": self._defaults.google_translation_model,
            "google_timeout_seconds": self._defaults.google_timeout_seconds,
            "google_image_aspect_ratio": self._defaults.google_image_aspect_ratio,
            "google_image_size": self._defaults.google_image_size,
            "palette_cleanup_passes": self._defaults.palette_cleanup_passes,
            "palette_generation_attempts": self._defaults.palette_generation_attempts,
            "prompt_templates": {
                "pipeline-1": PromptBuilder.editable_template(),
                "pipeline-2": PromptBuilder.editable_template(),
            },
        }

    def get(self) -> dict[str, object]:
        values = self.defaults()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            stored = {}
        if isinstance(stored, dict):
            values.update(stored)
        return self.validate(values)

    def set(self, values: dict[str, object]) -> dict[str, object]:
        validated = self.validate(values)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_text(
                    json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return validated

    def effective_settings(self) -> Settings:
        return self.candidate_settings(self.get())

    def candidate_settings(self, values: dict[str, object]) -> Settings:
        values = self.validate(values)
        return replace(
            self._defaults,
            image_provider=str(values["image_provider"]),
            transcription_provider=str(values["transcription_provider"]),
            normalization_provider=str(values["normalization_provider"]),
            google_api_base_url=str(values["google_api_base_url"]),
            google_image_model=str(values["google_image_model"]),
            google_transcription_model=str(values["google_transcription_model"]),
            google_normalization_model=str(values["google_normalization_model"]),
            google_translation_model=str(values["google_translation_model"]),
            google_timeout_seconds=float(values["google_timeout_seconds"]),
            google_image_aspect_ratio=str(values["google_image_aspect_ratio"]),
            google_image_size=str(values["google_image_size"]),
            palette_cleanup_passes=int(values["palette_cleanup_passes"]),
            palette_generation_attempts=int(values["palette_generation_attempts"]),
        )

    @staticmethod
    def validate(values: dict[str, object]) -> dict[str, object]:
        required = {
            "image_provider", "transcription_provider", "normalization_provider",
            "google_api_base_url", "google_image_model", "google_transcription_model",
            "google_normalization_model", "google_timeout_seconds", "google_image_aspect_ratio",
            "google_translation_model",
            "google_image_size", "palette_cleanup_passes", "palette_generation_attempts",
            "prompt_templates",
        }
        if set(values) != required:
            raise ValueError("Server settings fields are incomplete or unknown")
        for field in ("image_provider", "transcription_provider", "normalization_provider"):
            if values[field] not in {"mock", "google"}:
                raise ValueError(f"Invalid {field}")
        parsed = urlparse(str(values["google_api_base_url"]))
        if (
            parsed.scheme != "https" or not parsed.hostname
            or not (parsed.hostname == "googleapis.com" or parsed.hostname.endswith(".googleapis.com"))
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("Google base URL must be an HTTPS googleapis.com endpoint")
        for field in (
            "google_image_model",
            "google_transcription_model",
            "google_normalization_model",
            "google_translation_model",
        ):
            value = str(values[field]).strip()
            if len(value) > 200 or any(character in value for character in "\r\n"):
                raise ValueError(f"Invalid {field}")
            values[field] = value
        timeout = float(values["google_timeout_seconds"])
        cleanup = int(values["palette_cleanup_passes"])
        attempts = int(values["palette_generation_attempts"])
        if not 1 <= timeout <= 300 or not 0 <= cleanup <= 3 or not 1 <= attempts <= 5:
            raise ValueError("Runtime limits are invalid")
        if values["google_image_aspect_ratio"] not in {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}:
            raise ValueError("Invalid image aspect ratio")
        if values["google_image_size"] not in {"1K", "2K", "4K"}:
            raise ValueError("Invalid image size")
        templates = values["prompt_templates"]
        if not isinstance(templates, dict) or set(templates) != {"pipeline-1", "pipeline-2"}:
            raise ValueError("Both pipeline prompt templates are required")
        for template in templates.values():
            if not isinstance(template, str) or not template.strip() or len(template) > 20_000:
                raise ValueError("Prompt template is invalid")
        return dict(values)
