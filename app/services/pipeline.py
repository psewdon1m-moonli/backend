from __future__ import annotations

import json
import shutil
import uuid

from app.config import PACKS_DIR, RUNTIME_CACHE_DIR
from app.models import (
    CandidateImage,
    JobRecord,
    LibraryItem,
    PackBuildResponse,
    PublishPackResponse,
)
from app.services.backends import resolve_backend
from app.services.store import write_json
from app.services.validation import validate_manifest
from app.services.vectorizer import build_vector_assets


def generate_candidates(job: JobRecord, count: int = 6) -> list[CandidateImage]:
    backend = resolve_backend(job.request.generator_backend)
    return backend.generate_candidates(job_id=job.job_id, request=job.request, count=count)


def build_pack(job: JobRecord) -> PackBuildResponse:
    if not job.selected_candidate_id:
        raise ValueError("Composition is not selected")
    if not job.selected_palette:
        raise ValueError("Palette is not selected")

    pack_id = f"pack_{uuid.uuid4().hex[:12]}"
    pack_dir = PACKS_DIR / pack_id
    assets_dir = pack_dir / "assets"
    layers_dir = pack_dir / "layers"
    cumulative_dir = pack_dir / "cumulative"
    assets_dir.mkdir(parents=True, exist_ok=True)
    layers_dir.mkdir(parents=True, exist_ok=True)
    cumulative_dir.mkdir(parents=True, exist_ok=True)

    for idx, color in enumerate(job.selected_palette, start=1):
        layer_name = f"step_{idx:02d}_mask.txt"
        (layers_dir / layer_name).write_text(
            f"mock-mask for {color} (candidate={job.selected_candidate_id})\n",
            encoding="utf-8",
        )
        (cumulative_dir / f"step_{idx:02d}_state.txt").write_text(
            f"mock-cumulative-state {idx}\n",
            encoding="utf-8",
        )

    vector_assets = build_vector_assets(
        pack_dir=pack_dir,
        candidate_id=job.selected_candidate_id,
        palette=job.selected_palette,
        source_image_uri=job.selected_candidate_uri,
    )

    manifest = {
        "manifest_version": "v1",
        "pack_id": pack_id,
        "source": {
            "mode": job.request.mode,
            "job_id": job.job_id,
            "theme": job.request.theme,
            "difficulty": job.request.difficulty,
            "generator_backend": job.request.generator_backend,
        },
        "composition": {
            "candidate_id": job.selected_candidate_id,
        },
        "palette": {"colors": job.selected_palette},
        "steps": [
            {
                "step_index": idx,
                "color": color,
                "layer_mask": f"layers/step_{idx:02d}_mask.txt",
                "cumulative_state": f"cumulative/step_{idx:02d}_state.txt",
            }
            for idx, color in enumerate(job.selected_palette, start=1)
        ],
        "assets": {
            "preview": "assets/preview.txt",
        },
        "vector_assets": vector_assets,
    }
    manifest_path = pack_dir / "manifest.json"
    write_json(manifest_path, manifest)
    write_json(pack_dir / "palette.json", {"colors": job.selected_palette})
    (assets_dir / "preview.txt").write_text("mock-preview\n", encoding="utf-8")

    validation = validate_manifest(manifest_path)
    if not validation.valid:
        raise ValueError(f"Manifest validation failed: {'; '.join(validation.errors)}")

    return PackBuildResponse(
        pack_id=pack_id,
        pack_path=str(pack_dir),
        manifest_path=str(manifest_path),
        status="validated",
        validation=validation,
    )


def publish_pack(pack_id: str, destination: str) -> PublishPackResponse:
    source = PACKS_DIR / pack_id
    if not source.exists():
        raise FileNotFoundError(f"Pack not found: {pack_id}")

    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found for pack: {pack_id}")
    report = validate_manifest(manifest_path)
    if not report.valid:
        raise ValueError(f"Pack validation failed before publish: {'; '.join(report.errors)}")

    target = RUNTIME_CACHE_DIR / pack_id if destination == "runtime_cache" else PACKS_DIR / "library" / pack_id
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    return PublishPackResponse(
        pack_id=pack_id,
        destination=destination,
        published_path=str(target),
    )


def list_library_items() -> list[LibraryItem]:
    library_root = PACKS_DIR / "library"
    if not library_root.exists():
        return []
    items: list[LibraryItem] = []
    for pack_dir in sorted(library_root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir():
            continue
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        source = manifest.get("source", {})
        palette = manifest.get("palette", {})
        items.append(
            LibraryItem(
                pack_id=manifest.get("pack_id", pack_dir.name),
                theme=source.get("theme", "unknown"),
                difficulty=source.get("difficulty", "medium"),
                colors=palette.get("colors", []),
                manifest_path=str(manifest_path),
                preview_uri=f"/data/packs/library/{pack_dir.name}/vector/master.svg",
            )
        )
    return items
