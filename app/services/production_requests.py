from __future__ import annotations

from app.domain.profiles import PipelineProfile
from app.providers.image_generation.google import build_image_generation_request
from app.providers.prompt_normalization.google import build_normalization_request
from app.providers.transcription.google import build_transcription_request
from app.services.prompts import PromptBuilder
from app.settings import Settings


def _model_endpoint(settings: Settings, model: str, fallback: str) -> str:
    selected = model or fallback
    return f"{settings.google_api_base_url}/models/{selected}:generateContent"


def _google_request(
    request_id: str,
    title: str,
    url: str,
    body: dict[str, object],
) -> dict[str, object]:
    return {
        "id": request_id,
        "title": title,
        "direction": "Moonli backend → Google",
        "request": {
            "method": "POST",
            "url": url,
            "headers": {
                "Content-Type": "application/json",
                "x-goog-api-key": "<GOOGLE_API_KEY_FROM_VOLUME>",
            },
            "body": body,
        },
    }


def build_production_request_templates(
    settings: Settings, profiles: dict[str, PipelineProfile]
) -> list[dict[str, object]]:
    public_url = f"https://{settings.allowed_hosts[0]}/v1/generate"
    requests: list[dict[str, object]] = [
        {
            "id": "client-text",
            "title": "Client · text input",
            "direction": "Android / TouchDesigner → Moonli backend",
            "request": {
                "method": "POST",
                "url": public_url,
                "headers": {
                    "Authorization": "Bearer <MOONLI_ACCESS_KEY>",
                    "X-Moonli-Device-Id": "<td-########|aa-########>",
                    "Idempotency-Key": "<UNIQUE_REQUEST_ID>",
                    "Content-Type": "application/json",
                },
                "body": {
                    "type": "text",
                    "pipeline": "<pipeline-1|pipeline-2>",
                    "text": "<USER_REQUEST>",
                },
            },
        },
        {
            "id": "client-audio",
            "title": "Client · audio input",
            "direction": "Android / TouchDesigner → Moonli backend",
            "request": {
                "method": "POST",
                "url": public_url,
                "headers": {
                    "Authorization": "Bearer <MOONLI_ACCESS_KEY>",
                    "X-Moonli-Device-Id": "<td-########|aa-########>",
                    "Idempotency-Key": "<UNIQUE_REQUEST_ID>",
                    "Content-Type": "multipart/form-data; boundary=<generated>",
                },
                "multipart": {
                    "type": "audio",
                    "pipeline": "<pipeline-1|pipeline-2>",
                    "audio": "<BINARY_AUDIO_FILE>",
                },
            },
        },
    ]
    requests.append(
        _google_request(
            "google-transcription",
            "Google · transcription",
            _model_endpoint(
                settings,
                settings.google_transcription_model,
                "<GOOGLE_TRANSCRIPTION_MODEL>",
            ),
            build_transcription_request("<AUDIO_MIME_TYPE>", "<BASE64_AUDIO>"),
        )
    )
    requests.append(
        _google_request(
            "google-normalization",
            "Google · prompt normalization",
            _model_endpoint(
                settings,
                settings.google_normalization_model,
                "<GOOGLE_NORMALIZATION_MODEL>",
            ),
            build_normalization_request("<SOURCE_TEXT>"),
        )
    )
    for pipeline_id in ("pipeline-1", "pipeline-2"):
        profile = profiles[pipeline_id]
        prompt = PromptBuilder().build("<NORMALIZED_VISUAL_REQUEST>", profile)
        requests.append(
            _google_request(
                f"google-image-{pipeline_id}",
                f"Google · image generation · {pipeline_id}",
                _model_endpoint(
                    settings,
                    settings.google_image_model,
                    "<GOOGLE_IMAGE_MODEL>",
                ),
                build_image_generation_request(
                    prompt.text,
                    settings.google_image_aspect_ratio,
                    settings.google_image_size,
                ),
            )
        )
    return requests
