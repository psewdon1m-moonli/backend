from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import DATA_DIR
from app.services.vectorizer import build_vector_assets


class SegmentRequest(BaseModel):
    session_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    prepared_image_path: str = Field(min_length=1)
    layer_count: int = Field(ge=2, le=6, default=6)


class SegmentResponse(BaseModel):
    segment_dir: str
    master_svg_path: str
    layer_count: int


app = FastAPI(title="table_gen step-segment", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=SegmentResponse)
def run_segment(payload: SegmentRequest) -> SegmentResponse:
    session_dir = DATA_DIR / "sessions" / payload.session_id / "segment"
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        vector_assets = build_vector_assets(
            pack_dir=session_dir / f"base_{uuid.uuid4().hex[:8]}",
            candidate_id=payload.candidate_id,
            palette=[],
            source_image_uri=payload.prepared_image_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Segment failed: {exc}") from exc
    base_dirs = sorted([p for p in session_dir.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    latest_dir = base_dirs[0] if base_dirs else session_dir
    master_svg_path = str(latest_dir / vector_assets["master_svg"])
    return SegmentResponse(segment_dir=str(session_dir), master_svg_path=master_svg_path, layer_count=payload.layer_count)
