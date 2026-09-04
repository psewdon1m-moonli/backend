from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from app.domain.images import PaletteValidatedImage
from app.domain.layers import Layer, LayeredImageResult
from app.domain.profiles import PipelineProfile
from app.services.processing.palette_validator import hex_to_rgb
from app.services.processing.pixels import pixel_data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LayerProcessor:
    def process(
        self,
        validated: PaletteValidatedImage,
        profile: PipelineProfile,
        package_dir: Path,
    ) -> LayeredImageResult:
        package_dir.mkdir(parents=True, exist_ok=True)
        layers_dir = package_dir / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        composite = validated.image.convert("RGBA")
        composite_path = package_dir / "composite.png"
        composite.save(composite_path, format="PNG", optimize=True)

        source_pixels = list(pixel_data(composite))
        layers: list[Layer] = []
        for index, color in enumerate(profile.palette.colors):
            rgb = hex_to_rgb(color)
            used = False
            pixels: list[tuple[int, int, int, int]] = []
            for red, green, blue, alpha in source_pixels:
                if alpha > 0 and (red, green, blue) == rgb:
                    pixels.append((red, green, blue, alpha))
                    used = True
                else:
                    pixels.append((0, 0, 0, 0))
            layer_image = Image.new("RGBA", composite.size, (0, 0, 0, 0))
            layer_image.putdata(pixels)
            layer_path = layers_dir / f"{index:02d}.png"
            layer_image.save(layer_path, format="PNG", optimize=True)
            layers.append(
                Layer(
                    palette_index=index,
                    color=color,
                    raster_path=layer_path,
                    used=used,
                    sha256=sha256_file(layer_path),
                )
            )
        return LayeredImageResult(
            composite_path=composite_path,
            composite_sha256=sha256_file(composite_path),
            palette=profile.palette.colors,
            layers=tuple(layers),
            width=composite.width,
            height=composite.height,
        )
