from __future__ import annotations

from typing import Self

from app.models import GenerationRequest
from app.services import backends


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ValueError(f"http status {self.status_code}")


class _FakeClient:
    def __init__(self, *_: object, **__: object) -> None:
        self.poll_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, url: str, json: dict) -> _FakeResponse:
        assert url.endswith("/prompt")
        assert "prompt" in json
        return _FakeResponse({"prompt_id": "pid_123"})

    def get(self, url: str) -> _FakeResponse:
        assert url.endswith("/history/pid_123")
        self.poll_count += 1
        if self.poll_count < 2:
            return _FakeResponse({"pid_123": {"outputs": {}}})
        return _FakeResponse(
            {
                "pid_123": {
                    "outputs": {
                        "10": {
                            "images": [
                                {"filename": "a.png", "subfolder": "", "type": "output"},
                                {"filename": "b.png", "subfolder": "demo", "type": "output"},
                            ]
                        }
                    }
                }
            }
        )


def test_comfy_backend_extracts_candidates(monkeypatch, tmp_path) -> None:
    workflow_file = tmp_path / "workflow.json"
    workflow_file.write_text('{"1":{"inputs":{"text":"old"}}}', encoding="utf-8")
    monkeypatch.setattr(backends.httpx, "Client", _FakeClient)
    monkeypatch.setattr(backends.time, "sleep", lambda _: None)

    backend = backends.ComfyUIGenerationBackend(
        base_url="http://comfy.local",
        workflow_path=str(workflow_file),
        timeout_seconds=1,
        poll_interval_seconds=0,
        poll_max_attempts=5,
    )
    req = GenerationRequest(theme="cat", generator_backend="comfyui")
    result = backend.generate_candidates(job_id="job_1", request=req, count=2)

    assert len(result) == 2
    assert result[0].uri.startswith("http://comfy.local/view?")
    assert "filename=a.png" in result[0].uri
    assert "filename=b.png" in result[1].uri


def test_comfy_backend_requires_workflow_path() -> None:
    backend = backends.ComfyUIGenerationBackend(workflow_path="")
    req = GenerationRequest(theme="cat", generator_backend="comfyui")
    try:
        backend.generate_candidates(job_id="job_1", request=req, count=1)
        assert False, "expected ValueError for missing workflow path"
    except ValueError as exc:
        assert "TABLE_GEN_COMFYUI_WORKFLOW_PATH" in str(exc)
