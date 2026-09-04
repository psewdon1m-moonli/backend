from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from app.api.errors import MoonliError
from app.composition import apply_runtime_configuration

router = APIRouter(prefix="/internal/backups", include_in_schema=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _authorize(request: Request) -> None:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=True)


async def _spool_upload(request: Request) -> Path:
    settings = request.app.state.moonli_settings
    try:
        form = await request.form(max_files=1, max_fields=2, max_part_size=settings.backup_max_compressed_bytes)
    except Exception as exc:
        raise MoonliError("INVALID_BACKUP", "Invalid backup upload.", 422) from exc
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise MoonliError("INVALID_BACKUP", "Backup file is required.", 422)
    spool = settings.data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="backup-upload-", suffix=".zip", dir=spool, delete=False
        ) as handle:
            path = Path(handle.name)
            total = 0
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > settings.backup_max_compressed_bytes:
                    raise MoonliError("BACKUP_TOO_LARGE", "Backup upload exceeds the size limit.", 413)
                handle.write(chunk)
            handle.flush()
        if total == 0:
            raise MoonliError("INVALID_BACKUP", "Backup file is empty.", 422)
        return path
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


@router.post("")
def create_backup(request: Request) -> FileResponse:
    _authorize(request)
    path = request.app.state.backup_manager.create()
    created_at = datetime.now(UTC)
    request.app.state.audit_store.append(
        action="backup.create",
        outcome="success",
        summary="A complete logical backup was created.",
        target_type="archive",
        target_id=path.name,
        request_id=getattr(request.state, "request_id", None),
        context={"archive_bytes": path.stat().st_size},
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"moonli-backup-{created_at.strftime('%Y%m%dT%H%M%SZ')}.zip",
        headers={"Cache-Control": "no-store, private"},
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post("/inspect")
async def inspect_backup(request: Request) -> dict[str, object]:
    _authorize(request)
    path = await _spool_upload(request)
    try:
        return request.app.state.backup_manager.inspect(path)
    finally:
        path.unlink(missing_ok=True)


@router.post("/restore")
async def restore_backup(request: Request) -> dict[str, object]:
    _authorize(request)
    path = await _spool_upload(request)
    try:
        form_confirmation = request.query_params.get("confirmation", "")
        if form_confirmation != "RESTORE":
            raise MoonliError(
                "RESTORE_CONFIRMATION_REQUIRED",
                "Set confirmation=RESTORE to replace the current application state.",
                422,
            )
        archive_sha256 = _sha256(path)
        result = request.app.state.backup_manager.restore(path)
        apply_runtime_configuration(request.app)
        request.app.state.audit_store.append(
            action="backup.restore",
            outcome="success",
            summary="Application state was restored from a verified logical backup.",
            target_type="archive",
            target_id=archive_sha256,
            request_id=getattr(request.state, "request_id", None),
            context={"source_version": result["source_version"]},
        )
        return result
    finally:
        path.unlink(missing_ok=True)
