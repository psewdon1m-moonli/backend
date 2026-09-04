from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import (
    COMFYUI_BASE_URL,
    COMFYUI_MAX_BATCH_SIZE,
    COMFYUI_POLL_INTERVAL_SECONDS,
    COMFYUI_POLL_MAX_ATTEMPTS,
    COMFYUI_TIMEOUT_SECONDS,
    COMFYUI_WORKFLOW_PATH,
    NANO_BANANA_PRO_API_KEY,
    NANO_BANANA_PRO_ASPECT_RATIO,
    NANO_BANANA_PRO_BASE_URL,
    NANO_BANANA_PRO_IMAGE_SIZE,
    NANO_BANANA_PRO_MODEL,
    NANO_BANANA_PRO_TIMEOUT_SECONDS,
    RUNTIME_CACHE_DIR,
)
from app.models import CandidateImage, GenerationRequest

logger = logging.getLogger("table_gen.backends")


class GenerationBackend(ABC):
    @abstractmethod
    def generate_candidates(self, job_id: str, request: GenerationRequest, count: int) -> list[CandidateImage]:
        raise NotImplementedError


class MockGenerationBackend(GenerationBackend):
    def generate_candidates(self, job_id: str, request: GenerationRequest, count: int) -> list[CandidateImage]:
        candidates: list[CandidateImage] = []
        for idx in range(1, count + 1):
            digest = hashlib.sha1(f"{job_id}:{request.theme}:{idx}".encode()).hexdigest()[:8]
            score = round(max(0.05, 1.0 - (idx * 0.12)), 3)
            candidates.append(
                CandidateImage(
                    candidate_id=f"cand_{digest}",
                    uri=f"mock://candidates/{job_id}/bw_{idx}.png",
                    score=score,
                )
            )
        return candidates


class ComfyUIGenerationBackend(GenerationBackend):
    def __init__(
        self,
        base_url: str = COMFYUI_BASE_URL,
        workflow_path: str = COMFYUI_WORKFLOW_PATH,
        timeout_seconds: float = COMFYUI_TIMEOUT_SECONDS,
        poll_interval_seconds: float = COMFYUI_POLL_INTERVAL_SECONDS,
        poll_max_attempts: int = COMFYUI_POLL_MAX_ATTEMPTS,
        max_batch_size: int = COMFYUI_MAX_BATCH_SIZE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow_path = workflow_path
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_max_attempts = poll_max_attempts
        self.max_batch_size = max(1, max_batch_size)

    def generate_candidates(self, job_id: str, request: GenerationRequest, count: int) -> list[CandidateImage]:
        template = self._load_workflow_template()
        image_urls: list[str] = []
        remaining = count
        batch_index = 0

        with httpx.Client(timeout=self.timeout_seconds) as client:
            while remaining > 0:
                batch_size = min(remaining, self.max_batch_size)
                workflow = self._inject_prompt_text(template, request.theme)
                workflow = self._apply_batch_size(workflow, batch_size)
                workflow = self._offset_sampler_seed(workflow, seed_offset=batch_index * 9973)
                preferred_output_nodes = self._preferred_output_nodes(workflow)
                prompt_id = self._submit_prompt(client, workflow=workflow, client_id=f"{job_id}_{batch_index}")
                history_entry = self._poll_history_until_ready(client, prompt_id=prompt_id)
                batch_urls = self._extract_image_urls(history_entry, preferred_output_nodes=preferred_output_nodes)
                if not batch_urls:
                    raise ValueError("ComfyUI completed without output images")
                image_urls.extend(batch_urls[:batch_size])
                remaining -= batch_size
                batch_index += 1

        candidates: list[CandidateImage] = []
        for idx, uri in enumerate(image_urls[:count], start=1):
            digest = hashlib.sha1(f"comfy:{job_id}:{uri}:{idx}".encode()).hexdigest()[:8]
            score = round(max(0.05, 1.0 - (idx * 0.1)), 3)
            candidates.append(CandidateImage(candidate_id=f"cand_{digest}", uri=uri, score=score))
        return candidates

    def _load_workflow_template(self) -> dict:
        if not self.workflow_path:
            raise ValueError("TABLE_GEN_COMFYUI_WORKFLOW_PATH is required for comfyui backend")
        path = Path(self.workflow_path)
        if not path.exists():
            raise ValueError(f"ComfyUI workflow file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "nodes" in payload and "links" in payload:
            raise ValueError(
                "ComfyUI workflow file is in UI format. Export it as API format and use that JSON."
            )
        if not isinstance(payload, dict) or not payload:
            raise ValueError("ComfyUI workflow JSON must be a non-empty object in API format")
        return payload

    @staticmethod
    def _inject_prompt_text(workflow: dict, theme: str) -> dict:
        updated = deepcopy(workflow)
        prompt_text = theme.strip()
        if not prompt_text:
            return updated

        for node in updated.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            text_value = inputs.get("text")
            if not isinstance(text_value, str):
                continue
            if ComfyUIGenerationBackend._looks_like_negative_prompt(text_value):
                continue
            base_prompt = text_value.strip()
            if prompt_text.lower() in base_prompt.lower():
                continue
            inputs["text"] = f"{base_prompt}, {prompt_text}" if base_prompt else prompt_text
        return updated

    @staticmethod
    def _looks_like_negative_prompt(text: str) -> bool:
        lowered = text.lower()
        markers = (
            "bad anatomy",
            "watermark",
            "deformed",
            "worst quality",
            "blurry",
            "photorealistic",
            "extra limbs",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _apply_batch_size(workflow: dict, batch_size: int) -> dict:
        updated = deepcopy(workflow)
        for node in updated.values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != "EmptyLatentImage":
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                inputs["batch_size"] = batch_size
        return updated

    @staticmethod
    def _offset_sampler_seed(workflow: dict, seed_offset: int) -> dict:
        if seed_offset == 0:
            return workflow
        updated = deepcopy(workflow)
        for node in updated.values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != "KSampler":
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            seed = inputs.get("seed")
            if isinstance(seed, int):
                inputs["seed"] = max(0, seed + seed_offset)
        return updated

    def _submit_prompt(self, client: httpx.Client, workflow: dict, client_id: str) -> str:
        try:
            response = client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise ValueError(f"ComfyUI /prompt rejected workflow: {detail}") from exc
        payload = response.json()
        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            raise ValueError("ComfyUI response does not contain prompt_id")
        return str(prompt_id)

    @staticmethod
    def _preferred_output_nodes(workflow: dict) -> set[str]:
        # Prefer SaveImage nodes that receive input from RMBG so we keep alpha-cut foreground outputs.
        rmbg_nodes = {node_id for node_id, node in workflow.items() if isinstance(node, dict) and node.get("class_type") == "RMBG"}
        if not rmbg_nodes:
            return set()
        preferred: set[str] = set()
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != "SaveImage":
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            images_input = inputs.get("images")
            if isinstance(images_input, list) and images_input:
                source_node = str(images_input[0])
                if source_node in rmbg_nodes:
                    preferred.add(str(node_id))
        return preferred

    def _poll_history_until_ready(self, client: httpx.Client, prompt_id: str) -> dict:
        for _ in range(self.poll_max_attempts):
            response = client.get(f"{self.base_url}/history/{prompt_id}")
            response.raise_for_status()
            payload = response.json()
            entry = payload.get(prompt_id) or payload.get(str(prompt_id))
            if entry and isinstance(entry, dict):
                outputs = entry.get("outputs")
                if isinstance(outputs, dict) and outputs:
                    return entry
            time.sleep(self.poll_interval_seconds)
        raise ValueError("ComfyUI polling timeout: outputs were not ready")

    def _extract_image_urls(self, history_entry: dict, preferred_output_nodes: set[str] | None = None) -> list[str]:
        outputs = history_entry.get("outputs")
        if not isinstance(outputs, dict):
            return []

        urls: list[str] = []
        allowed = preferred_output_nodes or set()
        for node_id, node_data in outputs.items():
            if allowed and str(node_id) not in allowed:
                continue
            if not isinstance(node_data, dict):
                continue
            images = node_data.get("images")
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict):
                    continue
                filename = image.get("filename")
                subfolder = image.get("subfolder", "")
                image_type = image.get("type", "output")
                if image_type != "output":
                    continue
                if not filename:
                    continue
                query = urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})
                urls.append(f"{self.base_url}/view?{query}")
        return urls


class NanoBananaProGenerationBackend(GenerationBackend):
    STYLE_INSTRUCTIONS = """All generated images must follow the same visual style.

REFERENCE RULE
The reference image defines only the visual style:
- stroke thickness
- eye design
- face proportions
- simplicity of shapes

Do NOT copy:
- colors
- character identity
- composition
- exact shapes

Each request must generate a completely new character.

STYLE
flat SVG icon style
simple geometric shapes
clean vector shapes
minimal details
centered composition
white background

VECTOR RULE (strict)

The image must look like a simple SVG icon.

Shapes must be flat and uniform.

No gradients.
No shading.
No lighting.
No color blending.
No soft edges.
No transparency.

Each shape must use a single solid color.

STROKE RULE (strict)

Use a uniform SVG stroke.

Stroke color must be exactly one of the palette colors.
Do NOT create darker or lighter stroke colors.

Stroke must not simulate lighting or shadow.

COLOR PALETTE (strict)

Only the following colors are allowed:

blue  #4A9AD4
red   #FF1F2D
pink  #EC6A9E
yellow #F5E617
white #FFFFFF
black #000000

No other colors are allowed.

FILL RULE

All fills must be solid flat colors.

Each shape must use exactly one color from the palette.

FACE STRUCTURE (universal)

All characters share the same face structure.

large rounded head
two simple eyes
small nose
simple smiling mouth

optional round cheeks

CHARACTER TYPE RULE

Animals:
simple animal mouth and nose

Humans:
small round nose (dot or oval)
simple curved smile
no animal muzzle

DESIGN RULES

large rounded head
simple shapes
vector friendly
minimal details

GENERATION RULE

Every request must generate a completely new illustration.

Never modify a previous image.
Never reproduce the reference character.

Always keep the same visual style across all characters.

The result should look like a simple exportable SVG icon."""

    def __init__(
        self,
        base_url: str = NANO_BANANA_PRO_BASE_URL,
        api_key: str = NANO_BANANA_PRO_API_KEY,
        timeout_seconds: float = NANO_BANANA_PRO_TIMEOUT_SECONDS,
        model: str = NANO_BANANA_PRO_MODEL,
        aspect_ratio: str = NANO_BANANA_PRO_ASPECT_RATIO,
        image_size: str = NANO_BANANA_PRO_IMAGE_SIZE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size

    def generate_candidates(self, job_id: str, request: GenerationRequest, count: int) -> list[CandidateImage]:
        if not self.base_url:
            raise ValueError("TABLE_GEN_NANO_BANANA_PRO_BASE_URL is required for nano_banana_pro backend")
        if not self.api_key:
            raise ValueError("TABLE_GEN_NANO_BANANA_PRO_API_KEY is required for nano_banana_pro backend")
        if not self.model:
            raise ValueError("TABLE_GEN_NANO_BANANA_PRO_MODEL is required for nano_banana_pro backend")

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        endpoint = f"{self.base_url}/models/{self.model}:generateContent"
        output_dir = RUNTIME_CACHE_DIR / "nano_banana" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        image_uris: list[str] = []
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for idx in range(1, count + 1):
                request_prompt = request.theme.strip()
                user_prompt = (
                    f"USER REQUEST\n{request_prompt}\n\n"
                    f"GENERATION META\nvariation_index={idx}/{count}; palette_color_limit={request.max_colors}"
                )
                payload = {
                    "systemInstruction": {
                        "parts": [
                            {
                                "text": self.STYLE_INSTRUCTIONS
                            }
                        ]
                    },
                    "contents": [
                        {
                            "parts": [
                                {
                                    "text": user_prompt
                                }
                            ]
                        }
                    ],
                    "generationConfig": {
                        "responseModalities": ["IMAGE"],
                        "imageConfig": {
                            "aspectRatio": self.aspect_ratio,
                            "imageSize": self.image_size,
                        },
                    },
                }
                logger.warning(
                    "nano_banana_pro request model=%s aspect=%s size=%s instruction_chars=%s prompt=%s",
                    self.model,
                    self.aspect_ratio,
                    self.image_size,
                    len(self.STYLE_INSTRUCTIONS),
                    request_prompt[:120],
                )
                try:
                    response = client.post(endpoint, json=payload, headers=headers)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.text[:1200] if exc.response is not None else str(exc)
                    raise ValueError(f"nano_banana_pro generateContent rejected request: {detail}") from exc
                data = response.json()

                raw_items = self._extract_image_items(data)
                if not raw_items:
                    raise ValueError("nano_banana_pro response has no image parts")
                uri = self._materialize_item(
                    client=client,
                    output_dir=output_dir,
                    item=raw_items[0],
                    index=idx,
                )
                image_uris.append(uri)

        result: list[CandidateImage] = []
        for idx, uri in enumerate(image_uris, start=1):
            digest = hashlib.sha1(f"nano:{job_id}:{uri}:{idx}".encode()).hexdigest()[:8]
            score = round(max(0.05, 1.0 - (idx * 0.1)), 3)
            result.append(CandidateImage(candidate_id=f"cand_{digest}", uri=uri, score=score))
        return result

    @staticmethod
    def _extract_image_items(payload: dict) -> list[str]:
        candidates: list[str] = []
        candidates_list = payload.get("candidates")
        if isinstance(candidates_list, list):
            for candidate in candidates_list:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts")
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    inline_data = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline_data, dict):
                        data = inline_data.get("data")
                        if isinstance(data, str) and data.strip():
                            candidates.append(data.strip())
                            continue
                    for key in ("data", "url", "image_url", "src", "b64_json", "base64", "image_base64"):
                        value = part.get(key)
                        if isinstance(value, str) and value.strip():
                            candidates.append(value.strip())
                            break
        return candidates

    def _materialize_item(
        self,
        client: httpx.Client,
        output_dir: Path,
        item: str,
        index: int,
    ) -> str:
        file_path = output_dir / f"candidate_{index}.png"
        content: bytes
        if item.startswith(("http://", "https://")):
            image_response = client.get(item)
            image_response.raise_for_status()
            content = image_response.content
        else:
            base64_payload = item
            if "," in item and item.lower().startswith("data:image"):
                base64_payload = item.split(",", 1)[1]
            content = base64.b64decode(base64_payload)
        file_path.write_bytes(content)
        relative = file_path.relative_to(RUNTIME_CACHE_DIR.parent).as_posix()
        return f"/data/{relative}"


def resolve_backend(name: str) -> GenerationBackend:
    if name == "mock":
        return MockGenerationBackend()
    if name == "comfyui":
        return ComfyUIGenerationBackend()
    if name == "nano_banana_pro":
        return NanoBananaProGenerationBackend()
    raise ValueError(f"Unsupported generation backend: {name}")
