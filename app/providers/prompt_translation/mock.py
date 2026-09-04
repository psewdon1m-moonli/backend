from __future__ import annotations


class MockPromptTranslator:
    """Predictable pass-through used by local and test Pipeline 3 runs."""

    name = "mock"

    async def translate(self, text: str) -> str:
        return text
