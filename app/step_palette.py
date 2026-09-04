from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.models import GenerationRequest, JobRecord
from app.services.pipeline import build_pack


class PaletteVariant(BaseModel):
    name: str
    colors: list[str] = Field(min_length=2, max_length=6)


class PaletteRequest(BaseModel):
    candidate_id: str = Field(min_length=1)
    candidate_uri: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    variants: list[PaletteVariant] = Field(min_length=1, max_length=6)


class PaletteResponse(BaseModel):
    variants: list[dict[str, str]]


app = FastAPI(title="table_gen step-palette", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=PaletteResponse)
def run_palette(payload: PaletteRequest) -> PaletteResponse:
    results: list[dict[str, str]] = []
    for variant in payload.variants:
        request = GenerationRequest(
            mode="generate",
            generator_backend="mock",
            theme=payload.prompt,
            difficulty="medium",
            max_colors=min(6, len(variant.colors)),
        )
        job = JobRecord.new(job_id=f"pal_job_{uuid.uuid4().hex[:10]}", request=request)
        job.selected_candidate_id = payload.candidate_id
        job.selected_candidate_uri = payload.candidate_uri
        job.selected_palette = [c.upper() for c in variant.colors[:6]]
        try:
            built = build_pack(job)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Palette build failed: {exc}") from exc
        results.append(
            {
                "name": variant.name,
                "pack_id": built.pack_id,
                "pack_path": built.pack_path,
                "master_svg": f"{built.pack_path}/vector/master.svg",
                "preview_uri": f"/data/packs/{built.pack_id}/vector/master.svg",
            }
        )
    return PaletteResponse(variants=results)
