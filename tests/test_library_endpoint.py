from fastapi.testclient import TestClient

from app.main import app


def test_library_items_endpoint_returns_list() -> None:
    with TestClient(app) as client:
        response = client.get("/library/items")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

