from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

from app.domain.images import PaletteValidatedImage
from app.domain.profiles import PipelineProfile
from app.services.processing.palette_validator import hex_to_rgb
from app.services.processing.pixels import pixel_data

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SVG_TAG = f"{{{SVG_NAMESPACE}}}svg"
GROUP_TAG = f"{{{SVG_NAMESPACE}}}g"
PATH_TAG = f"{{{SVG_NAMESPACE}}}path"


class PaletteVectorizationError(ValueError):
    pass


@dataclass(frozen=True)
class PaletteVectorizationResult:
    content: bytes
    run_count: int
    used_colors: int
    opaque_pixels: int


@dataclass(frozen=True)
class PaletteSegmentationResult:
    content: bytes
    used_layers: int
    total_layers: int


def _svg_document(
    profile: PipelineProfile,
    paths: tuple[str, ...],
    *,
    title: str,
) -> bytes:
    groups: list[str] = []
    for index, (color, path_data) in enumerate(zip(profile.palette.colors, paths)):
        path = (
            f'<path fill="{color}" shape-rendering="crispEdges" d="{path_data}"/>'
            if path_data
            else ""
        )
        groups.append(
            f'<g id="palette-{index:02d}" data-palette-index="{index}" '
            f'data-color="{color}">{path}</g>'
        )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="{SVG_NAMESPACE}" width="{profile.width}" height="{profile.height}" '
        f'viewBox="0 0 {profile.width} {profile.height}" '
        f'data-moonli-vector-contract="1.0" data-pipeline="{profile.id}" '
        f'data-palette-version="{profile.palette.id}">\n'
        f"<title>{title}</title>\n"
        + "\n".join(groups)
        + "\n</svg>\n"
    )
    return content.encode("utf-8")


class PaletteVectorizer:
    """Converts an exact-palette raster to deterministic, palette-grouped SVG paths."""

    def vectorize(
        self,
        validated: PaletteValidatedImage,
        profile: PipelineProfile,
    ) -> PaletteVectorizationResult:
        image = validated.image.convert("RGBA")
        if image.size != (profile.width, profile.height):
            raise PaletteVectorizationError("Image dimensions do not match the pipeline profile")

        palette_index = {
            hex_to_rgb(color): index for index, color in enumerate(profile.palette.colors)
        }
        paths: list[list[str]] = [[] for _ in profile.palette.colors]
        pixels = list(pixel_data(image))
        run_count = 0
        opaque_pixels = 0

        for y in range(profile.height):
            row_start = y * profile.width
            x = 0
            while x < profile.width:
                red, green, blue, alpha = pixels[row_start + x]
                if alpha == 0:
                    x += 1
                    continue
                if alpha != 255 or (red, green, blue) not in palette_index:
                    raise PaletteVectorizationError(
                        "Vectorization requires a strictly palette-valid PNG"
                    )
                index = palette_index[(red, green, blue)]
                run_start = x
                x += 1
                while x < profile.width:
                    next_red, next_green, next_blue, next_alpha = pixels[row_start + x]
                    if next_alpha != 255 or (next_red, next_green, next_blue) != (
                        red,
                        green,
                        blue,
                    ):
                        break
                    x += 1
                length = x - run_start
                opaque_pixels += length
                paths[index].append(f"M{run_start} {y}h{length}v1h-{length}z")
                run_count += 1

        joined_paths = tuple("".join(parts) for parts in paths)
        return PaletteVectorizationResult(
            content=_svg_document(profile, joined_paths, title="Moonli palette vector"),
            run_count=run_count,
            used_colors=sum(bool(path) for path in joined_paths),
            opaque_pixels=opaque_pixels,
        )


def segment_palette_svg(content: bytes, profile: PipelineProfile) -> PaletteSegmentationResult:
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, UnicodeDecodeError) as exc:
        raise PaletteVectorizationError("Vector input is not valid SVG") from exc
    if root.tag != SVG_TAG:
        raise PaletteVectorizationError("Vector input must be an SVG document")
    expected_root = {
        "width": str(profile.width),
        "height": str(profile.height),
        "viewBox": f"0 0 {profile.width} {profile.height}",
        "data-moonli-vector-contract": "1.0",
        "data-pipeline": profile.id,
        "data-palette-version": profile.palette.id,
    }
    if any(root.get(key) != value for key, value in expected_root.items()):
        raise PaletteVectorizationError("SVG contract does not match the selected pipeline")

    paths: list[str | None] = [None] * len(profile.palette.colors)
    for group in root.findall(GROUP_TAG):
        raw_index = group.get("data-palette-index", "")
        try:
            index = int(raw_index)
        except ValueError as exc:
            raise PaletteVectorizationError("SVG contains an invalid palette index") from exc
        if index < 0 or index >= len(paths) or paths[index] is not None:
            raise PaletteVectorizationError("SVG palette groups are incomplete or duplicated")
        if group.get("data-color") != profile.palette.colors[index]:
            raise PaletteVectorizationError("SVG palette group color does not match its slot")
        children = list(group)
        if len(children) > 1 or (children and children[0].tag != PATH_TAG):
            raise PaletteVectorizationError("SVG palette group contains unsupported elements")
        if children:
            path = children[0]
            if path.get("fill") != profile.palette.colors[index]:
                raise PaletteVectorizationError("SVG path color does not match its palette slot")
            paths[index] = path.get("d", "")
        else:
            paths[index] = ""
    if any(path is None for path in paths):
        raise PaletteVectorizationError("SVG must contain every palette slot")

    vector_paths = tuple(path or "" for path in paths)
    layers: list[tuple[str, bytes, bool]] = []
    for index, path_data in enumerate(vector_paths):
        isolated_paths = tuple(
            path_data if slot == index else "" for slot in range(len(vector_paths))
        )
        name = f"layers/{index:02d}.svg"
        layer = _svg_document(
            profile,
            isolated_paths,
            title=f"Moonli palette layer {index:02d}",
        )
        layers.append((name, layer, bool(path_data)))

    manifest = {
        "contract_version": "1.0",
        "pipeline": profile.id,
        "palette_version": profile.palette.id,
        "canvas": {"width": profile.width, "height": profile.height},
        "master": "master.svg",
        "layers": [
            {
                "index": index,
                "color": profile.palette.colors[index],
                "used": used,
                "image": name,
                "sha256": hashlib.sha256(layer).hexdigest(),
            }
            for index, (name, layer, used) in enumerate(layers)
        ],
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("master.svg", content)
        for name, layer, _ in layers:
            archive.writestr(name, layer)
    return PaletteSegmentationResult(
        content=output.getvalue(),
        used_layers=sum(bool(path) for path in vector_paths),
        total_layers=len(vector_paths),
    )
