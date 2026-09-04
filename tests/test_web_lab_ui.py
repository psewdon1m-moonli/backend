from __future__ import annotations

from pathlib import Path


def test_lab_stage_cards_follow_the_pipeline_and_expose_result_actions() -> None:
    html = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    headings = (
        "Transcription · optional",
        "Prompt normalization",
        "Prompt builder",
        "Image generation",
        "Palette quantization",
        "Palette validation",
        "Vectorization",
        "Segmentation",
    )
    positions = [html.index(f"<h2>{heading}</h2>") for heading in headings]

    assert positions == sorted(positions)
    assert html.index("<h2>Full Run</h2>") < positions[0]
    assert "8 stages" in html
    for control_id in (
        "copyTranscriptionResult",
        "transcriptionToNormalization",
        "copyNormalizationResult",
        "normalizationToPrompt",
        "copyPromptResult",
        "promptToImage",
        "copyPaletteResult",
        "paletteToVectorization",
        "vectorizationStageForm",
        "segmentationStageForm",
    ):
        assert f'id="{control_id}"' in html


def test_full_run_lives_in_test_call_view_and_uses_configuration_defaults() -> None:
    html = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert '<span>Test Calls</span><span class="nav-ordinal">02</span>' in html
    assert 'data-view="pipeline"' not in html
    assert 'id="view-pipeline"' not in html
    assert '<header class="card-header no-ordinal"><h2>Full Run</h2>' in html
    assert 'id="pipelineInputType"' in html
    assert 'id="pipelineTag"' in html
    for removed_control in (
        "pipelineImageProvider",
        "pipelineTranscriptionProvider",
        "pipelineNormalizationProvider",
        "pipelineTemplateMode",
        "pipelineCleanupPasses",
        "pipelineAttempts",
    ):
        assert f'id="{removed_control}"' not in html
    for settings_control in (
        "settingsImageProvider",
        "settingsTranscriptionProvider",
        "settingsNormalizationProvider",
        "settingsCleanupPasses",
        "settingsGenerationAttempts",
    ):
        assert f'id="{settings_control}"' in html


def test_overview_contains_only_system_and_production_usage_cards() -> None:
    html = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    overview = html.split('<section id="view-overview"', 1)[1].split("</section>", 1)[0]

    for control_id in (
        "overviewCpu",
        "overviewRam",
        "overviewDisk",
        "overviewUptime",
        "overviewRequestCount",
        "overviewTokenCount",
        "productionUsageChart",
    ):
        assert f'id="{control_id}"' in overview
    assert "Quick start" not in overview
    assert "pipeline-1" not in overview
    assert "Providers" not in overview
    assert '<title>Moonli</title>' in html
    assert "halftone-moon-alpha.png" in html


def test_login_uses_the_saved_project_accent_before_authentication() -> None:
    root = Path(__file__).parents[1]
    html = (root / "app" / "web" / "index.html").read_text(encoding="utf-8")
    css = (root / "app" / "web" / "styles.css").read_text(encoding="utf-8")
    javascript = (root / "app" / "web" / "app.js").read_text(encoding="utf-8")

    assert 'class="login-wordmark">Moonli</span>' in html
    assert ".login-wordmark" in css
    assert "color: var(--accent);" in css
    assert "function applyStoredAccent()" in javascript
    assert javascript.index("applyStoredAccent();") < javascript.index("bindEvents();")


def test_devices_view_has_collapsible_cards_and_block_controls() -> None:
    root = Path(__file__).parents[1]
    html = (root / "app" / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app" / "web" / "app.js").read_text(encoding="utf-8")

    assert '<span>Devices</span><span class="nav-ordinal">05</span>' in html
    assert 'id="view-devices"' in html
    assert 'id="deviceList"' in html
    assert 'id="refreshDevices"' in html
    assert 'document.createElement("details")' in javascript
    assert '"Block"' in javascript
    assert '"Unblock"' in javascript
    assert 'apiCall("/internal/devices?limit=500")' in javascript


def test_documentation_is_a_full_english_navigation_view() -> None:
    root = Path(__file__).parents[1]
    html = (root / "app" / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app" / "web" / "app.js").read_text(encoding="utf-8")
    operations = (root / "app" / "web" / "operations.html").read_text(encoding="utf-8")

    assert '<span>Documentation</span><span class="nav-ordinal">07</span>' in html
    assert 'id="view-documentation"' in html
    for contract in (
        "POST /v1/generate",
        "X-Moonli-Device-Id",
        "pipeline-1",
        "pipeline-2",
        "moonli-logical-backup",
        "Nginx",
        "Android and TouchDesigner integration",
    ):
        assert contract in html
        assert contract in operations

    assert '<html lang="en">' in html
    assert '<html lang="en">' in operations
    assert not any("\u0400" <= char <= "\u04FF" for char in html + javascript + operations)
