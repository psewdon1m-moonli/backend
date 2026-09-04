from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
OutputMode = Literal["full_image", "layered_image"]


@dataclass(frozen=True)
class Palette:
    id: str
    version: int
    colors: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = tuple(color.upper() for color in self.colors)
        if not normalized or any(not HEX_COLOR.fullmatch(color) for color in normalized):
            raise ValueError(f"Palette {self.id!r} contains an invalid color")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"Palette {self.id!r} contains duplicate colors")
        object.__setattr__(self, "colors", normalized)


@dataclass(frozen=True)
class PipelineProfile:
    id: str
    output_mode: OutputMode
    palette: Palette
    width: int
    height: int
    visual_constraints: tuple[str, ...]


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Missing configuration file: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Configuration must be a mapping: {path}")
    return payload


def load_profiles(config_dir: Path) -> dict[str, PipelineProfile]:
    root = config_dir.resolve()
    payload = _read_yaml(root / "profiles.yaml")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("profiles.yaml must contain a non-empty profiles mapping")

    profiles: dict[str, PipelineProfile] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise TypeError(f"Profile {profile_id!r} must be a mapping")
        palette_path = (root / str(raw.get("palette", ""))).resolve()
        if root not in palette_path.parents:
            raise ValueError(f"Palette path escapes config directory for {profile_id!r}")
        palette_raw = _read_yaml(palette_path)
        palette = Palette(
            id=str(palette_raw.get("id", "")),
            version=int(palette_raw.get("version", 0)),
            colors=tuple(str(color) for color in palette_raw.get("colors", [])),
        )
        canvas = raw.get("canvas") or {}
        width = int(canvas.get("width", 0))
        height = int(canvas.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError(f"Profile {profile_id!r} has invalid canvas dimensions")
        output_mode = str(raw.get("output_mode", ""))
        if output_mode not in {"full_image", "layered_image"}:
            raise ValueError(f"Profile {profile_id!r} has invalid output_mode")
        profiles[str(profile_id)] = PipelineProfile(
            id=str(profile_id),
            output_mode=output_mode,  # type: ignore[arg-type]
            palette=palette,
            width=width,
            height=height,
            visual_constraints=tuple(str(item) for item in raw.get("visual_constraints", [])),
        )
    return profiles
