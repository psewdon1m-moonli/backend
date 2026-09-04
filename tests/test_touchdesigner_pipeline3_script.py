from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATION_SCRIPT = ROOT / "integrations" / "touchdesigner" / "pipeline3_generation.py"
TRANSCRIPTION_SCRIPT = ROOT / "integrations" / "touchdesigner" / "pipeline3_transcription.py"


def test_touchdesigner_generation_replacement_preserves_runtime_contract() -> None:
    source = GENERATION_SCRIPT.read_text(encoding="utf-8")
    ast.parse(source)

    assert 'API_BASE_URL = "https://moonli.shmoza.net"' in source
    assert 'target_op = op("answer")' in source
    assert 'op("../answer")' not in source
    assert 'op("index").par.value0 += 1' in source
    assert source.count('op("sfx_button").par.reloadpulse.pulse()') == 2
    assert source.count('op("/queue").appendRow') == 2
    assert 'operation_id = str(uuid.uuid4())' in source
    assert 'DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")' in source
    assert 'movie_in_name = f"moviefilein{index}"' in source
    assert 'EXPECTED_IMAGE_NAMES = ("image_1.jpg", "image_2.jpg", "image_3.jpg")' in source
    assert '"pipeline": "pipeline-3"' in source
    assert '"Accept": "application/zip"' in source


def test_touchdesigner_transcription_replacement_preserves_runtime_contract() -> None:
    source = TRANSCRIPTION_SCRIPT.read_text(encoding="utf-8")
    ast.parse(source)

    assert 'API_BASE_URL = "https://moonli.shmoza.net"' in source
    assert 'AUDIO_PATH = "Q:/projects/monli_table/MonliProj/voice.wav"' in source
    assert 'target_op = op("../answer")' in source
    assert '"pipeline", "pipeline-3"' in source
    assert '"Accept": "text/plain"' in source
    assert 'operation_id = str(uuid.uuid4())' in source
    assert 'DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")' in source
    assert '"op(args[0]).text = args[1]"' in source


def test_touchdesigner_pipeline_3_scripts_share_identity_and_configure_credentials() -> None:
    transcription = TRANSCRIPTION_SCRIPT.read_text(encoding="utf-8")
    generation = GENERATION_SCRIPT.read_text(encoding="utf-8")

    shared_contracts = (
        'API_BASE_URL = "https://moonli.shmoza.net"',
        'DEVICE_DIRECTORY = os.path.join(project.folder, ".moonli")',
        'DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")',
        'DEVICE_ID_PATTERN = re.compile(r"^td-[0-9]{8}$")',
    )
    for contract in shared_contracts:
        assert contract in transcription
        assert contract in generation

    assert 'API_KEY = "PASTE_MOONLI_ACCESS_KEY_HERE".strip()' in transcription
    assert (
        'os.getenv("MOONLI_ACCESS_KEY", "PASTE_MOONLI_ACCESS_KEY_HERE")'
        in generation
    )
