from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.errors import install_error_handlers
from app.api.routes.backups import router as backups_router
from app.api.routes.devices import router as devices_router
from app.api.routes.generate import router as v1_router
from app.api.routes.lab import router as lab_router
from app.api.routes.operations import router as operations_router
from app.api.routes.operator import router as operator_router
from app.api.routes.production import router as production_router
from app.api.routes.routing import router as routing_router
from app.api.routes.settings import router as settings_router
from app.api.routes.updater import router as updater_router
from app.composition import build_components
from app.config import (
    AUDIO_HANDLER_TIMEOUT_SECONDS,
    AUDIO_HANDLER_URL,
    PACKS_DIR,
    SESSION_GENERATE_BACKEND,
    STEP_GENERATE_URL,
    STEP_HTTP_TIMEOUT_SECONDS,
    STEP_SEGMENT_URL,
    STEP_VECTORIZE_URL,
    ensure_directories,
)
from app.models import (
    CandidateImage,
    CandidateShortlist,
    GenerationRequest,
    JobRecord,
    LibraryItem,
    PackBuildResponse,
    PublishPackRequest,
    PublishPackResponse,
    SelectCompositionRequest,
    SelectPaletteRequest,
    SessionRecord,
    ValidationReport,
)
from app.presets import PALETTE_PRESETS, SESSION_PALETTES
from app.services.pipeline import build_pack, list_library_items, publish_pack
from app.services.pipeline import generate_candidates as run_generation
from app.services.store import (
    find_job_by_pack_id,
    load_job,
    load_session,
    save_job,
    save_session,
)
from app.services.validation import validate_manifest
from app.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
MOONLI_SETTINGS = Settings.from_env()


@asynccontextmanager
async def lifespan(application: FastAPI):
    ensure_directories()
    components = build_components(MOONLI_SETTINGS)
    application.state.moonli_settings = components.settings
    application.state.client_authenticator = components.authenticator
    application.state.pipeline_profiles = components.profiles
    application.state.rate_limiter = components.rate_limiter
    application.state.generation_service = components.generation_service
    application.state.generation_services = components.generation_services
    application.state.pipeline3_service = components.pipeline3_service
    application.state.artifact_store = components.artifact_store
    application.state.production_secret_store = components.production_secret_store
    application.state.production_pipeline_config_store = (
        components.production_pipeline_config_store
    )
    application.state.run_repository = components.run_repository
    application.state.metrics = components.metrics
    application.state.production_usage_store = components.production_usage_store
    application.state.system_monitor = components.system_monitor
    application.state.operator_auth_store = components.operator_auth_store
    application.state.login_rate_limiter = components.login_rate_limiter
    application.state.audit_store = components.audit_store
    application.state.google_key_validator = components.google_key_validator
    application.state.backup_manager = components.backup_manager
    application.state.updater_client = components.updater_client
    application.state.server_settings_store = components.server_settings_store
    application.state.device_registry = components.device_registry
    application.state.routing_config_store = components.routing_config_store
    application.state.state_operation_lock = asyncio.Lock()
    components.run_repository.fail_stale_in_progress(components.settings.in_progress_stale_minutes)
    components.run_repository.apply_retention(
        components.settings.input_retention_hours,
        components.settings.completed_retention_days,
    )
    components.production_usage_store.trim()
    components.artifact_store.cleanup(
        staging_hours=components.settings.staging_retention_hours,
        input_hours=components.settings.input_retention_hours,
        completed_days=components.settings.completed_retention_days,
    )
    components.audit_store.append(
        action="service.start",
        outcome="success",
        summary="Moonli service initialized.",
        actor_type="service",
        actor_id="moonli",
    )

    async def maintenance() -> None:
        while True:
            await asyncio.sleep(3600)
            async with application.state.state_operation_lock:
                components.run_repository.fail_stale_in_progress(
                    components.settings.in_progress_stale_minutes
                )
                components.run_repository.apply_retention(
                    components.settings.input_retention_hours,
                    components.settings.completed_retention_days,
                )
                components.production_usage_store.trim()
                components.artifact_store.cleanup(
                    staging_hours=components.settings.staging_retention_hours,
                    input_hours=components.settings.input_retention_hours,
                    completed_days=components.settings.completed_retention_days,
                )

    task = asyncio.create_task(maintenance())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        components.audit_store.close()


app = FastAPI(
    title="Moonli Backend",
    version=MOONLI_SETTINGS.app_version,
    description="Server-owned tagged generation pipelines for Moonli",
    lifespan=lifespan,
)


@app.middleware("http")
async def disable_web_ui_cache(request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"
    serialized_paths = {
        "/v1/generate",
        "/v1/normalize",
        "/internal/settings",
        "/internal/auth/rotate-access-key",
        "/internal/updates/install",
        "/internal/updater/restore",
        "/internal/routing",
    }
    requires_lock = (
        request.method in {"POST", "PUT", "DELETE"}
        and (
            request.url.path in serialized_paths
            or request.url.path.startswith("/internal/backups")
            or request.url.path.startswith("/internal/devices/")
            or request.url.path.startswith("/internal/production/pipelines/")
        )
    )
    lock = getattr(request.app.state, "state_operation_lock", None)
    if requires_lock and lock is not None:
        async with lock:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if (
        request.url.path == "/"
        or request.url.path.startswith("/web/")
        or request.url.path.startswith("/internal/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(MOONLI_SETTINGS.allowed_hosts))
if MOONLI_SETTINGS.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(MOONLI_SETTINGS.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-API-Key",
            "X-CSRF-Token",
            "X-Moonli-Device-Id",
        ],
    )
install_error_handlers(app)
app.include_router(v1_router)
app.include_router(operator_router)
app.include_router(lab_router)
app.include_router(production_router)
app.include_router(operations_router)
app.include_router(backups_router)
app.include_router(devices_router)
app.include_router(updater_router)
app.include_router(settings_router)
app.include_router(routing_router)

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


def _normalize_audio_content_type(filename: str, content_type: str | None) -> str:
    raw = (content_type or "").strip().lower()
    alias_map = {
        "application/ogg": "audio/ogg",
        "audio/x-wav": "audio/wav",
        "audio/x-aac": "audio/aac",
        "audio/x-flac": "audio/flac",
        "audio/mp3": "audio/mpeg",
    }
    if raw in alias_map:
        return alias_map[raw]
    if raw and raw != "application/octet-stream":
        return raw

    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("audio/"):
        return guessed

    ext = Path(filename).suffix.lower()
    fallback_by_ext = {
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
    }
    return fallback_by_ext.get(ext, "application/octet-stream")


@app.get("/")
def frontend() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/livez", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readiness() -> dict[str, str]:
    if not app.state.operator_auth_store.initialized():
        raise HTTPException(status_code=503, detail="Operator credential is not initialized")
    return {"status": "ready"}


@app.post("/audio/prompt-short")
async def audio_prompt_short(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
) -> dict:
    filename = file.filename or "audio.bin"
    content_type = _normalize_audio_content_type(filename, file.content_type)
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio file")

    data = {}
    if session_id:
        data["session_id"] = session_id
    if user_id:
        data["user_id"] = user_id

    try:
        async with httpx.AsyncClient(timeout=AUDIO_HANDLER_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{AUDIO_HANDLER_URL.rstrip('/')}/audio/forward",
                data=data,
                files={"file": (filename, payload, content_type)},
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"audio handler call failed: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:1200]
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            detail = str(parsed.get("detail", detail))
        raise HTTPException(status_code=502, detail=f"audio handler returned {response.status_code}: {detail}")

    try:
        body = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"audio handler returned non-JSON response: {exc}") from exc

    webhook_data = body.get("webhook_response", {}) if isinstance(body, dict) else {}
    prompt_short = webhook_data.get("prompt_short") if isinstance(webhook_data, dict) else None
    transcript = webhook_data.get("transcript") if isinstance(webhook_data, dict) else None
    return {
        "prompt_short": prompt_short,
        "transcript": transcript,
        "audio_handler_response": body,
    }


def _to_data_uri(path: str | None) -> str | None:
    if not path:
        return None
    norm = path.replace("\\", "/")
    marker = "/app/data/"
    idx = norm.find(marker)
    if idx >= 0:
        return f"/data/{norm[idx + len(marker):]}"
    return None


@app.get("/library/items", response_model=list[LibraryItem])
def library_items() -> list[LibraryItem]:
    return list_library_items()


@app.get("/palettes/presets")
def palette_presets() -> dict[str, list[str]]:
    return PALETTE_PRESETS


@app.get("/palettes/session")
def session_palettes() -> dict[str, list[str]]:
    return SESSION_PALETTES


@app.post("/sessions", response_model=SessionRecord)
def create_session() -> SessionRecord:
    session = SessionRecord.new(session_id=f"sess_{uuid.uuid4().hex[:12]}")
    save_session(session)
    return session


@app.get("/sessions/{session_id}", response_model=SessionRecord)
def get_session(session_id: str) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.post("/sessions/{session_id}/mode", response_model=SessionRecord)
def select_session_mode(session_id: str, payload: dict[str, str]) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    mode = payload.get("mode")
    if mode not in {"library", "generate"}:
        raise HTTPException(status_code=400, detail="mode must be library or generate")
    session.mode = mode
    session.status = "mode_selected"
    save_session(session)
    return session


@app.post("/sessions/{session_id}/library/select", response_model=SessionRecord)
def select_library_for_session(session_id: str, payload: dict[str, str]) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "library":
        raise HTTPException(status_code=400, detail="Session mode must be library")
    pack_id = payload.get("pack_id")
    if not pack_id:
        raise HTTPException(status_code=400, detail="pack_id is required")
    try:
        published = publish_pack(pack_id=pack_id, destination="runtime_cache")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.selected_pack_id = pack_id
    session.runtime_pack_path = published.published_path
    session.status = "ready_for_runtime"
    save_session(session)
    return session


@app.post("/sessions/{session_id}/generate/start", response_model=SessionRecord)
def start_generate_session(session_id: str, payload: dict) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "generate":
        raise HTTPException(status_code=400, detail="Session mode must be generate")

    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    min_colors = int(payload.get("min_colors", 2))
    max_colors = int(payload.get("max_colors", 6))
    if min_colors < 2 or max_colors > 6 or min_colors > max_colors:
        raise HTTPException(status_code=400, detail="Color range must be between 2 and 6")

    backend = SESSION_GENERATE_BACKEND
    request_payload = {
        "prompt": prompt,
        "min_colors": min_colors,
        "max_colors": max_colors,
        "backend": backend,
        "count": 1,
    }
    try:
        with httpx.Client(timeout=STEP_HTTP_TIMEOUT_SECONDS) as client:
            response = client.post(f"{STEP_GENERATE_URL}/run", json=request_payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        if exc.response is not None:
            try:
                parsed = exc.response.json()
            except ValueError:
                parsed = None
            detail = (
                str(parsed.get("detail", ""))[:1200]
                if isinstance(parsed, dict)
                else exc.response.text[:1200]
            )
        session.status = "failed"
        session.error = f"generate step failed: {detail or exc}"
        save_session(session)
        raise HTTPException(status_code=502, detail=session.error) from exc
    except Exception as exc:
        session.status = "failed"
        session.error = f"generate step failed: {exc}"
        save_session(session)
        raise HTTPException(status_code=502, detail=session.error) from exc

    session.user_prompt = prompt
    session.min_colors = min_colors
    session.max_colors = max_colors
    session.candidates = [CandidateImage.model_validate(item) for item in data.get("candidates", [])]
    session.status = "candidates_generated"
    save_session(session)
    return session


@app.post("/sessions/{session_id}/generate/select", response_model=SessionRecord)
def select_generated_candidate(session_id: str, payload: dict[str, str]) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "generate" or session.status != "candidates_generated":
        raise HTTPException(status_code=400, detail="Invalid session state for candidate selection")

    candidate_id = payload.get("candidate_id")
    if not candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    if not any(item.candidate_id == candidate_id for item in session.candidates):
        raise HTTPException(status_code=400, detail="candidate_id not found in generated candidates")
    selected_candidate = next((item for item in session.candidates if item.candidate_id == candidate_id), None)
    if not selected_candidate:
        raise HTTPException(status_code=400, detail="candidate_id not found in generated candidates")
    selected_candidate_uri = (
        selected_candidate.uri if hasattr(selected_candidate, "uri") else str(selected_candidate.get("uri", ""))
    )
    if not selected_candidate_uri:
        raise HTTPException(status_code=400, detail="Selected candidate does not contain uri")

    session.selected_candidate_id = candidate_id
    session.selected_candidate_uri = selected_candidate_uri
    session.prepared_image_path = None
    session.vector_preview_uri = None
    session.status = "candidate_selected"
    save_session(session)
    return session


@app.post("/sessions/{session_id}/generate/vectorize", response_model=SessionRecord)
def run_generated_vectorize(session_id: str) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "generate" or session.status != "candidate_selected":
        raise HTTPException(status_code=400, detail="Invalid session state for vectorize")
    if not session.selected_candidate_id or not session.selected_candidate_uri:
        raise HTTPException(status_code=400, detail="Selected candidate is missing")

    try:
        with httpx.Client(timeout=STEP_HTTP_TIMEOUT_SECONDS) as client:
            r_vec = client.post(
                f"{STEP_VECTORIZE_URL}/run",
                json={
                    "session_id": session_id,
                    "candidate_id": session.selected_candidate_id,
                    "candidate_uri": session.selected_candidate_uri,
                    "layer_count": session.max_colors,
                },
            )
            r_vec.raise_for_status()
            vec_payload = r_vec.json()
        session.prepared_image_path = vec_payload.get("prepared_image_path")
        session.status = "vectorized"
        save_session(session)
    except Exception as exc:
        session.status = "failed"
        session.error = f"vectorize step failed: {exc}"
        save_session(session)
        raise HTTPException(status_code=502, detail=session.error) from exc
    return session


@app.post("/sessions/{session_id}/generate/segment", response_model=SessionRecord)
def run_generated_segment(session_id: str) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "generate" or session.status != "vectorized":
        raise HTTPException(status_code=400, detail="Invalid session state for segment")
    if not session.selected_candidate_id or not session.prepared_image_path:
        raise HTTPException(status_code=400, detail="Vectorize output is missing")

    try:
        with httpx.Client(timeout=STEP_HTTP_TIMEOUT_SECONDS) as client:
            r_seg = client.post(
                f"{STEP_SEGMENT_URL}/run",
                json={
                    "session_id": session_id,
                    "candidate_id": session.selected_candidate_id,
                    "prepared_image_path": session.prepared_image_path,
                    "layer_count": session.max_colors,
                },
            )
            r_seg.raise_for_status()
            seg_payload = r_seg.json()
        session.status = "segmented"
        session.vector_preview_uri = _to_data_uri(seg_payload.get("master_svg_path"))
        session.palette_variants = []
        save_session(session)
    except Exception as exc:
        session.status = "failed"
        session.error = f"segment step failed: {exc}"
        save_session(session)
        raise HTTPException(status_code=502, detail=session.error) from exc
    return session


@app.post("/sessions/{session_id}/variant/select", response_model=SessionRecord)
def select_palette_variant(session_id: str, payload: dict[str, str]) -> SessionRecord:
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.mode != "generate" or session.status != "palette_variants_generated":
        raise HTTPException(status_code=400, detail="Invalid session state for variant selection")

    pack_id = payload.get("pack_id")
    if not pack_id:
        raise HTTPException(status_code=400, detail="pack_id is required")
    match = next((item for item in session.palette_variants if item.get("pack_id") == pack_id), None)
    if not match:
        raise HTTPException(status_code=400, detail="pack_id not found in variants")
    try:
        runtime_copy = publish_pack(pack_id=pack_id, destination="runtime_cache")
        publish_pack(pack_id=pack_id, destination="library")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"publish failed: {exc}") from exc

    session.selected_pack_id = pack_id
    session.runtime_pack_path = runtime_copy.published_path
    session.status = "ready_for_runtime"
    save_session(session)
    return session


@app.post("/jobs", response_model=JobRecord)
def create_job(payload: GenerationRequest) -> JobRecord:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job = JobRecord.new(job_id=job_id, request=payload)
    save_job(job)
    return job


@app.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/jobs/{job_id}/generate", response_model=CandidateShortlist)
def generate_job_candidates(job_id: str) -> CandidateShortlist:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        candidates = run_generation(job, count=6)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Generation backend failure: {exc}") from exc
    job.shortlist = candidates[:4]
    job.status = "candidates_ready"
    save_job(job)
    return CandidateShortlist(items=job.shortlist)


@app.get("/jobs/{job_id}/shortlist", response_model=CandidateShortlist)
def get_shortlist(job_id: str) -> CandidateShortlist:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return CandidateShortlist(items=job.shortlist)


@app.post("/jobs/{job_id}/selection/composition", response_model=JobRecord)
def select_composition(job_id: str, payload: SelectCompositionRequest) -> JobRecord:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in {"candidates_ready", "composition_selected"}:
        raise HTTPException(status_code=400, detail="Invalid job state for composition selection")
    if not any(item.candidate_id == payload.candidate_id for item in job.shortlist):
        raise HTTPException(status_code=400, detail="Candidate is not in shortlist")
    job.selected_candidate_id = payload.candidate_id
    job.status = "composition_selected"
    save_job(job)
    return job


@app.post("/jobs/{job_id}/selection/palette", response_model=JobRecord)
def select_palette(job_id: str, payload: SelectPaletteRequest) -> JobRecord:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in {"composition_selected", "palette_selected"}:
        raise HTTPException(status_code=400, detail="Invalid job state for palette selection")
    if not job.selected_candidate_id:
        raise HTTPException(status_code=400, detail="Select composition first")
    if len(payload.colors) > job.request.max_colors:
        raise HTTPException(status_code=400, detail="Palette exceeds max_colors from request")
    job.selected_palette = payload.colors
    job.status = "palette_selected"
    save_job(job)
    return job


@app.post("/jobs/{job_id}/build", response_model=PackBuildResponse)
def build_job_pack(job_id: str) -> PackBuildResponse:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "palette_selected":
        raise HTTPException(status_code=400, detail="Invalid job state for build")
    try:
        result = build_pack(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.pack_id = result.pack_id
    job.status = "packed"
    save_job(job)
    return result


@app.post("/packs/{pack_id}/publish", response_model=PublishPackResponse)
def publish(pack_id: str, payload: PublishPackRequest) -> PublishPackResponse:
    try:
        published = publish_pack(pack_id=pack_id, destination=payload.destination)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job = find_job_by_pack_id(pack_id)
    if job:
        job.status = "published"
        save_job(job)
    return published


@app.get("/packs/{pack_id}/validate", response_model=ValidationReport)
def validate_pack(pack_id: str) -> ValidationReport:
    manifest_path = PACKS_DIR / pack_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"Manifest not found for pack: {pack_id}")
    return validate_manifest(manifest_path)
