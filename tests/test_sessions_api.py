from fastapi.testclient import TestClient

from app.main import app


def test_create_session_and_select_mode() -> None:
    with TestClient(app) as client:
        created = client.post("/sessions")
        assert created.status_code == 200
        session = created.json()
        assert session["status"] == "created"
        session_id = session["session_id"]

        mode = client.post(f"/sessions/{session_id}/mode", json={"mode": "library"})
        assert mode.status_code == 200
        assert mode.json()["status"] == "mode_selected"
        assert mode.json()["mode"] == "library"

