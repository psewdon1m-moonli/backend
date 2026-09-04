from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.domain.images import GeneratedImage, PaletteValidatedImage
from app.domain.profiles import Palette, PipelineProfile
from app.services.processing.pixels import pixel_data


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


@dataclass(frozen=True)
class PaletteValidationResult:
    valid: bool
    image: PaletteValidatedImage | None
    invalid_pixels: int
    invalid_colors: tuple[str, ...]
    reason: str | None = None
    snapped_pixels: int = 0
    opaque_pixels: int = 0


class PaletteValidator:
    def __init__(self, snap_distance: float) -> None:
        self._snap_distance = snap_distance

    def validate(
        self,
        generated: GeneratedImage,
        palette: Palette,
        profile: PipelineProfile,
    ) -> PaletteValidationResult:
        try:
            source = Image.open(BytesIO(generated.content))
            source.load()
        except (UnidentifiedImageError, OSError) as exc:
            return PaletteValidationResult(False, None, 0, (), f"Image cannot be decoded: {exc}")
        if source.format != "PNG":
            return PaletteValidationResult(False, None, 0, (), "Provider result must be PNG")
        image = source.convert("RGBA")
        if image.size != (profile.width, profile.height):
            return PaletteValidationResult(
                False,
                None,
                0,
                (),
                f"Unexpected dimensions {image.width}x{image.height}",
            )

        allowed = tuple(hex_to_rgb(color) for color in palette.colors)
        allowed_set = set(allowed)
        invalid_count = 0
        invalid_alpha_count = 0
        snapped_count = 0
        opaque_count = 0
        invalid_colors: set[str] = set()
        output: list[tuple[int, int, int, int]] = []
        for red, green, blue, alpha in pixel_data(image):
            if alpha == 0:
                output.append((0, 0, 0, 0))
                continue
            if alpha != 255:
                invalid_alpha_count += 1
            opaque_count += 1
            rgb = (red, green, blue)
            if rgb in allowed_set:
                output.append((red, green, blue, alpha))
                continue
            nearest = min(allowed, key=lambda value: sum((value[i] - rgb[i]) ** 2 for i in range(3)))
            distance = math.sqrt(sum((nearest[i] - rgb[i]) ** 2 for i in range(3)))
            if distance <= self._snap_distance:
                output.append((*nearest, alpha))
                snapped_count += 1
            else:
                output.append((red, green, blue, alpha))
                invalid_count += 1
                if len(invalid_colors) < 20:
                    invalid_colors.add(f"#{red:02X}{green:02X}{blue:02X}")

        if invalid_count or invalid_alpha_count:
            return PaletteValidationResult(
                False,
                None,
                invalid_count + invalid_alpha_count,
                tuple(sorted(invalid_colors)),
                "Image contains colors or alpha values outside the allowed palette contract",
                snapped_pixels=snapped_count,
                opaque_pixels=opaque_count,
            )
        image.putdata(output)
        return PaletteValidationResult(
            True,
            PaletteValidatedImage(image=image, snapped_pixels=snapped_count, opaque_pixels=opaque_count),
            0,
            (),
            snapped_pixels=snapped_count,
            opaque_pixels=opaque_count,
        )
