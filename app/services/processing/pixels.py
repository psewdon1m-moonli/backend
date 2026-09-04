from __future__ import annotations

from collections.abc import Iterable

from PIL import Image


def pixel_data(image: Image.Image) -> Iterable[tuple[int, ...]]:
    """Use Pillow's current pixel iterator while retaining Pillow 10 compatibility."""
    if hasattr(image, "get_flattened_data"):
        return image.get_flattened_data()
    return image.getdata()
