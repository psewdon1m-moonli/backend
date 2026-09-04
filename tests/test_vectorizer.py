from pathlib import Path

from PIL import Image

from app.services.vectorizer import _can_merge_colors, build_vector_assets


def test_build_vector_assets_creates_master_and_layers(tmp_path: Path) -> None:
    pack_dir = tmp_path / "pack_demo"
    pack_dir.mkdir(parents=True, exist_ok=True)
    palette = ["#FF0000", "#00FF00", "#0000FF"]

    vector_assets = build_vector_assets(
        pack_dir=pack_dir,
        candidate_id="cand_123",
        palette=palette,
    )

    master_svg = pack_dir / vector_assets["master_svg"]
    assert master_svg.exists()
    assert "<svg" in master_svg.read_text(encoding="utf-8")
    assert len(vector_assets["color_layers"]) == len(palette)
    for entry in vector_assets["color_layers"]:
        layer_path = pack_dir / entry["path"]
        assert layer_path.exists()
        content = layer_path.read_text(encoding="utf-8")
        assert "<path" in content or "<rect" in content
        assert entry["color"] in content


def test_build_vector_assets_merges_similar_and_skips_white(tmp_path: Path) -> None:
    pack_dir = tmp_path / "pack_demo_img"
    pack_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "source.png"

    # 4x2 image: white background + two very close reds that must be merged.
    img = Image.new("RGBA", (4, 2), color=(255, 255, 255, 255))
    px = img.load()
    px[0, 0] = (220, 30, 30, 255)
    px[1, 0] = (224, 34, 34, 255)
    px[0, 1] = (220, 30, 30, 255)
    px[1, 1] = (224, 34, 34, 255)
    img.save(src)

    vector_assets = build_vector_assets(
        pack_dir=pack_dir,
        candidate_id="cand_img",
        palette=[],
        source_image_uri=str(src),
    )

    colors = [entry["color"].upper() for entry in vector_assets["color_layers"]]
    assert "#FFFFFF" not in colors
    assert len(colors) == 1


def test_can_merge_colors_wider_warm_ranges() -> None:
    assert _can_merge_colors((0xE8, 0x63, 0x8E), (0xE2, 0x6B, 0x66))
    assert _can_merge_colors((0xEA, 0x9F, 0x4E), (0xF4, 0xDC, 0x13))


def test_can_merge_colors_wider_cool_ranges() -> None:
    assert _can_merge_colors((0x2C, 0x50, 0x6D), (0x42, 0x93, 0xCB))
