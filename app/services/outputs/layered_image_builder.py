from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.domain.images import PaletteValidatedImage
from app.domain.profiles import PipelineProfile
from app.services.outputs.models import BuiltOutput
from app.services.outputs.validator import RuntimeValidator
from app.services.processing.layers import LayerProcessor
from app.services.processing.layers.processor import sha256_file


class LayeredImageOutputBuilder:
    media_type = "application/vnd.moonli.layers+zip"

    def __init__(self, layer_processor: LayerProcessor, validator: RuntimeValidator) -> None:
        self._layer_processor = layer_processor
        self._validator = validator

    def build(
        self,
        validated: PaletteValidatedImage,
        profile: PipelineProfile,
        run_id: str,
        run_dir: Path,
    ) -> BuiltOutput:
        package_dir = run_dir / "package"
        result = self._layer_processor.process(validated, profile, package_dir)
        layers = [
            {
                "index": layer.palette_index,
                "color": layer.color,
                "used": layer.used,
                "image": f"layers/{layer.palette_index:02d}.png",
                "sha256": layer.sha256,
            }
            for layer in result.layers
        ]
        manifest = {
            "contract_version": "1.0",
            "run_id": run_id,
            "output_mode": "layered_image",
            "palette_version": profile.palette.id,
            "canvas": {"width": result.width, "height": result.height},
            "composite": "composite.png",
            "composite_sha256": result.composite_sha256,
            "palette": [
                {"index": layer.palette_index, "color": layer.color, "used": layer.used}
                for layer in result.layers
            ],
            "layers": layers,
        }
        (package_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._validator.validate_layered_package(package_dir, result, profile)

        zip_path = run_dir / "result.zip"
        ordered = [package_dir / "manifest.json", package_dir / "composite.png"]
        ordered.extend(package_dir / f"layers/{index:02d}.png" for index in range(len(result.layers)))
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in ordered:
                archive.write(path, path.relative_to(package_dir).as_posix())
        checksum = sha256_file(zip_path)
        self._validator.validate_zip(zip_path, checksum, len(result.layers))
        return BuiltOutput(zip_path, "result.zip", self.media_type, checksum)
