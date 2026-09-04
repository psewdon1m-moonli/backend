from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisualBrief:
    subject: str
    must_include: tuple[str, ...]
    must_avoid: tuple[str, ...]
    supporting_details: tuple[str, ...]
    compressed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "must_include": list(self.must_include),
            "must_avoid": list(self.must_avoid),
            "supporting_details": list(self.supporting_details),
            "compressed": self.compressed,
        }


@dataclass(frozen=True)
class GenerationPrompt:
    text: str
    template_version: str
    brief: VisualBrief
