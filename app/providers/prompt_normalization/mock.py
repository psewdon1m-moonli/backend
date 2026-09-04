from __future__ import annotations


class MockPromptNormalizer:
    """Predictable pass-through used only by tests and local mock runs."""

    name = "mock"

    async def normalize(self, text: str) -> str:
        return text
