from app.presets import PALETTE_PRESETS


def test_palette_presets_are_valid() -> None:
    assert PALETTE_PRESETS
    for colors in PALETTE_PRESETS.values():
        assert 1 <= len(colors) <= 6
        for color in colors:
            assert isinstance(color, str)
            assert len(color) == 7
            assert color.startswith("#")
            int(color[1:], 16)

