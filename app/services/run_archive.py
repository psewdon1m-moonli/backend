from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

from app.domain.inputs import GenerationInput
from app.domain.profiles import PipelineProfile

RUN_ARCHIVE_MEDIA_TYPE = "application/vnd.moonli.run-artifacts+zip"
_ATTEMPT = re.compile(r"_(\d+)\.")


@dataclass(frozen=True)
class RunArchive:
    content: bytes
    filename: str
    media_type: str
    sha256: str


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_audio_name(value: str) -> str:
    name = Path(value).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return safe or "request-audio.bin"


def _attempt_index(path: Path) -> int:
    match = _ATTEMPT.search(path.name)
    return int(match.group(1)) if match else -1


def _latest(directory: Path, pattern: str) -> Path:
    candidates = sorted(directory.glob(pattern), key=_attempt_index)
    if not candidates:
        raise FileNotFoundError(f"Missing run artifact: {pattern}")
    return candidates[-1]


def _add_nested_vector_layers(entries: dict[str, bytes], path: Path) -> list[str]:
    layer_entries: list[str] = []
    with zipfile.ZipFile(path) as nested:
        vector_manifest: dict[str, object] | None = None
        for member in nested.namelist():
            pure = PurePosixPath(member)
            if pure.is_absolute() or ".." in pure.parts or "\\" in member:
                raise ValueError("Unsafe vector layer archive member")
            if member == "manifest.json":
                vector_manifest = json.loads(nested.read(member))
                continue
            elif member.startswith("layers/") and member.endswith(".svg"):
                target = f"layers/vector/{pure.name}"
                layer_entries.append(target)
            else:
                continue
            entries[target] = nested.read(member)
    if vector_manifest is None:
        raise ValueError("Vector layer archive does not contain a manifest")
    vector_manifest["master"] = "vector/master.svg"
    for layer in vector_manifest.get("layers", []):
        if isinstance(layer, dict) and "image" in layer:
            layer["image"] = f"layers/vector/{PurePosixPath(str(layer['image'])).name}"
    entries["layers/vector/manifest.json"] = _json_bytes(vector_manifest)
    return sorted(layer_entries)


def _add_raster_layers(entries: dict[str, bytes], package_dir: Path) -> list[str]:
    layer_entries: list[str] = []
    for source in sorted((package_dir / "layers").glob("*.png")):
        target = f"layers/raster/{source.name}"
        entries[target] = source.read_bytes()
        layer_entries.append(target)
    raster_manifest = json.loads((package_dir / "manifest.json").read_bytes())
    raster_manifest["composite"] = "layers/raster/composite.png"
    for layer in raster_manifest.get("layers", []):
        if isinstance(layer, dict) and "image" in layer:
            layer["image"] = f"layers/raster/{PurePosixPath(str(layer['image'])).name}"
    entries["layers/raster/manifest.json"] = _json_bytes(raster_manifest)
    entries["layers/raster/composite.png"] = (package_dir / "composite.png").read_bytes()
    return layer_entries


def build_run_archive(
    *,
    run_id: str,
    completed_dir: Path,
    profile: PipelineProfile,
    generation_input: GenerationInput,
    trace: dict[str, object],
    image_provider: str,
    transcription_provider: str,
    normalization_provider: str,
    final_media_type: str,
) -> RunArchive:
    entries: dict[str, bytes] = {}
    stage_artifacts: dict[str, list[str]] = {}

    if generation_input.type == "text":
        assert generation_input.text is not None
        input_path = "input/request.txt"
        entries[input_path] = generation_input.text.text.encode("utf-8")
    else:
        assert generation_input.audio is not None
        input_path = f"input/{_safe_audio_name(generation_input.audio.filename)}"
        entries[input_path] = generation_input.audio.content
    stage_artifacts["input"] = [input_path]

    if generation_input.type == "audio":
        transcription = str(trace.get("transcription") or "")
        entries["text/transcription.txt"] = transcription.encode("utf-8")
        stage_artifacts["transcription"] = ["text/transcription.txt"]
    else:
        stage_artifacts["transcription"] = []

    entries["text/normalized.txt"] = str(trace.get("normalized_text") or "").encode("utf-8")
    stage_artifacts["normalization"] = ["text/normalized.txt"]
    entries["prompt/prompt.txt"] = str(trace.get("prompt") or "").encode("utf-8")
    entries["prompt/visual-brief.json"] = _json_bytes(trace.get("visual_brief"))
    stage_artifacts["prompt_building"] = [
        "prompt/prompt.txt",
        "prompt/visual-brief.json",
    ]

    generated = _latest(completed_dir, "provider_attempt_*.png")
    quantized = _latest(completed_dir, "quantized_attempt_*.png")
    entries["images/generated.png"] = generated.read_bytes()
    entries["images/quantized.png"] = quantized.read_bytes()
    entries["images/validated.png"] = (completed_dir / "source.png").read_bytes()
    stage_artifacts["image_generation"] = ["images/generated.png"]
    stage_artifacts["quantization"] = ["images/quantized.png"]

    quantization_report = _latest(completed_dir, "quantization_attempt_*.json")
    validation_report = completed_dir / "palette_validation.json"
    entries["reports/quantization.json"] = quantization_report.read_bytes()
    entries["reports/palette-validation.json"] = validation_report.read_bytes()
    entries["reports/execution-trace.json"] = _json_bytes(trace.get("execution_trace") or [])
    stage_artifacts["quantization"].append("reports/quantization.json")
    stage_artifacts["validation"] = [
        "images/validated.png",
        "reports/palette-validation.json",
    ]

    for source in sorted(completed_dir.glob("provider_attempt_*.png"), key=_attempt_index):
        attempt = _attempt_index(source)
        entries[f"attempts/generation/generated-{attempt:02d}.png"] = source.read_bytes()
    for source in sorted(completed_dir.glob("quantized_attempt_*.png"), key=_attempt_index):
        attempt = _attempt_index(source)
        entries[f"attempts/quantization/quantized-{attempt:02d}.png"] = source.read_bytes()
    for source in sorted(
        completed_dir.glob("palette_validation_attempt_*.json"), key=_attempt_index
    ):
        attempt = _attempt_index(source)
        entries[f"attempts/validation/report-{attempt:02d}.json"] = source.read_bytes()

    layered = profile.output_mode == "layered_image"
    if layered:
        vector_path = completed_dir / "vectorized.svg"
        entries["vector/master.svg"] = vector_path.read_bytes()
        stage_artifacts["vectorization"] = ["vector/master.svg"]
        vector_layers = _add_nested_vector_layers(
            entries, completed_dir / "vector_layers.zip"
        )
        raster_layers = _add_raster_layers(entries, completed_dir / "package")
        stage_artifacts["segmentation"] = vector_layers + raster_layers
        final_path = "output/final-layer-package.zip"
        entries[final_path] = (completed_dir / "result.zip").read_bytes()
    else:
        stage_artifacts["vectorization"] = []
        stage_artifacts["segmentation"] = []
        final_path = "output/final.png"
        entries[final_path] = (completed_dir / "result.png").read_bytes()
    stage_artifacts["final_output"] = [final_path]

    stage_order = (
        "input",
        "transcription",
        "normalization",
        "prompt_building",
        "image_generation",
        "quantization",
        "validation",
        "vectorization",
        "segmentation",
        "final_output",
    )
    manifest = {
        "contract_version": "moonli-run-artifacts.v1",
        "run_id": run_id,
        "pipeline": profile.id,
        "input_type": generation_input.type,
        "output_mode": profile.output_mode,
        "palette_version": profile.palette.id,
        "providers": {
            "image": image_provider,
            "transcription": transcription_provider,
            "normalization": normalization_provider,
        },
        "final_output": {"path": final_path, "media_type": final_media_type},
        "stages": [
            {
                "name": stage,
                "status": "included" if stage_artifacts[stage] else "not_applicable",
                "artifacts": stage_artifacts[stage],
            }
            for stage in stage_order
        ],
    }

    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, content in [("manifest.json", _json_bytes(manifest)), *sorted(entries.items())]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    content = output.getvalue()
    filename = f"moonli-run-{profile.id}-{generation_input.type}.zip"
    return RunArchive(
        content=content,
        filename=filename,
        media_type=RUN_ARCHIVE_MEDIA_TYPE,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def build_pipeline3_run_archive(
    *,
    run_id: str,
    completed_dir: Path,
    generation_input: GenerationInput,
    trace: dict[str, object],
    image_provider: str,
    transcription_provider: str,
    normalization_provider: str,
    translation_provider: str,
) -> RunArchive:
    """Build the diagnostic archive for the shorter pipeline-3 stage graph."""
    entries: dict[str, bytes] = {}
    stage_artifacts: dict[str, list[str]] = {}

    if generation_input.type == "text":
        assert generation_input.text is not None
        input_path = "input/request.txt"
        entries[input_path] = generation_input.text.text.encode("utf-8")
    else:
        assert generation_input.audio is not None
        input_path = f"input/{_safe_audio_name(generation_input.audio.filename)}"
        entries[input_path] = generation_input.audio.content
    stage_artifacts["input"] = [input_path]

    if generation_input.type == "audio":
        entries["text/transcription.txt"] = str(trace.get("transcription") or "").encode(
            "utf-8"
        )
        stage_artifacts["transcription"] = ["text/transcription.txt"]
    else:
        stage_artifacts["transcription"] = []

    entries["text/normalized.txt"] = str(trace.get("normalized_text") or "").encode(
        "utf-8"
    )
    stage_artifacts["normalization"] = ["text/normalized.txt"]
    entries["prompt/prompt.txt"] = str(trace.get("prompt") or "").encode("utf-8")
    stage_artifacts["prompt_building"] = ["prompt/prompt.txt"]

    image_paths: list[str] = []
    for name in ("image_1.jpg", "image_2.jpg", "image_3.jpg"):
        target = f"images/{name}"
        entries[target] = (completed_dir / name).read_bytes()
        image_paths.append(target)
    stage_artifacts["image_generation"] = image_paths
    for stage in ("quantization", "validation", "vectorization", "segmentation"):
        stage_artifacts[stage] = []

    entries["reports/execution-trace.json"] = _json_bytes(
        trace.get("execution_trace") or []
    )
    final_path = "output/moonli-images.zip"
    entries[final_path] = (completed_dir / "moonli-images.zip").read_bytes()
    stage_artifacts["final_output"] = [final_path]

    stage_order = (
        "input",
        "transcription",
        "normalization",
        "prompt_building",
        "image_generation",
        "quantization",
        "validation",
        "vectorization",
        "segmentation",
        "final_output",
    )
    manifest = {
        "contract_version": "moonli-run-artifacts.v1",
        "run_id": run_id,
        "pipeline": "pipeline-3",
        "input_type": generation_input.type,
        "output_mode": "jpeg_set",
        "palette_version": None,
        "providers": {
            "image": image_provider,
            "transcription": transcription_provider,
            "normalization": normalization_provider,
            "translation": translation_provider,
        },
        "final_output": {"path": final_path, "media_type": "application/zip"},
        "stages": [
            {
                "name": stage,
                "status": "included" if stage_artifacts[stage] else "not_applicable",
                "artifacts": stage_artifacts[stage],
            }
            for stage in stage_order
        ],
    }

    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, content in [("manifest.json", _json_bytes(manifest)), *sorted(entries.items())]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    content = output.getvalue()
    return RunArchive(
        content=content,
        filename=f"moonli-run-pipeline-3-{generation_input.type}.zip",
        media_type=RUN_ARCHIVE_MEDIA_TYPE,
        sha256=hashlib.sha256(content).hexdigest(),
    )
