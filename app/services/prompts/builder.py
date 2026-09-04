from __future__ import annotations

import hashlib
import re
import string

from app.domain.profiles import PipelineProfile
from app.domain.prompts import GenerationPrompt, VisualBrief

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")
INCLUDE_MARKERS = (
    "must include",
    "must have",
    "обязательно",
    "самое главное",
    "важно",
    "должен быть",
    "должна быть",
    "должно быть",
)
AVOID_MARKERS = (
    "must avoid",
    "must not",
    "do not",
    "without",
    "не должно",
    "не должно быть",
    "не должно присутствовать",
    "без ",
    "избег",
    "никаких",
)
EDITABLE_TEMPLATE = """Create one finished illustration from the request below.

Original request:
{input_text}

Visual subject:
{subject}

Must include:
{must_include}

Must avoid:
{must_avoid}

Supporting details:
{supporting_details}

Profile constraints:
{constraints}

Exact palette constraint: use only these RGB colors for every visible pixel:
{palette}

Canvas: {width}x{height} pixels.
Pipeline: {pipeline}.
Do not invent shades, blends, gradients, or anti-aliased edge colors."""
TEMPLATE_FIELDS = frozenset(
    {
        "input_text",
        "subject",
        "must_include",
        "must_avoid",
        "supporting_details",
        "constraints",
        "palette",
        "width",
        "height",
        "pipeline",
    }
)


def _sentences(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in SENTENCE_BOUNDARY.split(text):
        value = raw.strip(" \t-•")
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _fit_sentences(items: list[str], budget: int) -> tuple[str, ...]:
    selected: list[str] = []
    used = 0
    for item in items:
        words = item.split()
        if len(words) > 80:
            item = " ".join(words[:80])
        cost = len(item) + 2
        if selected and used + cost > budget:
            continue
        if not selected and cost > budget:
            item = " ".join(item.split()[:40])
            cost = len(item)
        selected.append(item)
        used += cost
    return tuple(selected)


class PromptBuilder:
    template_version = "moonli_visual_v1"

    def __init__(
        self,
        compression_threshold: int = 1200,
        detail_budget: int = 1800,
        template: str | None = None,
        templates: dict[str, str] | None = None,
    ) -> None:
        self._compression_threshold = compression_threshold
        self._detail_budget = detail_budget
        self._template = template
        self._templates = dict(templates or {})

    def build(self, text: str, profile: PipelineProfile) -> GenerationPrompt:
        brief = self._extract_brief(text)
        template = self._templates.get(profile.id, self._template)
        if template is not None:
            return self._build_from_template(text, brief, profile, template)
        allowed = ", ".join(profile.palette.colors)
        sections = [
            "Create one finished illustration from the visual brief below.",
            f"Visual subject: {brief.subject}",
        ]
        if brief.must_include:
            sections.append("Must include:\n- " + "\n- ".join(brief.must_include))
        if brief.must_avoid:
            sections.append("Must avoid:\n- " + "\n- ".join(brief.must_avoid))
        if brief.supporting_details:
            sections.append("Supporting details:\n- " + "\n- ".join(brief.supporting_details))
        sections.extend(
            [
                "Profile constraints:\n- " + "\n- ".join(profile.visual_constraints),
                (
                    "Exact palette constraint: use only these RGB colors for every visible pixel: "
                    f"{allowed}. Do not invent shades, blends, gradients, or anti-aliased edge colors."
                ),
                f"Canvas: {profile.width}x{profile.height} pixels.",
            ]
        )
        return GenerationPrompt(text="\n\n".join(sections), template_version=self.template_version, brief=brief)

    @staticmethod
    def editable_template() -> str:
        return EDITABLE_TEMPLATE

    def _build_from_template(
        self, text: str, brief: VisualBrief, profile: PipelineProfile, template: str
    ) -> GenerationPrompt:
        formatter = string.Formatter()
        for _, field_name, format_spec, conversion in formatter.parse(template):
            if field_name is None:
                continue
            if field_name not in TEMPLATE_FIELDS or format_spec or conversion:
                raise ValueError(f"Unsupported prompt-template field: {field_name}")

        def _lines(items: tuple[str, ...]) -> str:
            return "\n".join(f"- {item}" for item in items) if items else "- None specified"

        rendered = template.format_map(
            {
                "input_text": text,
                "subject": brief.subject,
                "must_include": _lines(brief.must_include),
                "must_avoid": _lines(brief.must_avoid),
                "supporting_details": _lines(brief.supporting_details),
                "constraints": _lines(profile.visual_constraints),
                "palette": ", ".join(profile.palette.colors),
                "width": str(profile.width),
                "height": str(profile.height),
                "pipeline": profile.id,
            }
        ).strip()
        if not rendered:
            raise ValueError("Prompt template produced an empty prompt")
        version = f"custom_{hashlib.sha256(template.encode('utf-8')).hexdigest()[:12]}"
        return GenerationPrompt(text=rendered, template_version=version, brief=brief)

    def _extract_brief(self, text: str) -> VisualBrief:
        sentences = _sentences(text)
        lowered = [sentence.casefold() for sentence in sentences]
        avoid = [sentence for sentence, low in zip(sentences, lowered) if any(mark in low for mark in AVOID_MARKERS)]
        include = [
            sentence
            for sentence, low in zip(sentences, lowered)
            if sentence not in avoid and any(mark in low for mark in INCLUDE_MARKERS)
        ]
        ordinary = [sentence for sentence in sentences if sentence not in avoid and sentence not in include]
        compressed = len(text) > self._compression_threshold or len(sentences) > 10
        if compressed:
            subject_items = ordinary[:2] or include[:1] or avoid[:1]
            supporting = ordinary[2:]
        else:
            subject_items = ordinary or include[:1] or avoid[:1]
            supporting = []
        subject = " ".join(_fit_sentences(subject_items, 600)) or "User-described scene"
        return VisualBrief(
            subject=subject,
            must_include=_fit_sentences(include, 1000),
            must_avoid=_fit_sentences(avoid, 1000),
            supporting_details=_fit_sentences(supporting, self._detail_budget),
            compressed=compressed,
        )
