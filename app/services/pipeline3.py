from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from app.api.errors import MoonliError
from app.domain.inputs import GenerationInput
from app.providers.errors import NoVisualSubjectError, ProviderError
from app.providers.image_generation.variants import ImageVariantGenerator
from app.providers.prompt_normalization.base import PromptNormalizer
from app.providers.prompt_translation.base import PromptTranslator
from app.services.input_resolver import InputResolver
from app.storage.artifact_store import LocalArtifactStore
from app.storage.run_repository import SqliteRunRepository
from app.telemetry import MetricsRegistry

logger = logging.getLogger("moonli.pipeline3")
IMAGE_SET_MEDIA_TYPE = "application/zip"
IMAGE_SET_FILENAME = "moonli-images.zip"
IMAGE_NAMES = ("image_1.jpg", "image_2.jpg", "image_3.jpg")


@dataclass(frozen=True)
class Pipeline3Result:
    run_id: str
    path: Path
    media_type: str
    sha256: str
    replayed: bool


class Pipeline3Service:
    def __init__(
        self,
        *,
        input_resolver: InputResolver,
        prompt_normalizer: PromptNormalizer,
        prompt_translator: PromptTranslator,
        image_generator: ImageVariantGenerator,
        artifact_store: LocalArtifactStore,
        run_repository: SqliteRunRepository,
        metrics: MetricsRegistry,
    ) -> None:
        self._input_resolver = input_resolver
        self._prompt_normalizer = prompt_normalizer
        self._prompt_translator = prompt_translator
        self._image_generator = image_generator
        self._artifacts = artifact_store
        self._runs = run_repository
        self._metrics = metrics

    @property
    def providers(self) -> dict[str, str]:
        return {
            "transcription": self._input_resolver.transcriber_name,
            "normalization": self._prompt_normalizer.name,
            "translation": self._prompt_translator.name,
            "image": self._image_generator.name,
        }

    async def normalize(
        self,
        *,
        generation_input: GenerationInput,
        idempotency_key: str,
        request_hash: str,
    ) -> Pipeline3Result:
        started = time.perf_counter()
        run_id = f"run_{uuid.uuid4().hex}"
        run, created = self._runs.reserve(
            run_id=run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            pipeline_profile="pipeline-3",
            input_type="audio",
            palette_version="not-applicable",
        )
        if not created:
            return self._replay(run)
        try:
            run_dir = self._artifacts.begin_run(run_id)
            assert generation_input.audio is not None
            suffix = Path(generation_input.audio.filename).suffix.lower()
            if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
                suffix = ".bin"
            input_key = f"inputs/{run_id}/input_audio{suffix}"
            self._artifacts.put(input_key, generation_input.audio.content)
            self._runs.set_input(run_id, None, input_key)
            self._runs.set_stage(run_id, "INPUT_VALIDATED")
            self._runs.set_stage(run_id, "TRANSCRIBING")
            resolved = await self._input_resolver.resolve(generation_input)
            self._runs.set_stage(run_id, "TEXT_READY")
            self._runs.set_stage(run_id, "NORMALIZING_PROMPT")
            try:
                normalized = await self._prompt_normalizer.normalize(resolved.text)
            except NoVisualSubjectError as exc:
                raise MoonliError(
                    "NO_VISUAL_SUBJECT", "Say what you want to draw.", 422
                ) from exc
            except ProviderError as exc:
                raise MoonliError(
                    "PROMPT_NORMALIZATION_FAILED",
                    "Unable to normalize the visual request.",
                    502,
                ) from exc
            self._runs.set_prompt_trace(
                run_id,
                normalized_text=normalized,
                transcription=resolved.transcription,
                visual_brief={"subject": normalized},
                prompt="",
            )
            self._runs.set_stage(run_id, "TEXT_NORMALIZED")
            content = normalized.encode("utf-8")
            output_path = run_dir / "normalized.txt"
            output_path.write_bytes(content)
            self._artifacts.publish_run(run_id)
            asset_key = self._artifacts.completed_asset_key(run_id, output_path.name)
            digest = hashlib.sha256(content).hexdigest()
            completed = self._runs.complete(run_id, asset_key, "text/plain", digest)
            self._metrics.increment("moonli_success_total", profile="pipeline-3-normalize")
            self._metrics.observe(
                "moonli_generation_duration",
                time.perf_counter() - started,
                profile="pipeline-3-normalize",
            )
            return Pipeline3Result(
                run_id=completed.run_id,
                path=self._artifacts.path_for(asset_key),
                media_type="text/plain",
                sha256=digest,
                replayed=False,
            )
        except MoonliError as exc:
            self._runs.fail(run_id, exc.code, exc.message)
            raise
        except Exception as exc:
            self._runs.fail(run_id, "INTERNAL_ERROR", "Unexpected normalization failure.")
            logger.exception(
                json.dumps({"run_id": run_id, "pipeline": "pipeline-3", "operation": "normalize"})
            )
            raise MoonliError("INTERNAL_ERROR", "Unexpected normalization failure.", 500) from exc

    async def generate(
        self,
        *,
        normalized_text: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Pipeline3Result:
        run_id = f"run_{uuid.uuid4().hex}"
        run, created = self._runs.reserve(
            run_id=run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            pipeline_profile="pipeline-3",
            input_type="text",
            palette_version="not-applicable",
        )
        if not created:
            return self._replay(run)
        try:
            run_dir = self._artifacts.begin_run(run_id)
            self._runs.set_input(run_id, normalized_text, None)
            self._runs.set_stage(run_id, "INPUT_VALIDATED")
            self._runs.set_stage(run_id, "TEXT_READY")
            self._runs.set_stage(run_id, "TRANSLATING_PROMPT")
            try:
                prompt = await self._prompt_translator.translate(normalized_text)
            except ProviderError as exc:
                raise MoonliError(
                    "PROMPT_TRANSLATION_FAILED",
                    "Unable to translate the visual request into English.",
                    502,
                ) from exc
            self._runs.set_prompt_trace(
                run_id,
                normalized_text=normalized_text,
                transcription=None,
                visual_brief={"subject": normalized_text},
                prompt=prompt,
            )
            self._runs.set_stage(run_id, "PROMPT_READY")
            self._runs.set_stage(run_id, "GENERATING")
            try:
                images = await self._image_generator.generate(prompt)
            except ProviderError as exc:
                raise MoonliError(
                    "IMAGE_GENERATION_FAILED", "Unable to generate three images.", 502
                ) from exc
            for name, content in zip(IMAGE_NAMES, images, strict=True):
                self._artifacts.put(f"staging/{run_id}/{name}", content)
            self._runs.set_stage(run_id, "IMAGE_READY")
            self._runs.set_stage(run_id, "OUTPUT_BUILDING")
            archive = self._build_archive(images)
            result_path = run_dir / IMAGE_SET_FILENAME
            result_path.write_bytes(archive)
            self._runs.set_stage(run_id, "VALIDATING")
            self._artifacts.publish_run(run_id)
            asset_key = self._artifacts.completed_asset_key(run_id, IMAGE_SET_FILENAME)
            digest = hashlib.sha256(archive).hexdigest()
            completed = self._runs.complete(run_id, asset_key, IMAGE_SET_MEDIA_TYPE, digest)
            self._metrics.increment("moonli_success_total", profile="pipeline-3")
            return Pipeline3Result(
                run_id=completed.run_id,
                path=self._artifacts.path_for(asset_key),
                media_type=IMAGE_SET_MEDIA_TYPE,
                sha256=digest,
                replayed=False,
            )
        except MoonliError as exc:
            self._runs.fail(run_id, exc.code, exc.message)
            raise
        except Exception as exc:
            self._runs.fail(run_id, "INTERNAL_ERROR", "Unexpected image generation failure.")
            logger.exception(
                json.dumps({"run_id": run_id, "pipeline": "pipeline-3", "operation": "generate"})
            )
            raise MoonliError("INTERNAL_ERROR", "Unexpected image generation failure.", 500) from exc

    async def full_run(
        self,
        *,
        generation_input: GenerationInput,
        idempotency_key: str,
        request_hash: str,
    ) -> Pipeline3Result:
        """Run the complete pipeline-3 diagnostic flow as one idempotent operation."""
        started = time.perf_counter()
        run_id = f"run_{uuid.uuid4().hex}"
        run, created = self._runs.reserve(
            run_id=run_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            pipeline_profile="pipeline-3",
            input_type=generation_input.type,
            palette_version="not-applicable",
        )
        if not created:
            return self._replay(run)
        try:
            run_dir = self._artifacts.begin_run(run_id)
            if generation_input.type == "text":
                assert generation_input.text is not None
                self._runs.set_input(run_id, generation_input.text.text, None)
            else:
                assert generation_input.audio is not None
                suffix = Path(generation_input.audio.filename).suffix.lower()
                if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
                    suffix = ".bin"
                input_key = f"inputs/{run_id}/input_audio{suffix}"
                self._artifacts.put(input_key, generation_input.audio.content)
                self._runs.set_input(run_id, None, input_key)

            self._runs.set_stage(run_id, "INPUT_VALIDATED")
            if generation_input.type == "audio":
                self._runs.set_stage(run_id, "TRANSCRIBING")
            resolved = await self._input_resolver.resolve(generation_input)
            self._runs.set_stage(run_id, "TEXT_READY")
            self._runs.set_stage(run_id, "NORMALIZING_PROMPT")
            try:
                normalized = await self._prompt_normalizer.normalize(resolved.text)
            except NoVisualSubjectError as exc:
                raise MoonliError(
                    "NO_VISUAL_SUBJECT", "Say what you want to draw.", 422
                ) from exc
            except ProviderError as exc:
                raise MoonliError(
                    "PROMPT_NORMALIZATION_FAILED",
                    "Unable to normalize the visual request.",
                    502,
                ) from exc
            self._runs.set_stage(run_id, "TEXT_NORMALIZED")
            self._runs.set_stage(run_id, "TRANSLATING_PROMPT")
            try:
                prompt = await self._prompt_translator.translate(normalized)
            except ProviderError as exc:
                raise MoonliError(
                    "PROMPT_TRANSLATION_FAILED",
                    "Unable to translate the visual request into English.",
                    502,
                ) from exc
            self._runs.set_prompt_trace(
                run_id,
                normalized_text=normalized,
                transcription=resolved.transcription,
                visual_brief={"subject": normalized},
                prompt=prompt,
            )
            self._runs.set_stage(run_id, "PROMPT_READY")
            self._runs.set_stage(run_id, "GENERATING")
            try:
                images = await self._image_generator.generate(prompt)
            except ProviderError as exc:
                raise MoonliError(
                    "IMAGE_GENERATION_FAILED", "Unable to generate three images.", 502
                ) from exc
            for name, content in zip(IMAGE_NAMES, images, strict=True):
                (run_dir / name).write_bytes(content)
            self._runs.set_source(
                run_id,
                self._image_generator.name,
                f"staging/{run_id}/{IMAGE_NAMES[0]}",
            )
            self._runs.set_stage(run_id, "IMAGE_READY")
            self._runs.set_stage(run_id, "OUTPUT_BUILDING")
            archive = self._build_archive(images)
            result_path = run_dir / IMAGE_SET_FILENAME
            result_path.write_bytes(archive)
            self._runs.set_stage(run_id, "VALIDATING")
            self._artifacts.publish_run(run_id)
            asset_key = self._artifacts.completed_asset_key(run_id, IMAGE_SET_FILENAME)
            digest = hashlib.sha256(archive).hexdigest()
            completed = self._runs.complete(run_id, asset_key, IMAGE_SET_MEDIA_TYPE, digest)
            self._metrics.increment("moonli_success_total", profile="pipeline-3-full-run")
            self._metrics.observe(
                "moonli_generation_duration",
                time.perf_counter() - started,
                profile="pipeline-3-full-run",
            )
            return Pipeline3Result(
                run_id=completed.run_id,
                path=self._artifacts.path_for(asset_key),
                media_type=IMAGE_SET_MEDIA_TYPE,
                sha256=digest,
                replayed=False,
            )
        except MoonliError as exc:
            self._runs.fail(run_id, exc.code, exc.message)
            raise
        except Exception as exc:
            self._runs.fail(run_id, "INTERNAL_ERROR", "Unexpected pipeline-3 failure.")
            logger.exception(
                json.dumps({"run_id": run_id, "pipeline": "pipeline-3", "operation": "full-run"})
            )
            raise MoonliError("INTERNAL_ERROR", "Unexpected pipeline-3 failure.", 500) from exc

    @staticmethod
    def _build_archive(images: tuple[bytes, bytes, bytes]) -> bytes:
        output = BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_STORED, allowZip64=False
        ) as archive:
            for name, content in zip(IMAGE_NAMES, images, strict=True):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        return output.getvalue()

    def _replay(self, run) -> Pipeline3Result:
        if (
            run.status == "COMPLETE"
            and run.result_asset_key
            and self._artifacts.exists(run.result_asset_key)
        ):
            assert run.result_media_type and run.result_sha256
            return Pipeline3Result(
                run_id=run.run_id,
                path=self._artifacts.path_for(run.result_asset_key),
                media_type=run.result_media_type,
                sha256=run.result_sha256,
                replayed=True,
            )
        if run.status == "COMPLETE":
            raise MoonliError(
                "RESULT_EXPIRED",
                "The stored result has expired; use a new Idempotency-Key.",
                410,
            )
        if run.status == "FAILED":
            raise MoonliError(
                run.error_code or "INTERNAL_ERROR",
                run.error_message or "The operation failed.",
                409,
            )
        raise MoonliError(
            "GENERATION_IN_PROGRESS",
            "An operation with this Idempotency-Key is still running.",
            409,
            retry_after=5,
        )
