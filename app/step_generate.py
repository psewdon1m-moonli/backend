from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.models import CandidateImage, GenerationRequest, JobRecord
from app.services.pipeline import generate_candidates


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    min_colors: int = Field(ge=2, le=6, default=2)
    max_colors: int = Field(ge=2, le=6, default=6)
    backend: str = "comfyui"
    count: int = Field(ge=1, le=8, default=4)


class GenerateResponse(BaseModel):
    candidates: list[CandidateImage]


app = FastAPI(title="table_gen step-generate", version="0.1.0")
logger = logging.getLogger("table_gen.step_generate")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=GenerateResponse)
def run_generation(payload: GenerateRequest) -> GenerateResponse:
    if payload.min_colors > payload.max_colors:
        raise HTTPException(status_code=400, detail="min_colors must be <= max_colors")
    supported_backends = {"mock", "comfyui", "nano_banana_pro"}
    if payload.backend not in supported_backends:
        raise HTTPException(status_code=400, detail=f"Unsupported backend: {payload.backend}")
    request = GenerationRequest(
        mode="generate",
        generator_backend=payload.backend,
        theme=payload.prompt,
        difficulty="medium",
        max_colors=payload.max_colors,
    )
    logger.warning("step-generate backend=%s count=%s", payload.backend, payload.count)
    job = JobRecord.new(job_id=f"step_job_{uuid.uuid4().hex[:10]}", request=request)
    try:
        candidates = generate_candidates(job, count=payload.count)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Generation failed: {exc}") from exc
    return GenerateResponse(candidates=candidates[: payload.count])
