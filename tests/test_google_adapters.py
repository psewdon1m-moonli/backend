from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from typing import ClassVar

import pytest
from PIL import Image

from app.domain.inputs import AudioInput
from app.domain.profiles import Palette, PipelineProfile
from app.domain.prompts import GenerationPrompt, VisualBrief
from app.providers.errors import ProviderError
from app.providers.image_generation import google as image_google
from app.providers.prompt_normalization import google as normalization_google
from app.providers.transcription import google as transcription_google


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Client:
    response_payload: ClassVar[dict] = {}
    captured: ClassVar[dict] = {}

    def __init__(self, **kwargs) -> None:
        self.captured["timeout"] = kwargs.get("timeout")
        self.captured["proxy"] = kwargs.get("proxy")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, json: dict, headers: dict) -> _Response:
        self.captured.update({"url": url, "json": json, "headers": headers})
        return _Response(self.response_payload)


def test_google_transcriber_returns_plain_transcription(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [{"content": {"parts": [{"text": "  a red tree  "}]}}]
    }
    _Client.captured = {}
    monkeypatch.setattr(transcription_google.httpx, "AsyncClient", _Client)
    adapter = transcription_google.GoogleTranscriber(
        "https://google.invalid/v1beta", "secret", "transcription-model", 12
    )
    result = asyncio.run(adapter.transcribe(AudioInput(b"audio", "audio/wav", "input.wav")))
    assert result == "a red tree"
    inline = _Client.captured["json"]["contents"][0]["parts"][1]["inlineData"]
    assert base64.b64decode(inline["data"]) == b"audio"
    assert "prompt" not in result.lower()


def test_google_adapter_reports_provider_token_usage(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [{"content": {"parts": [{"text": "a red tree"}]}}],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 4,
            "totalTokenCount": 15,
        },
    }
    _Client.captured = {}
    monkeypatch.setattr(transcription_google.httpx, "AsyncClient", _Client)
    recorded = []
    adapter = transcription_google.GoogleTranscriber(
        "https://google.invalid/v1beta",
        "secret",
        "transcription-model",
        12,
        lambda stage, body: recorded.append((stage, body["usageMetadata"])),
    )

    asyncio.run(adapter.transcribe(AudioInput(b"audio", "audio/wav", "input.wav")))

    assert recorded == [
        (
            "transcription",
            {
                "promptTokenCount": 11,
                "candidatesTokenCount": 4,
                "totalTokenCount": 15,
            },
        )
    ]


def test_google_adapter_resolves_rotated_key_at_request_time(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [{"content": {"parts": [{"text": "a red tree"}]}}]
    }
    _Client.captured = {}
    monkeypatch.setattr(transcription_google.httpx, "AsyncClient", _Client)
    current = {"key": "first-production-key"}
    adapter = transcription_google.GoogleTranscriber(
        "https://google.invalid/v1beta",
        lambda: current["key"],
        "transcription-model",
        12,
    )
    current["key"] = "rotated-production-key"

    asyncio.run(adapter.transcribe(AudioInput(b"audio", "audio/wav", "input.wav")))

    assert _Client.captured["headers"]["x-goog-api-key"] == "rotated-production-key"


def test_google_adapter_resolves_proxy_route_at_request_time(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [{"content": {"parts": [{"text": "a red tree"}]}}]
    }
    _Client.captured = {}
    monkeypatch.setattr(transcription_google.httpx, "AsyncClient", _Client)
    current = {"proxy": None}
    adapter = transcription_google.GoogleTranscriber(
        "https://google.invalid/v1beta",
        "secret",
        "transcription-model",
        12,
        proxy_url=lambda: current["proxy"],
    )
    current["proxy"] = "http://vless-proxy:18080"

    asyncio.run(adapter.transcribe(AudioInput(b"audio", "audio/wav", "input.wav")))

    assert _Client.captured["proxy"] == "http://vless-proxy:18080"


def test_google_prompt_normalizer_extracts_short_english_visual_intent(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": '{"normalized_prompt":"apple tree with red apples"}'}
                    ]
                }
            }
        ]
    }
    _Client.captured = {}
    monkeypatch.setattr(normalization_google.httpx, "AsyncClient", _Client)
    adapter = normalization_google.GooglePromptNormalizer(
        "https://google.invalid/v1beta", "secret", "normalization-model", 12
    )

    result = asyncio.run(
        adapter.normalize(
            "Привет, это тестовый запуск. А, сделай мне, пожалуйста, яблоню с красными яблоками."
        )
    )

    assert result == "apple tree with red apples"
    sent = _Client.captured["json"]
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    assert sent["generationConfig"]["maxOutputTokens"] == 1024
    assert "Remove greetings" in sent["contents"][0]["parts"][0]["text"]


def test_google_prompt_normalizer_accepts_plain_text_from_compatible_model(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [{"content": {"parts": [{"text": "cute penguin icon"}]}}]
    }
    _Client.captured = {}
    monkeypatch.setattr(normalization_google.httpx, "AsyncClient", _Client)
    adapter = normalization_google.GooglePromptNormalizer(
        "https://google.invalid/v1beta", "secret", "normalization-model", 12
    )

    result = asyncio.run(adapter.normalize("Нарисуй милого пингвина"))

    assert result == "cute penguin icon"


def test_google_prompt_normalizer_preserves_structured_negative_constraints(
    monkeypatch,
) -> None:
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": (
                                '{"subject":"moonlit garden with a red tree and a tiger",'
                                '"must_include":[],"must_avoid":["people"]}'
                            )
                        }
                    ]
                }
            }
        ]
    }
    _Client.captured = {}
    monkeypatch.setattr(normalization_google.httpx, "AsyncClient", _Client)
    adapter = normalization_google.GooglePromptNormalizer(
        "https://google.invalid/v1beta", "secret", "normalization-model", 12
    )

    result = asyncio.run(
        adapter.normalize(
            "A calm moonlit garden with one red tree, without peoplebut with tiger"
        )
    )

    assert result == "moonlit garden with a red tree and a tiger. without people"
    schema = _Client.captured["json"]["generationConfig"]["responseSchema"]
    assert schema["required"] == ["subject", "must_include", "must_avoid"]
    assert "peoplebut" in _Client.captured["json"]["contents"][0]["parts"][0]["text"]


def test_google_prompt_normalizer_rejects_dropped_negative_constraints(monkeypatch) -> None:
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": '{"normalized_prompt":"garden with a tiger"}'}
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr(normalization_google.httpx, "AsyncClient", _Client)
    adapter = normalization_google.GooglePromptNormalizer(
        "https://google.invalid/v1beta", "secret", "normalization-model", 12
    )

    with pytest.raises(ProviderError, match="dropped an explicit negative"):
        asyncio.run(adapter.normalize("garden with a tiger, without people"))


def test_google_image_adapter_returns_materializable_inline_image(monkeypatch) -> None:
    image = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(output.getvalue()).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    _Client.captured = {}
    monkeypatch.setattr(image_google.httpx, "AsyncClient", _Client)
    adapter = image_google.GoogleImageGenerator(
        "https://google.invalid/v1beta", "secret", "image-model", 12, "1:1", "1K"
    )
    palette = Palette("p1", 1, ("#FF0000",))
    profile = PipelineProfile("pipeline-1", "full_image", palette, 2, 2, ())
    prompt = GenerationPrompt("Only #FF0000", "v1", VisualBrief("tree", (), (), (), False))
    result = asyncio.run(adapter.generate(prompt, palette, profile, 2))
    assert result.media_type == "image/png"
    assert result.content == output.getvalue()
    sent_text = _Client.captured["json"]["contents"][0]["parts"][0]["text"]
    assert "previous result violated" in sent_text
    assert "large connected regions of solid flat color" in sent_text
    assert "grain, stippling" in sent_text


def test_google_image_adapter_normalizes_jpeg_response_to_png(monkeypatch) -> None:
    image = Image.new("RGB", (2, 2), (255, 0, 0))
    jpeg = BytesIO()
    image.save(jpeg, format="JPEG")
    _Client.response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": base64.b64encode(jpeg.getvalue()).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    _Client.captured = {}
    monkeypatch.setattr(image_google.httpx, "AsyncClient", _Client)
    adapter = image_google.GoogleImageGenerator(
        "https://google.invalid/v1beta", "secret", "image-model", 12, "1:1", "1K"
    )
    palette = Palette("p1", 1, ("#FE0000",))
    profile = PipelineProfile("pipeline-1", "full_image", palette, 2, 2, ())
    prompt = GenerationPrompt("Only #FE0000", "v1", VisualBrief("tree", (), (), (), False))

    result = asyncio.run(adapter.generate(prompt, palette, profile, 1))

    assert result.media_type == "image/png"
    with Image.open(BytesIO(result.content)) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (2, 2)
