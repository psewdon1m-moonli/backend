from __future__ import annotations

import json
from pathlib import Path

from app.domain.images import PaletteValidatedImage
from app.domain.profiles import PipelineProfile
from app.services.outputs.models import BuiltOutput
from app.services.outputs.validator import RuntimeValidator
from app.services.processing.layers.processor import sha256_file


class FullImageOutputBuilder:
    media_type = "image/png"

    def __init__(self, validator: RuntimeValidator) -> None:
        self._validator = validator

    def build(
        self,
        validated: PaletteValidatedImage,
        profile: PipelineProfile,
        run_id: str,
        run_dir: Path,
    ) -> BuiltOutput:
        result_path = run_dir / "result.png"
        validated.image.save(result_path, format="PNG", optimize=True)
        checksum = sha256_file(result_path)
        internal_manifest = {
            "contract_version": "1.0",
            "run_id": run_id,
            "output_mode": profile.output_mode,
            "palette_version": profile.palette.id,
            "result": "result.png",
            "sha256": checksum,
            "canvas": {"width": profile.width, "height": profile.height},
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(internal_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._validator.validate_full_image(result_path, profile, checksum)
        return BuiltOutput(result_path, "result.png", self.media_type, checksum)
