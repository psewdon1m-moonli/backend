from __future__ import annotations

import pytest

from app.settings import Settings


def test_production_rejects_mock_providers(monkeypatch) -> None:
    monkeypatch.setenv("MOONLI_ENV", "production")
    monkeypatch.setenv("MOONLI_API_KEYS", "production-client-key")
    monkeypatch.setenv("MOONLI_ALLOWED_HOSTS", "moonli.example.com")
    monkeypatch.setenv("MOONLI_IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("MOONLI_TRANSCRIPTION_PROVIDER", "google")
    with pytest.raises(ValueError, match="Mock providers"):
        Settings.from_env()


def test_production_rejects_wildcard_host(monkeypatch) -> None:
    monkeypatch.setenv("MOONLI_ALLOWED_HOSTS", "*")
    with pytest.raises(ValueError, match="explicit hosts"):
        Settings.from_env()
