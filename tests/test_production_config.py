from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import MoonliError, install_error_handlers
from app.api.routes.production import router
from app.composition import build_components
from app.settings import Settings
from app.storage.production_secret_store import ProductionSecretStore


class _AcceptingValidator:
    async def validate(self, _: str) -> None:
        return None


class _RejectingValidator:
    async def validate(self, _: str) -> None:
        raise MoonliError("GOOGLE_KEY_INVALID", "Google rejected the credential.", 422)


def _client(tmp_path) -> tuple[TestClient, ProductionSecretStore]:
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        operator_access_key="test-operator-access-key",
    )
    components = build_components(settings)
    app = FastAPI()
    app.state.moonli_settings = settings
    app.state.client_authenticator = components.authenticator
    app.state.pipeline_profiles = components.profiles
    app.state.production_secret_store = components.production_secret_store
    app.state.production_pipeline_config_store = components.production_pipeline_config_store
    app.state.production_usage_store = components.production_usage_store
    app.state.system_monitor = components.system_monitor
    app.state.operator_auth_store = components.operator_auth_store
    app.state.audit_store = components.audit_store
    app.state.google_key_validator = _AcceptingValidator()
    app.state.server_settings_store = components.server_settings_store
    app.state.artifact_store = components.artifact_store
    app.state.run_repository = components.run_repository
    app.state.metrics = components.metrics
    app.state.generation_services = components.generation_services
    app.state.pipeline3_service = components.pipeline3_service
    app.state.routing_config_store = components.routing_config_store
    install_error_handlers(app)
    app.include_router(router)
    client = TestClient(app)
    session = components.operator_auth_store.create_session("test-operator-access-key")
    client.cookies.set(settings.operator_cookie_name, session.token)
    client.headers.update({"X-CSRF-Token": session.csrf_token})
    return client, components.production_secret_store


def _headers() -> dict[str, str]:
    return {}


def test_production_key_is_persisted_without_being_returned(tmp_path) -> None:
    client, store = _client(tmp_path)
    secret = "production-google-key-1234567890"
    with client:
        before = client.get("/internal/production/config", headers=_headers())
        saved = client.put(
            "/internal/production/google-key",
            headers=_headers(),
            json={"google_api_key": secret},
        )
        after = client.get("/internal/production/config", headers=_headers())

    assert before.status_code == saved.status_code == after.status_code == 200
    assert before.json()["production"]["google_key"]["configured"] is False
    assert saved.json()["google_key"] == {"configured": True, "source": "volume"}
    assert secret not in saved.text
    assert secret not in after.text
    assert store.path.read_text(encoding="utf-8") == secret
    assert ProductionSecretStore(store.path.parent).get_google_api_key() == secret


def test_rejected_google_key_does_not_replace_saved_secret(tmp_path) -> None:
    client, store = _client(tmp_path)
    original = "original-google-key-1234567890"
    store.set_google_api_key(original)
    client.app.state.google_key_validator = _RejectingValidator()

    with client:
        response = client.put(
            "/internal/production/google-key",
            json={"google_api_key": "rejected-google-key-1234567890"},
        )

    assert response.status_code == 422
    assert store.get_google_api_key() == original


def test_production_config_contains_two_client_and_four_internal_requests(tmp_path) -> None:
    client, _ = _client(tmp_path)
    with client:
        response = client.get("/internal/production/config", headers=_headers())

    assert response.status_code == 200
    requests = response.json()["production"]["requests"]
    assert len(requests) == 6
    assert [item["id"] for item in requests] == [
        "client-text",
        "client-audio",
        "google-transcription",
        "google-normalization",
        "google-image-pipeline-1",
        "google-image-pipeline-2",
    ]
    assert all(item["request"]["method"] == "POST" for item in requests)
    assert requests[0]["request"]["url"].endswith("/v1/generate")
    assert requests[0]["request"]["headers"]["X-Moonli-Device-Id"]
    assert requests[1]["request"]["headers"]["X-Moonli-Device-Id"]
    assert requests[2]["request"]["url"].endswith(":generateContent")
    assert requests[4]["request"]["body"] != requests[5]["request"]["body"]
    assert "GOOGLE_API_KEY_FROM_VOLUME" in response.text
    pipelines = response.json()["production"]["pipelines"]
    assert set(pipelines) == {"pipeline-1", "pipeline-2", "pipeline-3"}
    assert pipelines["pipeline-3"]["google_image_model"] == "gemini-3-pro-image-preview"
    assert pipelines["pipeline-3"]["output"] == {
        "type": "jpeg-set",
        "count": 3,
        "width": 1024,
        "height": 1024,
        "files": ["image_1.jpg", "image_2.jpg", "image_3.jpg"],
        "post_processing": False,
    }


def test_pipeline_3_integration_kit_contains_two_requests_and_two_scripts(tmp_path) -> None:
    client, _ = _client(tmp_path)
    with client:
        response = client.get(
            "/internal/production/pipelines/pipeline-3/integration"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["pipeline"] == "pipeline-3"
    assert [item["id"] for item in payload["requests"]] == [
        "normalize-audio",
        "generate-images",
    ]
    assert [item["filename"] for item in payload["scripts"]] == [
        "pipeline3_transcription.py",
        "pipeline3_generation.py",
    ]
    assert "POST /v1/normalize HTTP/1.1" in payload["requests"][0]["content"]
    assert "POST /v1/generate HTTP/1.1" in payload["requests"][1]["content"]
    assert "<NORMALIZED_TEXT_ONLY>" in payload["requests"][0]["content"]
    assert "image_1.jpg" in payload["requests"][1]["content"]
    assert 'op("../answer")' in payload["scripts"][0]["content"]
    assert 'target_op = op("answer")' in payload["scripts"][1]["content"]
    assert "PASTE_MOONLI_ACCESS_KEY_HERE" in response.text
    assert "AIza" not in response.text


def test_pipeline_configuration_update_is_isolated_and_applied(tmp_path) -> None:
    client, _ = _client(tmp_path)
    before = client.app.state.production_pipeline_config_store.get()
    pipeline_3 = dict(before["pipelines"]["pipeline-3"])
    pipeline_3["google_image_model"] = "models/custom-pipeline-three-model"

    with client:
        response = client.put(
            "/internal/production/pipelines/pipeline-3/config",
            json=pipeline_3,
        )
        after = client.get("/internal/production/config")

    assert response.status_code == after.status_code == 200
    assert response.json()["config"]["google_image_model"] == "custom-pipeline-three-model"
    pipelines = after.json()["production"]["pipelines"]
    assert pipelines["pipeline-1"] == before["pipelines"]["pipeline-1"] | {
        "google_key": pipelines["pipeline-1"]["google_key"],
        "output": pipelines["pipeline-1"]["output"],
    }
    assert pipelines["pipeline-3"]["google_image_model"] == "custom-pipeline-three-model"


def test_production_config_requires_moonli_authentication(tmp_path) -> None:
    client, _ = _client(tmp_path)
    client.cookies.clear()
    with client:
        response = client.get("/internal/production/config")
    assert response.status_code == 401


def test_production_stats_expose_system_and_persistent_usage(tmp_path) -> None:
    client, _ = _client(tmp_path)
    components_usage = client.app.state.production_usage_store
    components_usage.record_request("pipeline-1", "text")
    components_usage.record_google_response(
        "normalization",
        {
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 5,
                "totalTokenCount": 17,
            }
        },
    )

    with client:
        response = client.get("/internal/production/stats", headers=_headers())

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["system"]) == {"cpu_percent", "ram", "disk", "uptime_seconds"}
    assert payload["production_api"]["requests"] == 1
    assert payload["production_api"]["tokens"] == 17
    assert len(payload["production_api"]["series"]) == 24


def test_production_stats_require_moonli_authentication(tmp_path) -> None:
    client, _ = _client(tmp_path)
    client.cookies.clear()
    with client:
        response = client.get("/internal/production/stats")
    assert response.status_code == 401


def test_production_can_start_before_volume_key_is_configured(tmp_path) -> None:
    settings = replace(
        Settings.from_env(),
        environment="production",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        image_provider="google",
        transcription_provider="google",
        normalization_provider="google",
        google_api_key="",
        google_image_model="image-model",
        google_transcription_model="transcription-model",
        google_normalization_model="normalization-model",
        api_keys=("production-client-key",),
        operator_access_key="production-operator-access-key",
    )

    settings.validate()
    components = build_components(settings)

    assert components.production_secret_store.get_google_api_key() == ""
