from __future__ import annotations

import json
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from app.domain.layers import LayeredImageResult
from app.domain.profiles import PipelineProfile
from app.services.processing.layers.processor import sha256_file
from app.services.processing.palette_validator import hex_to_rgb
from app.services.processing.pixels import pixel_data


class OutputValidationError(ValueError):
    pass


def _safe_member(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


class RuntimeValidator:
    def validate_full_image(
        self, path: Path, profile: PipelineProfile, expected_sha256: str
    ) -> None:
        if not path.is_file() or sha256_file(path) != expected_sha256:
            raise OutputValidationError("Full-image result checksum mismatch")
        try:
            source = Image.open(path)
            source.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise OutputValidationError("Full-image result is not a decodable PNG") from exc
        if source.format != "PNG" or source.size != (profile.width, profile.height):
            raise OutputValidationError("Full-image result has an invalid format or dimensions")
        allowed = {hex_to_rgb(color) for color in profile.palette.colors}
        for red, green, blue, alpha in pixel_data(source.convert("RGBA")):
            if alpha and (red, green, blue) not in allowed:
                raise OutputValidationError("Full-image result contains an unexpected color")

    def validate_layered_package(
        self,
        package_dir: Path,
        result: LayeredImageResult,
        profile: PipelineProfile,
    ) -> None:
        manifest_path = package_dir / "manifest.json"
        if not manifest_path.is_file():
            raise OutputValidationError("Layered-image manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutputValidationError("Layered-image manifest is invalid JSON") from exc
        if manifest.get("contract_version") != "1.0" or manifest.get("output_mode") != "layered_image":
            raise OutputValidationError("Layered-image manifest contract is invalid")
        if manifest.get("palette_version") != profile.palette.id:
            raise OutputValidationError("Layered-image manifest palette version is invalid")
        canvas = manifest.get("canvas") or {}
        if (canvas.get("width"), canvas.get("height")) != (result.width, result.height):
            raise OutputValidationError("Layered-image manifest canvas is invalid")
        if manifest.get("composite") != "composite.png":
            raise OutputValidationError("Layered-image manifest composite path is invalid")
        if manifest.get("composite_sha256") != result.composite_sha256:
            raise OutputValidationError("Layered-image manifest composite checksum is invalid")
        if sha256_file(result.composite_path) != result.composite_sha256:
            raise OutputValidationError("Layered-image composite checksum mismatch")

        entries = manifest.get("layers")
        if not isinstance(entries, list) or len(entries) != len(profile.palette.colors):
            raise OutputValidationError("Layered-image manifest must contain every palette slot")
        reconstructed = Image.new("RGBA", (result.width, result.height), (0, 0, 0, 0))
        actual_used: list[bool] = []
        for expected_index, (entry, layer) in enumerate(zip(entries, result.layers)):
            expected_path = f"layers/{expected_index:02d}.png"
            if not isinstance(entry, dict) or entry.get("index") != expected_index:
                raise OutputValidationError("Layered-image layer ordering is invalid")
            if entry.get("color") != profile.palette.colors[expected_index]:
                raise OutputValidationError("Layered-image layer color does not match its palette slot")
            if entry.get("image") != expected_path or not _safe_member(str(entry.get("image", ""))):
                raise OutputValidationError("Layered-image layer path is unsafe or unexpected")
            if layer.raster_path.resolve() != (package_dir / expected_path).resolve():
                raise OutputValidationError("Layered-image layer source is inconsistent")
            if not layer.raster_path.is_file() or sha256_file(layer.raster_path) != entry.get("sha256"):
                raise OutputValidationError("Layered-image layer checksum mismatch")
            try:
                layer_image = Image.open(layer.raster_path).convert("RGBA")
                layer_image.load()
            except (UnidentifiedImageError, OSError) as exc:
                raise OutputValidationError("Layered-image layer is not a decodable PNG") from exc
            if layer_image.size != (result.width, result.height):
                raise OutputValidationError("Layered-image layer dimensions do not match the canvas")
            allowed_rgb = hex_to_rgb(layer.color)
            used = False
            for red, green, blue, alpha in pixel_data(layer_image):
                if alpha:
                    used = True
                    if alpha != 255 or (red, green, blue) != allowed_rgb:
                        raise OutputValidationError(
                            "Layered-image layer contains an unexpected color or alpha"
                        )
            if used != bool(entry.get("used")) or used != layer.used:
                raise OutputValidationError("Layered-image layer used flag is incorrect")
            actual_used.append(used)
            reconstructed = Image.alpha_composite(reconstructed, layer_image)

        with Image.open(result.composite_path) as source:
            composite = source.convert("RGBA")
            composite.load()
        if reconstructed.tobytes() != composite.tobytes():
            raise OutputValidationError("Layered-image layers do not reconstruct the composite")
        palette_entries = manifest.get("palette")
        if not isinstance(palette_entries, list) or len(palette_entries) != len(profile.palette.colors):
            raise OutputValidationError("Layered-image palette slots are invalid")
        for index, item in enumerate(palette_entries):
            expected = {"index": index, "color": profile.palette.colors[index], "used": actual_used[index]}
            if item != expected:
                raise OutputValidationError("Layered-image palette metadata is inconsistent")

    def validate_zip(self, path: Path, expected_sha256: str, palette_size: int) -> None:
        if not path.is_file() or sha256_file(path) != expected_sha256:
            raise OutputValidationError("Layered-image ZIP checksum mismatch")
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if any(not _safe_member(name) for name in names):
                    raise OutputValidationError("Layered-image ZIP contains an unsafe path")
                required = {"manifest.json", "composite.png"}
                required.update(f"layers/{index:02d}.png" for index in range(palette_size))
                if set(names) != required:
                    raise OutputValidationError("Layered-image ZIP contents do not match the contract")
                if archive.testzip() is not None:
                    raise OutputValidationError("Layered-image ZIP contains a corrupt member")
        except zipfile.BadZipFile as exc:
            raise OutputValidationError("Layered-image output is not a valid ZIP") from exc
