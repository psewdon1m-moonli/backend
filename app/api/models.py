from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PipelineTag = Literal["pipeline-1", "pipeline-2", "pipeline-3"]
ProcessedPipelineTag = Literal["pipeline-1", "pipeline-2"]


class TextGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern="^text$")
    pipeline: PipelineTag
    text: str
