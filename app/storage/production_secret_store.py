from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path


class ProductionSecretStore:
    """Persist production-only secrets in a mounted runtime directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._path = self._root / "google-api-key"
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def get_google_api_key(self) -> str:
        with self._lock:
            try:
                value = self._path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return ""
        return value if self._valid(value) else ""

    def set_google_api_key(self, value: str) -> None:
        normalized = value.strip()
        if not self._valid(normalized):
            raise ValueError("Google API key must contain 16-512 non-whitespace characters")
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            try:
                self._root.chmod(0o700)
            except OSError:
                pass
            temporary = self._root / f".google-api-key.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_text(normalized, encoding="utf-8")
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, self._path)
            finally:
                temporary.unlink(missing_ok=True)

    def status(self, fallback: str = "") -> dict[str, object]:
        stored = self.get_google_api_key()
        effective = stored or fallback.strip()
        source = "volume" if stored else "environment" if effective else None
        return {
            "configured": bool(effective),
            "source": source,
        }

    def clear_google_api_key(self) -> None:
        with self._lock:
            self._path.unlink(missing_ok=True)

    @staticmethod
    def _valid(value: str) -> bool:
        return 16 <= len(value) <= 512 and not any(character.isspace() for character in value)
