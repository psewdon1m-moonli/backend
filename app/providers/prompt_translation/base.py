from __future__ import annotations

from typing import Protocol


class PromptTranslator(Protocol):
    name: str

    async def translate(self, text: str) -> str: ...
