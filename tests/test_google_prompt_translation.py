from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from app.providers.errors import ProviderError
from app.providers.prompt_translation import google as translation_google


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _Client:
    response_payload: ClassVar[dict[str, object]] = {}
    captured: ClassVar[dict[str, object]] = {}

    def __init__(self, **kwargs) -> None:
        self.captured["client"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, json: dict, headers: dict) -> _Response:
        self.captured.update({"url": url, "json": json, "headers": headers})
        return _Response(self.response_payload)


def _translator(**kwargs) -> translation_google.GooglePromptTranslator:
    return translation_google.GooglePromptTranslator(
        "https://google.invalid/v1beta",
        "secret",
        "gemini-2.5-flash",
        12,
        **kwargs,
    )


def test_google_prompt_translator_translates_russian_without_adding_details(
    monkeypatch,
) -> None:
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "apple tree with red apples"}]
                }
            }
        ]
    }
    _Client.captured = {}
    usage: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(translation_google.httpx, "AsyncClient", _Client)

    result = asyncio.run(
        _translator(usage_recorder=lambda stage, body: usage.append((stage, body))).translate(
            "яблоня с красными яблоками"
        )
    )

    assert result == "apple tree with red apples"
    assert _Client.captured["url"].endswith(
        "/models/gemini-2.5-flash:generateContent"
    )
    payload = _Client.captured["json"]
    assert payload["generationConfig"] == {
        "temperature": 0,
        "maxOutputTokens": 512,
        "responseMimeType": "text/plain",
    }
    assert "Do not add" in payload["contents"][0]["parts"][0]["text"]
    assert usage == [("translation", _Client.response_payload)]


def test_google_prompt_translator_skips_google_for_english(monkeypatch) -> None:
    class _ForbiddenClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("English prompt must not call Google")

    monkeypatch.setattr(translation_google.httpx, "AsyncClient", _ForbiddenClient)

    result = asyncio.run(_translator().translate("cute penguin icon"))

    assert result == "cute penguin icon"


def test_google_prompt_translator_rejects_dropped_negative_constraint(
    monkeypatch,
) -> None:
    _Client.response_payload = {
        "candidates": [
            {"content": {"parts": [{"text": "moonlit garden with a tiger"}]}}
        ]
    }
    monkeypatch.setattr(translation_google.httpx, "AsyncClient", _Client)

    with pytest.raises(ProviderError, match="dropped a negative constraint"):
        asyncio.run(_translator().translate("лунный сад с тигром без людей"))
