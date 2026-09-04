from __future__ import annotations

import ast
import re
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
    assert "ENABLE_TABLE_ACTIONS" not in source
    assert "Table actions are disabled" not in source
    assert source.count('op("sfx_button").par.reloadpulse.pulse()') == 2
    assert source.count('op("/queue").appendRow') == 2
    assert 'operation_id = str(uuid.uuid4())' in source
    assert 'DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")' in source
    assert "IMAGE_SAVE_DIR = os.path.join(" in source
    assert 'f"t{int(parent().digits)}"' in source
    assert "directory = IMAGE_SAVE_DIR" in source
    assert "IMAGE_SAVE_BASE" not in source
    assert '"/AI_SCRIPT2/moviefilein1"' in source
    assert '"/AI_SCRIPT2/moviefilein2"' in source
    assert '"/AI_SCRIPT2/moviefilein3"' in source
    assert '"/AI_SCRIPT2/moviefilein4"' not in source
    assert '"/Table_1/AI_SCRIPT2/' not in source
    assert 'movie_in_path = MOVIE_FILE_IN_PATHS[index - 1]' in source
    assert 'EXPECTED_IMAGE_NAMES = ("image_1.jpg", "image_2.jpg", "image_3.jpg")' in source
    assert '"pipeline": "pipeline-3"' in source
    assert '"Accept": "application/zip"' in source


def test_touchdesigner_transcription_replacement_preserves_runtime_contract() -> None:
    source = TRANSCRIPTION_SCRIPT.read_text(encoding="utf-8")
    ast.parse(source)

    assert 'API_BASE_URL = "https://moonli.shmoza.net"' in source
    assert "def _find_monli_project_directory" not in source
    assert "MONLI_PROJECT_DIRECTORY" not in source
    assert "AUDIO_PATH = os.path.join(" in source
    assert 'f"t{int(parent(2).digits)}"' in source
    assert '"voice.wav"' in source
    assert 'target_op = op("../answer")' in source
    assert '"pipeline", "pipeline-3"' in source
    assert '"Accept": "text/plain"' in source
    assert 'TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())' in source
    assert 'context=TLS_CONTEXT' in source
    assert 'operation_id = str(uuid.uuid4())' in source
    assert 'DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")' in source
    assert '"op(args[0]).text = args[1]"' in source
    assert 'if code == "NO_VISUAL_SUBJECT":' in source
    assert '"Say what you want to draw."' in source
    assert "if channel is not None and channel.index > 0:" in source
    assert 'op("../index").par.value0 += 1' in source


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

    key_pattern = re.compile(r'^API_KEY = "([^"]+)"\.strip\(\)$', re.MULTILINE)
    transcription_key = key_pattern.search(transcription)
    generation_key = key_pattern.search(generation)
    assert transcription_key is not None
    assert generation_key is not None
    assert transcription_key.group(1) == generation_key.group(1)
    assert len(transcription_key.group(1)) >= 32
    assert not transcription_key.group(1).startswith("PASTE_")
    assert 'API_KEY.startswith("PASTE_")' in transcription
    assert 'API_KEY.startswith("PASTE_")' in generation
