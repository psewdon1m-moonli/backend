from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.api.errors import MoonliError
from app.domain.images import ImageAsset
from app.domain.inputs import GenerationInput
from app.domain.profiles import PipelineProfile
from app.providers.errors import NoVisualSubjectError, ProviderError
from app.providers.image_generation.base import ImageGenerator
from app.providers.prompt_normalization.base import PromptNormalizer
from app.services.input_resolver import InputResolver
from app.services.outputs import (
    FullImageOutputBuilder,
    LayeredImageOutputBuilder,
    RuntimeValidator,
)
from app.services.outputs.models import BuiltOutput
from app.services.outputs.validator import OutputValidationError
from app.services.processing.palette_quantizer import (
    PaletteQuantizationError,
    PaletteQuantizer,
)
from app.services.processing.palette_validator import PaletteValidator
from app.services.processing.palette_vectorizer import (
    PaletteVectorizationError,
    PaletteVectorizer,
    segment_palette_svg,
)
from app.services.prompts import PromptBuilder
from app.storage.artifact_store import LocalArtifactStore
from app.storage.run_repository import SqliteRunRepository
from app.telemetry import MetricsRegistry

logger = logging.getLogger("moonli.generation")


@dataclass(frozen=True)
class GenerationResult:
    run_id: str
    path: Path
    media_type: str
    sha256: str
    replayed: bool


class GenerationService:
    def __init__(
        self,
        input_resolver: InputResolver,
        prompt_normalizer: PromptNormalizer,
        prompt_builder: PromptBuilder,
        image_generator: ImageGenerator,
        palette_quantizer: PaletteQuantizer,
        palette_validator: PaletteValidator,
        artifact_store: LocalArtifactStore,
        run_repository: SqliteRunRepository,
        runtime_validator: RuntimeValidator,
        metrics: MetricsRegistry,
        generation_attempts: int,
    ) -> None:
        self._input_resolver = input_resolver
        self._prompt_normalizer = prompt_normalizer
        self._prompt_builder = prompt_builder
        self._image_generator = image_generator
        self._palette_quantizer = palette_quantizer
        self._palette_validator = palette_validator
        self._artifact_store = artifact_store
        self._runs = run_repository
        self._runtime_validator = runtime_validator
        self._metrics = metrics
        self._generation_attempts = generation_attempts
        self._full_image_builder = FullImageOutputBuilder(runtime_validator)
        self._layered_image_builder = LayeredImageOutputBuilder(
            layer_processor=self._new_layer_processor(), validator=runtime_validator
        )

    @staticmethod
    def _new_layer_processor():
        from app.services.processing.layers import LayerProcessor

        return LayerProcessor()

    async def generate(
        self,
        generation_input: GenerationInput,
        profile: PipelineProfile,
        idempotency_key: str,
        request_hash: str,
    ) -> GenerationResult:
        started = time.perf_counter()
        run_id = f"run_{uuid.uuid4().hex}"
        run, created = self._runs.reserve(
            run_id=run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            pipeline_profile=profile.id,
            input_type=generation_input.type,
            palette_version=profile.palette.id,
        )
        if not created:
            return self._replay(run)

        self._metrics.increment("moonli_requests_total", profile=profile.id, input=generation_input.type)
        self._log(run_id, profile.id, "RECEIVED", "started")
        try:
            run_dir = self._artifact_store.begin_run(run_id)
            if generation_input.type == "text":
                assert generation_input.text is not None
                self._runs.set_input(run_id, generation_input.text.text, None)
            else:
                assert generation_input.audio is not None
                suffix = Path(generation_input.audio.filename).suffix.lower()
                if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
                    suffix = ".bin"
                input_key = f"inputs/{run_id}/input_audio{suffix}"
                self._artifact_store.put(input_key, generation_input.audio.content)
                self._runs.set_input(run_id, None, input_key)
            self._stage(run_id, profile.id, "INPUT_VALIDATED")
            if generation_input.type == "audio":
                self._stage(run_id, profile.id, "TRANSCRIBING")
                transcription_started = time.perf_counter()
            else:
                transcription_started = None
            normalized = await self._input_resolver.resolve(generation_input)
            if transcription_started is not None:
                self._metrics.observe(
                    "moonli_transcription_duration",
                    time.perf_counter() - transcription_started,
                    provider=self._input_resolver.transcriber_name,
                )
            self._stage(run_id, profile.id, "TEXT_READY")

            self._stage(run_id, profile.id, "NORMALIZING_PROMPT")
            normalization_started = time.perf_counter()
            try:
                normalized_prompt = await self._prompt_normalizer.normalize(normalized.text)
            except NoVisualSubjectError as exc:
                raise MoonliError(
                    "NO_VISUAL_SUBJECT", "Say what you want to draw.", 422
                ) from exc
            except ProviderError as exc:
                self._metrics.increment(
                    "moonli_provider_errors_total", provider=self._prompt_normalizer.name
                )
                raise MoonliError(
                    "PROMPT_NORMALIZATION_FAILED", "Unable to normalize the visual request.", 502
                ) from exc
            self._metrics.observe(
                "moonli_prompt_normalization_duration",
                time.perf_counter() - normalization_started,
                provider=self._prompt_normalizer.name,
            )
            self._stage(run_id, profile.id, "TEXT_NORMALIZED")

            self._stage(run_id, profile.id, "BUILDING_PROMPT")
            try:
                prompt = self._prompt_builder.build(normalized_prompt, profile)
            except Exception as exc:
                raise MoonliError("PROMPT_BUILD_FAILED", "Unable to build an image prompt.", 500) from exc
            self._runs.set_prompt_trace(
                run_id,
                normalized_text=normalized_prompt,
                transcription=normalized.transcription,
                visual_brief=prompt.brief.as_dict(),
                prompt=prompt.text,
            )
            self._stage(run_id, profile.id, "PROMPT_READY")

            validated = None
            source_asset = None
            for attempt in range(1, self._generation_attempts + 1):
                self._stage(run_id, profile.id, "GENERATING", attempt=attempt)
                generation_started = time.perf_counter()
                try:
                    generated = await self._image_generator.generate(prompt, profile.palette, profile, attempt)
                except ProviderError as exc:
                    self._metrics.increment("moonli_provider_errors_total", provider=self._image_generator.name)
                    raise MoonliError("IMAGE_GENERATION_FAILED", "Unable to generate an image.", 502) from exc
                self._metrics.observe(
                    "moonli_image_generation_duration",
                    time.perf_counter() - generation_started,
                    provider=self._image_generator.name,
                )
                source_asset = self._materialize(run_id, run_dir, generated.content, attempt, generated.media_type)
                self._runs.set_source(run_id, generated.provider, f"staging/{run_id}/{source_asset.path.name}")
                self._stage(run_id, profile.id, "IMAGE_READY", attempt=attempt)
                self._stage(run_id, profile.id, "PALETTE_QUANTIZING", attempt=attempt)
                quantization_started = time.perf_counter()
                try:
                    quantized = self._palette_quantizer.quantize(
                        generated, profile.palette, profile
                    )
                except PaletteQuantizationError as exc:
                    raise MoonliError(
                        "PALETTE_QUANTIZATION_FAILED",
                        "Unable to quantize the generated image to the allowed palette.",
                        502,
                    ) from exc
                self._metrics.observe(
                    "moonli_palette_quantization_duration",
                    time.perf_counter() - quantization_started,
                    profile=profile.id,
                )
                self._metrics.increment(
                    "moonli_palette_quantized_pixels_total",
                    quantized.changed_pixels,
                    profile=profile.id,
                )
                self._artifact_store.put(
                    f"staging/{run_id}/quantized_attempt_{attempt}.png",
                    quantized.image.content,
                )
                self._artifact_store.put(
                    f"staging/{run_id}/quantization_attempt_{attempt}.json",
                    json.dumps(
                        {
                            "attempt": attempt,
                            "changed_pixels": quantized.changed_pixels,
                            "cleanup_changed_pixels": quantized.cleanup_changed_pixels,
                            "cleanup_removed_components": (
                                quantized.cleanup_removed_components
                            ),
                            "opaque_pixels": quantized.opaque_pixels,
                            "transparent_pixels": quantized.transparent_pixels,
                            "unique_colors_before": quantized.unique_colors_before,
                            "unique_colors_after": quantized.unique_colors_after,
                            "palette_counts": list(quantized.palette_counts),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8"),
                )
                self._stage(
                    run_id,
                    profile.id,
                    "PALETTE_QUANTIZED",
                    attempt=attempt,
                    changed_pixels=quantized.changed_pixels,
                    cleanup_changed_pixels=quantized.cleanup_changed_pixels,
                    cleanup_removed_components=quantized.cleanup_removed_components,
                    unique_colors_before=quantized.unique_colors_before,
                    unique_colors_after=quantized.unique_colors_after,
                )
                self._stage(run_id, profile.id, "PALETTE_VALIDATING", attempt=attempt)
                palette_check = self._palette_validator.validate(
                    quantized.image, profile.palette, profile
                )
                validation_payload = {
                    "attempt": attempt,
                    "pipeline": profile.id,
                    "palette_version": profile.palette.id,
                    "valid": palette_check.valid,
                    "invalid_pixels": palette_check.invalid_pixels,
                    "invalid_colors": list(palette_check.invalid_colors),
                    "reason": palette_check.reason,
                    "snapped_pixels": palette_check.snapped_pixels,
                    "opaque_pixels": palette_check.opaque_pixels,
                }
                validation_content = json.dumps(
                    validation_payload, ensure_ascii=False, indent=2
                ).encode("utf-8")
                self._artifact_store.put(
                    f"staging/{run_id}/palette_validation_attempt_{attempt}.json",
                    validation_content,
                )
                if palette_check.valid:
                    self._artifact_store.put(
                        f"staging/{run_id}/palette_validation.json",
                        validation_content,
                    )
                    validated = palette_check.image
                    assert validated is not None
                    break
                self._metrics.increment("moonli_palette_mismatch_total", profile=profile.id)
                if attempt < self._generation_attempts:
                    self._metrics.increment("moonli_regenerations_total", profile=profile.id)
                    self._log(
                        run_id,
                        profile.id,
                        "PALETTE_VALIDATING",
                        "retry",
                        attempt=attempt,
                        invalid_pixels=palette_check.invalid_pixels,
                    )

            if validated is None:
                raise MoonliError(
                    "PALETTE_VALIDATION_FAILED",
                    "Unable to generate an image that matches the allowed palette.",
                    502,
                )

            exact_source = run_dir / "source.png"
            validated.image.save(exact_source, format="PNG", optimize=True)
            self._runs.set_source(run_id, self._image_generator.name, f"staging/{run_id}/source.png")
            if profile.output_mode == "layered_image":
                self._stage(run_id, profile.id, "VECTORIZING")
                try:
                    vectorized = PaletteVectorizer().vectorize(validated, profile)
                    self._artifact_store.put(
                        f"staging/{run_id}/vectorized.svg", vectorized.content
                    )
                    self._stage(
                        run_id,
                        profile.id,
                        "VECTORIZED",
                        runs=vectorized.run_count,
                        used_colors=vectorized.used_colors,
                    )
                    self._stage(run_id, profile.id, "SEGMENTING")
                    segmented = segment_palette_svg(vectorized.content, profile)
                    self._artifact_store.put(
                        f"staging/{run_id}/vector_layers.zip", segmented.content
                    )
                    self._stage(
                        run_id,
                        profile.id,
                        "SEGMENTED",
                        used_layers=segmented.used_layers,
                        total_layers=segmented.total_layers,
                    )
                except PaletteVectorizationError as exc:
                    raise MoonliError(
                        "VECTORIZATION_FAILED",
                        "Unable to vectorize and segment the palette-valid image.",
                        500,
                    ) from exc
                self._stage(run_id, profile.id, "LAYER_PROCESSING")
                layer_started = time.perf_counter()
            else:
                layer_started = None
            self._stage(run_id, profile.id, "OUTPUT_BUILDING")
            self._stage(run_id, profile.id, "VALIDATING")
            try:
                output = self._build_output(validated, profile, run_id, run_dir)
            except OutputValidationError as exc:
                self._metrics.increment("moonli_output_validation_failures_total", profile=profile.id)
                raise MoonliError("OUTPUT_VALIDATION_FAILED", "Generated output failed validation.", 500) from exc
            if layer_started is not None:
                self._metrics.observe(
                    "moonli_layer_processing_duration", time.perf_counter() - layer_started, profile=profile.id
                )

            self._artifact_store.publish_run(run_id)
            asset_key = self._artifact_store.completed_asset_key(run_id, output.filename)
            completed = self._runs.complete(run_id, asset_key, output.media_type, output.sha256)
            result_path = self._artifact_store.path_for(asset_key)
            self._metrics.increment("moonli_success_total", profile=profile.id)
            self._metrics.increment("moonli_response_bytes_total", result_path.stat().st_size, profile=profile.id)
            self._metrics.observe(
                "moonli_generation_duration", time.perf_counter() - started, profile=profile.id
            )
            self._log(run_id, profile.id, "COMPLETE", "success", bytes=result_path.stat().st_size)
            return GenerationResult(
                run_id=completed.run_id,
                path=result_path,
                media_type=output.media_type,
                sha256=output.sha256,
                replayed=False,
            )
        except MoonliError as exc:
            self._runs.fail(run_id, exc.code, exc.message)
            self._metrics.increment("moonli_failures_total", profile=profile.id, code=exc.code)
            self._log(run_id, profile.id, "FAILED", "failure", code=exc.code)
            raise
        except Exception as exc:
            self._runs.fail(run_id, "INTERNAL_ERROR", "Unexpected generation failure.")
            self._metrics.increment("moonli_failures_total", profile=profile.id, code="INTERNAL_ERROR")
            logger.exception(
                json.dumps(
                    {"run_id": run_id, "profile": profile.id, "stage": "FAILED", "result": "internal_error"}
                )
            )
            raise MoonliError("INTERNAL_ERROR", "Unexpected generation failure.", 500) from exc

    def _replay(self, run) -> GenerationResult:
        if run.status == "COMPLETE" and run.result_asset_key and self._artifact_store.exists(run.result_asset_key):
            assert run.result_media_type and run.result_sha256
            self._metrics.increment("moonli_idempotent_replays_total", profile=run.pipeline_profile)
            return GenerationResult(
                run_id=run.run_id,
                path=self._artifact_store.path_for(run.result_asset_key),
                media_type=run.result_media_type,
                sha256=run.result_sha256,
                replayed=True,
            )
        if run.status == "COMPLETE":
            raise MoonliError(
                "RESULT_EXPIRED",
                "The stored result has expired; submit a new request with a new Idempotency-Key.",
                410,
            )
        if run.status == "FAILED":
            raise MoonliError(run.error_code or "INTERNAL_ERROR", run.error_message or "Generation failed.", 409)
        raise MoonliError(
            "GENERATION_IN_PROGRESS",
            "A generation with this Idempotency-Key is still running.",
            409,
            retry_after=5,
        )

    def _materialize(
        self, run_id: str, run_dir: Path, content: bytes, attempt: int, media_type: str
    ) -> ImageAsset:
        path = self._artifact_store.put(f"staging/{run_id}/provider_attempt_{attempt}.png", content)
        digest = hashlib.sha256(content).hexdigest()
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError):
            width, height = 0, 0
        return ImageAsset(
            asset_id=f"img_{digest[:16]}",
            path=path,
            media_type=media_type,
            sha256=digest,
            width=width,
            height=height,
        )

    def _build_output(self, validated, profile: PipelineProfile, run_id: str, run_dir: Path) -> BuiltOutput:
        if profile.output_mode == "full_image":
            return self._full_image_builder.build(validated, profile, run_id, run_dir)
        return self._layered_image_builder.build(validated, profile, run_id, run_dir)

    def _stage(self, run_id: str, profile: str, stage: str, **details: object) -> None:
        self._runs.set_stage(run_id, stage)  # type: ignore[arg-type]
        self._log(run_id, profile, stage, "ok", **details)

    @staticmethod
    def _log(run_id: str, profile: str, stage: str, result: str, **details: object) -> None:
        logger.info(
            json.dumps(
                {"run_id": run_id, "profile": profile, "stage": stage, "result": result, **details},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
