from __future__ import annotations

from typing import Protocol


class PromptNormalizer(Protocol):
    name: str

    async def normalize(self, text: str) -> str: ...
