from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image, ImageDraw

from app.domain.images import GeneratedImage
from app.domain.profiles import Palette, PipelineProfile
from app.domain.prompts import GenerationPrompt


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


class MockImageGenerator:
    """Development provider that creates a real, palette-valid PNG fixture."""

    name = "mock"

    async def generate(
        self,
        prompt: GenerationPrompt,
        palette: Palette,
        profile: PipelineProfile,
        attempt: int,
    ) -> GeneratedImage:
        colors = list(palette.colors)
        seed = int(hashlib.sha256(prompt.text.encode("utf-8")).hexdigest()[:8], 16)
        background = colors[seed % len(colors)]
        image = Image.new("RGBA", (profile.width, profile.height), (*_rgb(background), 255))
        draw = ImageDraw.Draw(image)
        width, height = image.size
        for index in range(1, min(len(colors), 7)):
            color = colors[(seed + index) % len(colors)]
            inset_x = width * index // 16
            inset_y = height * index // 16
            if index % 2:
                draw.rounded_rectangle(
                    (inset_x, inset_y, width - inset_x, height - inset_y),
                    radius=max(1, width // 20),
                    fill=(*_rgb(color), 255),
                )
            else:
                draw.ellipse(
                    (inset_x, inset_y, width - inset_x, height - inset_y),
                    fill=(*_rgb(color), 255),
                )
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return GeneratedImage(content=output.getvalue(), media_type="image/png", provider=self.name)
