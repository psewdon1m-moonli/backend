from __future__ import annotations

from io import BytesIO

from PIL import Image

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.services.processing.palette_quantizer import PaletteQuantizer
from app.services.processing.palette_validator import PaletteValidator, hex_to_rgb
from app.services.processing.pixels import pixel_data


def _profile(colors: tuple[str, ...], size: int = 8) -> PipelineProfile:
    return PipelineProfile(
        id="pipeline-1",
        output_mode="full_image",
        palette=Palette("test_palette_v1", 1, colors),
        width=size,
        height=size,
        visual_constraints=(),
    )


def _generated(image: Image.Image) -> GeneratedImage:
    output = BytesIO()
    image.save(output, format="PNG")
    return GeneratedImage(output.getvalue(), "image/png", "test")


def test_quantizer_maps_visible_pixels_to_exact_lab_palette_and_preserves_transparency() -> None:
    profile = _profile(("#4A9AD4", "#FF1F2D", "#000000", "#FFFFFF"))
    source = Image.new("RGBA", (8, 8), (76, 150, 197, 255))
    source.putpixel((0, 0), (20, 200, 50, 128))
    source.putpixel((7, 7), (1, 2, 3, 0))

    result = PaletteQuantizer(cleanup_passes=0).quantize(
        _generated(source), profile.palette, profile
    )
    decoded = Image.open(BytesIO(result.image.content)).convert("RGBA")
    visible = {pixel[:3] for pixel in pixel_data(decoded) if pixel[3]}

    assert visible <= {hex_to_rgb(color) for color in profile.palette.colors}
    assert decoded.getpixel((7, 7)) == (0, 0, 0, 0)
    assert decoded.getpixel((0, 0))[3] == 255
    assert result.transparent_pixels == 1
    assert result.changed_pixels > 0
    assert PaletteValidator(0).validate(result.image, profile.palette, profile).valid


def test_cleanup_removes_an_isolated_palette_color_without_inventing_colors() -> None:
    profile = _profile(("#000000", "#FFFFFF"), size=5)
    source = Image.new("RGBA", (5, 5), (0, 0, 0, 255))
    source.putpixel((2, 2), (255, 255, 255, 255))

    result = PaletteQuantizer(cleanup_passes=1).quantize(
        _generated(source), profile.palette, profile
    )
    decoded = Image.open(BytesIO(result.image.content)).convert("RGBA")

    assert decoded.getpixel((2, 2)) == (0, 0, 0, 255)
    assert result.cleanup_changed_pixels == 1
    assert result.unique_colors_after == 1
    assert PaletteValidator(0).validate(result.image, profile.palette, profile).valid


def test_cleanup_removes_small_color_islands_but_preserves_large_regions() -> None:
    profile = _profile(("#000000", "#FFFFFF"), size=24)
    source = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    for y in range(5, 8):
        for x in range(5, 8):
            source.putpixel((x, y), (0, 0, 0, 255))
    for y in range(24):
        for x in range(15, 20):
            source.putpixel((x, y), (0, 0, 0, 255))

    result = PaletteQuantizer(cleanup_passes=1).quantize(
        _generated(source), profile.palette, profile
    )
    decoded = Image.open(BytesIO(result.image.content)).convert("RGBA")

    assert decoded.getpixel((6, 6)) == (255, 255, 255, 255)
    assert decoded.getpixel((17, 12)) == (0, 0, 0, 255)
    assert result.cleanup_removed_components >= 1
    assert PaletteValidator(0).validate(result.image, profile.palette, profile).valid
