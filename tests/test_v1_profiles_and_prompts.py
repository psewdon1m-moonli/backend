from __future__ import annotations

from app.domain.profiles import load_profiles
from app.services.prompts import PromptBuilder
from app.settings import BASE_DIR


def test_versioned_profiles_have_fixed_palette_contracts() -> None:
    profiles = load_profiles(BASE_DIR / "config")
    assert set(profiles) == {"pipeline-1", "pipeline-2"}
    assert profiles["pipeline-1"].output_mode == "full_image"
    assert profiles["pipeline-2"].output_mode == "layered_image"
    assert len(profiles["pipeline-1"].palette.colors) == 6
    assert len(profiles["pipeline-2"].palette.colors) == 12


def test_prompt_builder_compresses_long_text_without_losing_constraints() -> None:
    profile = load_profiles(BASE_DIR / "config")["pipeline-2"]
    repeated = "Мы сегодня много говорили о море и вспоминали спокойный вечер. " * 30
    source = (
        repeated
        + "На маленьком острове стоит дом. "
        + "Самое главное, чтобы большое дерево было красным. "
        + "Людей точно не должно быть."
    )
    result = PromptBuilder().build(source, profile)

    assert result.brief.compressed is True
    assert any("дерево" in item for item in result.brief.must_include)
    assert any("Людей" in item for item in result.brief.must_avoid)
    assert len(result.text) < len(source)
    assert "large coherent color regions" in result.text
    assert all(color in result.text for color in profile.palette.colors)


def test_prompt_builder_preserves_conflicting_explicit_constraints_for_traceability() -> None:
    profile = load_profiles(BASE_DIR / "config")["pipeline-1"]
    result = PromptBuilder().build(
        "The scene must include a red tree. The scene must not include a red tree.", profile
    )
    assert result.brief.must_include == ("The scene must include a red tree.",)
    assert result.brief.must_avoid == ("The scene must not include a red tree.",)
