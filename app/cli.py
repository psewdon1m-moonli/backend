from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.config import ensure_directories
from app.models import CandidateImage, GenerationRequest, JobRecord
from app.presets import PALETTE_PRESETS
from app.services.pipeline import (
    build_pack,
    generate_candidates,
    list_library_items,
    publish_pack,
)
from app.services.store import save_job


def _ask_choice(prompt: str, min_value: int, max_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw.isdigit():
            print("Enter an option number.")
            continue
        value = int(raw)
        if value < min_value or value > max_value:
            print("Select a valid number from the list.")
            continue
        return value


def _create_job(request: GenerationRequest) -> JobRecord:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job = JobRecord.new(job_id=job_id, request=request)
    save_job(job)
    return job


def _select_palette_interactive() -> list[str]:
    preset_names = list(PALETTE_PRESETS.keys())
    print("\nSelect a palette:")
    for idx, name in enumerate(preset_names, start=1):
        colors = ", ".join(PALETTE_PRESETS[name])
        print(f"{idx}. {name}: {colors}")
    selected = _ask_choice("Palette number: ", 1, len(preset_names))
    return PALETTE_PRESETS[preset_names[selected - 1]]


def _select_composition_generate_interactive(job: JobRecord) -> tuple[JobRecord, CandidateImage]:
    candidates = generate_candidates(job, count=6)[:4]
    if not candidates:
        raise RuntimeError("No generation candidates received.")
    job.shortlist = candidates
    job.status = "candidates_ready"
    save_job(job)

    print("\nSelect a composition:")
    for idx, item in enumerate(candidates, start=1):
        print(f"{idx}. {item.candidate_id} ({item.uri}) score={item.score}")
    selected = _ask_choice("Composition number: ", 1, len(candidates))
    choice = candidates[selected - 1]
    job.selected_candidate_id = choice.candidate_id
    job.status = "composition_selected"
    save_job(job)
    return job, choice


def _select_composition_library_interactive(job: JobRecord) -> tuple[JobRecord, str]:
    items = list_library_items()
    if not items:
        raise RuntimeError("The library is empty. Publish at least one pack with destination=library first.")
    print("\nSelect a finished composition from the library:")
    for idx, item in enumerate(items, start=1):
        colors = ",".join(item.colors) if item.colors else "-"
        print(f"{idx}. {item.pack_id} | theme={item.theme} | difficulty={item.difficulty} | colors={colors}")
    selected = _ask_choice("Composition number: ", 1, len(items))
    item = items[selected - 1]
    job.shortlist = [CandidateImage(candidate_id=item.pack_id, uri=item.manifest_path, score=1.0)]
    job.status = "candidates_ready"
    job.selected_candidate_id = item.pack_id
    job.status = "composition_selected"
    save_job(job)
    return job, item.pack_id


def run_flow(
    source_mode: str,
    theme: str,
    palette_name: str,
    generator_backend: str = "comfyui",
) -> dict[str, str]:
    ensure_directories()
    if palette_name not in PALETTE_PRESETS:
        raise ValueError(f"Unknown palette preset: {palette_name}")
    if source_mode not in {"library", "generate"}:
        raise ValueError("source_mode must be 'library' or 'generate'")

    request = GenerationRequest(
        mode=source_mode,
        generator_backend=generator_backend if source_mode == "generate" else "mock",
        theme=theme,
        difficulty="medium",
        max_colors=6,
    )
    job = _create_job(request)

    if source_mode == "generate":
        try:
            candidates = generate_candidates(job, count=6)[:4]
        except Exception as exc:
            raise RuntimeError(f"ComfyUI generation failed: {exc}") from exc
        if not candidates:
            raise RuntimeError("No generation candidates received.")
        job.shortlist = candidates
        job.status = "candidates_ready"
        job.selected_candidate_id = candidates[0].candidate_id
        job.status = "composition_selected"
    else:
        items = list_library_items()
        if not items:
            raise RuntimeError("Library is empty.")
        item = items[0]
        job.shortlist = [CandidateImage(candidate_id=item.pack_id, uri=item.manifest_path, score=1.0)]
        job.status = "candidates_ready"
        job.selected_candidate_id = item.pack_id
        job.status = "composition_selected"

    job.selected_palette = PALETTE_PRESETS[palette_name]
    job.status = "palette_selected"
    save_job(job)

    built = build_pack(job)
    job.pack_id = built.pack_id
    job.status = "packed"
    save_job(job)

    published = publish_pack(pack_id=built.pack_id, destination="runtime_cache")
    job.status = "published"
    save_job(job)

    manifest = json.loads((Path(published.published_path) / "manifest.json").read_text(encoding="utf-8"))
    return {
        "job_id": job.job_id,
        "pack_id": built.pack_id,
        "pack_path": built.pack_path,
        "published_path": published.published_path,
        "master_svg": str(Path(published.published_path) / manifest["vector_assets"]["master_svg"]),
        "layer_count": str(len(manifest["vector_assets"]["color_layers"])),
    }


def main() -> None:
    ensure_directories()
    print("table_gen CLI")
    print("1. Use an existing item (library)")
    print("2. Create a new item (generate)")
    mode_choice = _ask_choice("Select a mode: ", 1, 2)
    source_mode = "library" if mode_choice == 1 else "generate"

    theme = "library_selected"
    selected_info = ""
    if source_mode == "generate":
        theme_input = input("Enter a generation theme (default: forest animal): ").strip()
        theme = theme_input or "forest animal"
        request = GenerationRequest(
            mode="generate",
            generator_backend="comfyui",
            theme=theme,
            difficulty="medium",
            max_colors=6,
        )
        job = _create_job(request)
        try:
            job, picked = _select_composition_generate_interactive(job)
            selected_info = picked.candidate_id
        except Exception as exc:
            raise RuntimeError(f"ComfyUI generation failed: {exc}") from exc
    else:
        request = GenerationRequest(
            mode="library",
            generator_backend="mock",
            theme="library_selected",
            difficulty="medium",
            max_colors=6,
        )
        job = _create_job(request)
        job, selected_pack_id = _select_composition_library_interactive(job)
        selected_info = selected_pack_id

    palette = _select_palette_interactive()
    job.selected_palette = palette
    job.status = "palette_selected"
    save_job(job)

    built = build_pack(job)
    job.pack_id = built.pack_id
    job.status = "packed"
    save_job(job)
    published = publish_pack(pack_id=built.pack_id, destination="runtime_cache")
    job.status = "published"
    save_job(job)

    manifest_path = Path(published.published_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vector_assets = manifest["vector_assets"]

    print("\nArtifact built successfully")
    print(f"mode: {source_mode}")
    print(f"theme: {theme}")
    print(f"selected composition: {selected_info}")
    print(f"job_id: {job.job_id}")
    print(f"pack_id: {built.pack_id}")
    print(f"pack_path: {built.pack_path}")
    print(f"published_path: {published.published_path}")
    print(f"master_svg: {Path(published.published_path) / vector_assets['master_svg']}")
    print("color_layers:")
    for item in vector_assets["color_layers"]:
        print(f"  {item['color']}: {Path(published.published_path) / item['path']}")


if __name__ == "__main__":
    main()
