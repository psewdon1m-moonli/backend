from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from app.storage.production_pipeline_config import PIPELINE_IDS


class ProductionSecretStore:
    """Persist production-only secrets in a mounted runtime directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._path = self._root / "google-api-key"
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _pipeline_path(self, pipeline: str | None) -> Path:
        if pipeline is None:
            return self._path
        if pipeline not in PIPELINE_IDS:
            raise ValueError("Unknown pipeline")
        return self._root / "google" / pipeline

    def get_google_api_key(self, pipeline: str | None = None) -> str:
        path = self._pipeline_path(pipeline)
        with self._lock:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                if pipeline is not None:
                    return self.get_google_api_key()
                return ""
        return value if self._valid(value) else ""

    def set_google_api_key(self, value: str, pipeline: str | None = None) -> None:
        normalized = value.strip()
        if not self._valid(normalized):
            raise ValueError("Google API key must contain 16-512 non-whitespace characters")
        path = self._pipeline_path(pipeline)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._root.chmod(0o700)
            except OSError:
                pass
            temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_text(normalized, encoding="utf-8")
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def status(self, fallback: str = "", pipeline: str | None = None) -> dict[str, object]:
        pipeline_path = self._pipeline_path(pipeline)
        try:
            pipeline_value = pipeline_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pipeline_value = ""
        stored = pipeline_value if self._valid(pipeline_value) else ""
        legacy = self.get_google_api_key() if pipeline is not None else ""
        effective = stored or fallback.strip()
        effective = effective or legacy
        source = (
            "volume"
            if stored
            else "environment"
            if fallback.strip()
            else "legacy-volume"
            if legacy
            else None
        )
        return {
            "configured": bool(effective),
            "source": source,
        }

    def clear_google_api_key(self, pipeline: str | None = None) -> None:
        with self._lock:
            self._pipeline_path(pipeline).unlink(missing_ok=True)

    @staticmethod
    def _valid(value: str) -> bool:
        return 16 <= len(value) <= 512 and not any(character.isspace() for character in value)
