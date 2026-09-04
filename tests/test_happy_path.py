import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_phase1_happy_path() -> None:
    with TestClient(app) as client:
        create = client.post(
            "/jobs",
            json={
                "mode": "generate",
                "generator_backend": "mock",
                "theme": "forest animal",
                "difficulty": "medium",
                "max_colors": 4,
                "table_profile_id": "table_default",
                "session_profile_id": "kids_20m",
            },
        )
        assert create.status_code == 200
        job = create.json()
        job_id = job["job_id"]

        generated = client.post(f"/jobs/{job_id}/generate")
        assert generated.status_code == 200
        shortlist = generated.json()["items"]
        assert len(shortlist) == 4

        selected = client.post(
            f"/jobs/{job_id}/selection/composition",
            json={"candidate_id": shortlist[0]["candidate_id"]},
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "composition_selected"

        palette = client.post(
            f"/jobs/{job_id}/selection/palette",
            json={"colors": ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"]},
        )
        assert palette.status_code == 200
        assert palette.json()["status"] == "palette_selected"

        built = client.post(f"/jobs/{job_id}/build")
        assert built.status_code == 200
        pack_id = built.json()["pack_id"]
        assert built.json()["validation"]["valid"] is True
        manifest_path = Path(built.json()["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "vector_assets" in manifest
        assert manifest["vector_assets"]["master_svg"] == "vector/master.svg"

        validated = client.get(f"/packs/{pack_id}/validate")
        assert validated.status_code == 200
        assert validated.json()["valid"] is True

        published = client.post(
            f"/packs/{pack_id}/publish",
            json={"destination": "runtime_cache"},
        )
        assert published.status_code == 200
        assert published.json()["destination"] == "runtime_cache"
