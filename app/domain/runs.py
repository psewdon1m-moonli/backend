from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunStatus = Literal[
    "RECEIVED",
    "INPUT_VALIDATED",
    "TRANSCRIBING",
    "TEXT_READY",
    "NORMALIZING_PROMPT",
    "TEXT_NORMALIZED",
    "BUILDING_PROMPT",
    "PROMPT_READY",
    "GENERATING",
    "IMAGE_READY",
    "PALETTE_QUANTIZING",
    "PALETTE_QUANTIZED",
    "PALETTE_VALIDATING",
    "VECTORIZING",
    "VECTORIZED",
    "SEGMENTING",
    "SEGMENTED",
    "LAYER_PROCESSING",
    "OUTPUT_BUILDING",
    "VALIDATING",
    "COMPLETE",
    "FAILED",
]


@dataclass(frozen=True)
class GenerationRun:
    run_id: str
    idempotency_key: str
    request_hash: str
    pipeline_profile: str
    input_type: str
    status: RunStatus
    result_asset_key: str | None = None
    result_media_type: str | None = None
    result_sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None
