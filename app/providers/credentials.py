from __future__ import annotations

from collections.abc import Callable

from app.providers.errors import ProviderError

GoogleApiKeySource = str | Callable[[], str]


def validate_google_api_key_source(source: GoogleApiKeySource) -> None:
    if isinstance(source, str):
        if not source.strip():
            raise ValueError("Google API key is required")
        return
    if not callable(source):
        raise TypeError("Google API key source must be text or a callable")


def resolve_google_api_key(source: GoogleApiKeySource) -> str:
    value = source() if callable(source) else source
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ProviderError("Google API key is not configured")
    return normalized
