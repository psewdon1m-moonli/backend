from __future__ import annotations

from io import BytesIO

from PIL import Image

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.services.processing.palette_validator import PaletteValidator


def _profile() -> PipelineProfile:
    return PipelineProfile(
        id="test",
        output_mode="full_image",
        palette=Palette(id="test_v1", version=1, colors=("#FF0000", "#FFFFFF")),
        width=2,
        height=2,
        visual_constraints=(),
    )


def _generated(pixels: list[tuple[int, int, int, int]]) -> GeneratedImage:
    image = Image.new("RGBA", (2, 2))
    image.putdata(pixels)
    output = BytesIO()
    image.save(output, format="PNG")
    return GeneratedImage(output.getvalue(), "image/png", "test")


def test_palette_validator_accepts_exact_and_unused_palette_colors() -> None:
    generated = _generated([(255, 0, 0, 255)] * 4)
    result = PaletteValidator(snap_distance=4).validate(generated, _profile().palette, _profile())
    assert result.valid is True
    assert result.image is not None
    assert result.image.snapped_pixels == 0


def test_palette_validator_snaps_only_near_color_technical_deviation() -> None:
    generated = _generated([(254, 2, 1, 255)] * 4)
    result = PaletteValidator(snap_distance=4).validate(generated, _profile().palette, _profile())
    assert result.valid is True
    assert result.image is not None
    assert result.image.snapped_pixels == 4
    assert result.image.image.getcolors(maxcolors=4) == [(4, (255, 0, 0, 255))]


def test_palette_validator_rejects_illegal_color_and_gradient() -> None:
    illegal = _generated([(0, 0, 255, 255), (255, 0, 0, 255), (255, 255, 255, 255), (255, 0, 0, 255)])
    gradient = _generated([(255, 0, 0, 255), (192, 0, 0, 255), (128, 0, 0, 255), (64, 0, 0, 255)])
    validator = PaletteValidator(snap_distance=4)
    assert validator.validate(illegal, _profile().palette, _profile()).valid is False
    assert validator.validate(gradient, _profile().palette, _profile()).valid is False


def test_palette_validator_ignores_fully_transparent_rgb_values() -> None:
    generated = _generated([(0, 255, 0, 0), (255, 0, 0, 255), (255, 255, 255, 255), (255, 0, 0, 255)])
    result = PaletteValidator(snap_distance=4).validate(generated, _profile().palette, _profile())
    assert result.valid is True
    assert result.image is not None
    assert result.image.image.getpixel((0, 0)) == (0, 0, 0, 0)
