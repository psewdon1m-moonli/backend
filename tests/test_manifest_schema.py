import json
from pathlib import Path

from app.services.validation import validate_manifest


def test_manifest_requires_vector_assets(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "v1",
                "pack_id": "pack_x",
                "source": {
                    "mode": "generate",
                    "job_id": "job_x",
                    "theme": "cat",
                    "difficulty": "medium",
                    "generator_backend": "mock",
                },
                "composition": {"candidate_id": "cand_x"},
                "palette": {"colors": ["#FF0000"]},
                "steps": [{"step_index": 1, "color": "#FF0000", "layer_mask": "layers/a.txt", "cumulative_state": "cumulative/a.txt"}],
                "assets": {"preview": "assets/preview.txt"},
            }
        ),
        encoding="utf-8",
    )
    report = validate_manifest(manifest_path)
    assert report.valid is False
    assert any("vector_assets" in message for message in report.errors)

