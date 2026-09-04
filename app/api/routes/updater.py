from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, Header, Request
from starlette.datastructures import UploadFile

from app.api.errors import MoonliError

router = APIRouter(include_in_schema=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _operator(request: Request, *, mutate: bool = False) -> None:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=mutate)


@router.get("/api/v1/register/snapshot")
def updater_catalog(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    configured = request.app.state.moonli_settings.updater_catalog_token
    scheme, _, token = (authorization or "").partition(" ")
    if (
        not configured
        or scheme.lower() != "bearer"
        or not token
        or not secrets.compare_digest(token, configured)
    ):
        raise MoonliError("UNAUTHORIZED", "Invalid updater catalog credential.", 401)
    values = {
        "repositories": {
            "moonli": {"url": request.app.state.moonli_settings.backend_repository_url}
        }
    }
    body = json.dumps({"values": values}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    checksum = "sha256:" + hashlib.sha256(body).hexdigest()
    revision = checksum.removeprefix("sha256:")[:24]
    return {
        "schema": "exocortex.register.snapshot.v1",
        "revision": revision,
        "checksum": checksum,
        "values": values,
    }


@router.get("/internal/updates/status")
def update_status(request: Request) -> dict[str, object]:
    _operator(request)
    return request.app.state.updater_client.status()


@router.get("/internal/updates/releases")
def update_releases(request: Request) -> dict[str, object]:
    _operator(request)
    return request.app.state.updater_client.releases()


@router.post("/internal/updates/install")
def install_update(request: Request, version: str = "") -> dict[str, object]:
    _operator(request, mutate=True)
    result = request.app.state.updater_client.start(version)
    request.app.state.audit_store.append(
        action="update.install",
        outcome="success",
        summary="A local updater job was requested.",
        target_type="update_job",
        target_id=str(result.get("id", "pending")),
        request_id=getattr(request.state, "request_id", None),
        context={"version": version or "latest"},
    )
    return result


@router.get("/internal/updates/jobs/{job_id}")
def update_job(job_id: str, request: Request) -> dict[str, object]:
    _operator(request)
    return request.app.state.updater_client.job(job_id)


@router.post("/internal/updates/jobs/{job_id}/rollback")
def rollback_update(job_id: str, request: Request) -> dict[str, object]:
    _operator(request, mutate=True)
    result = request.app.state.updater_client.rollback(job_id)
    request.app.state.audit_store.append(
        action="update.rollback",
        outcome="success",
        summary="A local updater rollback was requested.",
        target_type="update_job",
        target_id=job_id,
        request_id=getattr(request.state, "request_id", None),
    )
    return result


@router.post("/internal/updater/restore")
async def updater_restore(
    request: Request,
    x_updater_token: str | None = Header(default=None, alias="X-Updater-Token"),
) -> dict[str, object]:
    configured = request.app.state.moonli_settings.updater_control_token
    if not configured or not x_updater_token or not secrets.compare_digest(configured, x_updater_token):
        raise MoonliError("UNAUTHORIZED", "Invalid updater control token.", 401)
    settings = request.app.state.moonli_settings
    form = await request.form(max_files=1, max_fields=1, max_part_size=settings.backup_max_compressed_bytes)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise MoonliError("INVALID_BACKUP", "Updater backup file is required.", 422)
    spool = settings.data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="updater-restore-", suffix=".zip", dir=spool, delete=False
        ) as handle:
            path = Path(handle.name)
            total = 0
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > settings.backup_max_compressed_bytes:
                    raise MoonliError("BACKUP_TOO_LARGE", "Updater backup exceeds the size limit.", 413)
                handle.write(chunk)
            handle.flush()
        if total == 0:
            raise MoonliError("INVALID_BACKUP", "Updater backup file is empty.", 422)
        result = request.app.state.backup_manager.restore(path)
        request.app.state.audit_store.append(
            action="update.restore",
            outcome="success",
            summary="Updater rollback restored application state.",
            actor_type="service",
            actor_id="local-updater",
            target_type="backup",
            target_id=_sha256(path),
            request_id=getattr(request.state, "request_id", None),
        )
        return result
    finally:
        await upload.close()
        if path is not None:
            path.unlink(missing_ok=True)
