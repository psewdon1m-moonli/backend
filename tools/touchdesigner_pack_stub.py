from __future__ import annotations

import json
from pathlib import Path


def load_pack(pack_path: str) -> dict:
    root = Path(pack_path)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vector = manifest.get("vector_assets", {})
    return {
        "pack_id": manifest.get("pack_id"),
        "steps_count": len(manifest.get("steps", [])),
        "master_svg": root / vector.get("master_svg", ""),
        "color_layers": [root / item["path"] for item in vector.get("color_layers", [])],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TouchDesigner import stub for content pack")
    parser.add_argument("pack_path", help="Path to pack directory")
    args = parser.parse_args()
    result = load_pack(args.pack_path)
    print(f"pack_id: {result['pack_id']}")
    print(f"steps_count: {result['steps_count']}")
    print(f"master_svg: {result['master_svg']}")
    print("color_layers:")
    for path in result["color_layers"]:
        print(f"  {path}")

