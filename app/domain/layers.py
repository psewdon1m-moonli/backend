from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layer:
    palette_index: int
    color: str
    raster_path: Path
    used: bool
    sha256: str


@dataclass(frozen=True)
class LayeredImageResult:
    composite_path: Path
    composite_sha256: str
    palette: tuple[str, ...]
    layers: tuple[Layer, ...]
    width: int
    height: int
