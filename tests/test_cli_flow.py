from pathlib import Path

import pytest

from app.cli import run_flow
from app.services.pipeline import publish_pack


def test_cli_generate_flow_creates_vector_artifacts() -> None:
    result = run_flow(
        source_mode="generate",
        theme="forest animal",
        palette_name="kids_basic",
        generator_backend="mock",
    )
    assert result["job_id"].startswith("job_")
    assert result["pack_id"].startswith("pack_")
    assert Path(result["master_svg"]).exists()
    assert result["layer_count"] == "4"


def test_cli_library_flow_uses_library_item() -> None:
    generated = run_flow(
        source_mode="generate",
        theme="owl",
        palette_name="forest",
        generator_backend="mock",
    )
    publish_pack(pack_id=generated["pack_id"], destination="library")

    result = run_flow(
        source_mode="library",
        theme="ignored",
        palette_name="forest",
        generator_backend="mock",
    )
    assert result["job_id"].startswith("job_")
    assert Path(result["master_svg"]).exists()


def test_cli_generate_comfyui_failure_is_clear() -> None:
    with pytest.raises(RuntimeError, match="ComfyUI generation failed"):
        run_flow(
            source_mode="generate",
            theme="fox",
            palette_name="forest",
            generator_backend="comfyui",
        )

