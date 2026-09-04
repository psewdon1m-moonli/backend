from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageFilter, UnidentifiedImageError

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.services.processing.palette_validator import hex_to_rgb
from app.services.processing.pixels import pixel_data


class PaletteQuantizationError(ValueError):
    """Raised when an image cannot be converted to the selected palette contract."""


@dataclass(frozen=True)
class PaletteQuantizationResult:
    image: GeneratedImage
    changed_pixels: int
    cleanup_changed_pixels: int
    opaque_pixels: int
    transparent_pixels: int
    unique_colors_before: int
    unique_colors_after: int
    palette_counts: tuple[int, ...]
    cleanup_removed_components: int = 0


MAX_CLEANUP_COMPONENT_PIXELS = 64


def _remove_small_components(
    indices: bytearray,
    transparent_mask: bytearray,
    width: int,
    height: int,
    palette_size: int,
) -> int:
    """Replace tiny palette islands with the strict majority color around them."""
    visited = bytearray(transparent_mask)
    removed_components = 0
    for seed in range(len(indices)):
        if visited[seed]:
            continue
        color = indices[seed]
        visited[seed] = 1
        stack = [seed]
        component: list[int] = []
        boundary = [0] * palette_size
        while stack:
            position = stack.pop()
            component.append(position)
            x = position % width
            y = position // width
            neighbors: list[int] = []
            if x:
                neighbors.append(position - 1)
            if x + 1 < width:
                neighbors.append(position + 1)
            if y:
                neighbors.append(position - width)
            if y + 1 < height:
                neighbors.append(position + width)
            for neighbor in neighbors:
                if transparent_mask[neighbor]:
                    continue
                neighbor_color = indices[neighbor]
                if neighbor_color == color:
                    if not visited[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)
                else:
                    boundary[neighbor_color] += 1

        if len(component) > MAX_CLEANUP_COMPONENT_PIXELS:
            continue
        boundary_total = sum(boundary)
        if not boundary_total:
            continue
        replacement = max(range(palette_size), key=boundary.__getitem__)
        if boundary[replacement] * 2 <= boundary_total:
            continue
        for position in component:
            indices[position] = replacement
        removed_components += 1
    return removed_components


def _srgb_channel_to_linear(value: int) -> float:
    channel = value / 255.0
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red = _srgb_channel_to_linear(rgb[0])
    green = _srgb_channel_to_linear(rgb[1])
    blue = _srgb_channel_to_linear(rgb[2])

    # sRGB -> XYZ, D65 reference white.
    x = (red * 0.4124564 + green * 0.3575761 + blue * 0.1804375) / 0.95047
    y = red * 0.2126729 + green * 0.7151522 + blue * 0.0721750
    z = (red * 0.0193339 + green * 0.1191920 + blue * 0.9503041) / 1.08883

    epsilon = 216 / 24389
    kappa = 24389 / 27

    def pivot(value: float) -> float:
        if value > epsilon:
            return value ** (1 / 3)
        return (kappa * value + 16) / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


class PaletteQuantizer:
    """Maps every visible pixel to a fixed palette using CIE76 distance in Lab."""

    def __init__(self, cleanup_passes: int = 1) -> None:
        if cleanup_passes < 0 or cleanup_passes > 3:
            raise ValueError("cleanup_passes must be between 0 and 3")
        self._cleanup_passes = cleanup_passes

    def quantize(
        self,
        generated: GeneratedImage,
        palette: Palette,
        profile: PipelineProfile,
    ) -> PaletteQuantizationResult:
        try:
            source = Image.open(BytesIO(generated.content))
            source.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise PaletteQuantizationError(f"Image cannot be decoded: {exc}") from exc
        if source.size != (profile.width, profile.height):
            raise PaletteQuantizationError(
                f"Unexpected dimensions {source.width}x{source.height}; "
                f"expected {profile.width}x{profile.height}"
            )

        image = source.convert("RGBA")
        pixels = list(pixel_data(image))
        palette_rgb = tuple(hex_to_rgb(color) for color in palette.colors)
        palette_lab = tuple(_rgb_to_lab(color) for color in palette_rgb)
        nearest_cache: dict[tuple[int, int, int], int] = {
            color: index for index, color in enumerate(palette_rgb)
        }
        before_colors: set[tuple[int, int, int]] = set()
        indices = bytearray(len(pixels))
        original_indices = bytearray(len(pixels))
        transparent_mask = bytearray(len(pixels))
        changed_pixels = 0
        opaque_pixels = 0
        transparent_pixels = 0

        for position, (red, green, blue, alpha) in enumerate(pixels):
            if alpha == 0:
                indices[position] = 255
                original_indices[position] = 255
                transparent_mask[position] = 1
                transparent_pixels += 1
                continue

            opaque_pixels += 1
            rgb = (red, green, blue)
            before_colors.add(rgb)
            palette_index = nearest_cache.get(rgb)
            if palette_index is None:
                lab = _rgb_to_lab(rgb)
                palette_index = min(
                    range(len(palette_lab)),
                    key=lambda index: sum(
                        (lab[channel] - palette_lab[index][channel]) ** 2
                        for channel in range(3)
                    ),
                )
                nearest_cache[rgb] = palette_index
            indices[position] = palette_index
            original_indices[position] = palette_index
            if rgb != palette_rgb[palette_index] or alpha != 255:
                changed_pixels += 1

        cleanup_changed_pixels = 0
        cleanup_removed_components = 0
        if self._cleanup_passes and opaque_pixels:
            index_image = Image.frombytes("L", image.size, bytes(indices))
            for _ in range(self._cleanup_passes):
                filtered = bytearray(index_image.filter(ImageFilter.ModeFilter(size=3)).tobytes())
                for position in range(len(filtered)):
                    if transparent_mask[position]:
                        filtered[position] = 255
                    elif filtered[position] == 255:
                        filtered[position] = indices[position]
                indices = filtered
                index_image = Image.frombytes("L", image.size, bytes(indices))
            for _ in range(self._cleanup_passes):
                cleanup_removed_components += _remove_small_components(
                    indices,
                    transparent_mask,
                    image.width,
                    image.height,
                    len(palette_rgb),
                )
            cleanup_changed_pixels = sum(
                1
                for position, palette_index in enumerate(indices)
                if not transparent_mask[position] and palette_index != original_indices[position]
            )

        palette_counts = [0] * len(palette_rgb)
        output_pixels: list[tuple[int, int, int, int]] = []
        for palette_index in indices:
            if palette_index == 255:
                output_pixels.append((0, 0, 0, 0))
            else:
                palette_counts[palette_index] += 1
                output_pixels.append((*palette_rgb[palette_index], 255))

        output = Image.new("RGBA", image.size)
        output.putdata(output_pixels)
        buffer = BytesIO()
        output.save(buffer, format="PNG", optimize=True)
        return PaletteQuantizationResult(
            image=GeneratedImage(buffer.getvalue(), "image/png", generated.provider),
            changed_pixels=changed_pixels,
            cleanup_changed_pixels=cleanup_changed_pixels,
            opaque_pixels=opaque_pixels,
            transparent_pixels=transparent_pixels,
            unique_colors_before=len(before_colors),
            unique_colors_after=sum(count > 0 for count in palette_counts),
            palette_counts=tuple(palette_counts),
            cleanup_removed_components=cleanup_removed_components,
        )
