from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    media_type: str
    provider: str


@dataclass(frozen=True)
class ImageAsset:
    asset_id: str
    path: Path
    media_type: str
    sha256: str
    width: int
    height: int


@dataclass
class PaletteValidatedImage:
    image: Image.Image
    snapped_pixels: int
    opaque_pixels: int
