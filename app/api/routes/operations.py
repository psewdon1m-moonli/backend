from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

router = APIRouter(prefix="/internal/operations", include_in_schema=False)


def _authorize(request: Request, *, mutate: bool = False) -> None:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=mutate)


@router.get("/audit")
def audit_events(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    before: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    _authorize(request)
    return request.app.state.audit_store.page(limit=limit, before=before)


@router.get("/logs/export")
def export_logs(request: Request) -> FileResponse:
    _authorize(request)
    store = request.app.state.audit_store
    settings = request.app.state.moonli_settings
    created_at = datetime.now(UTC)
    spool = settings.data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="moonli-logs-", suffix=".zip", dir=spool, delete=False
    ) as temporary:
        path = Path(temporary.name)
    count = 0
    digest = hashlib.sha256()
    error_digest = hashlib.sha256()
    error_count = 0
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            with archive.open("events.jsonl", "w") as stream:
                for event in store.iter_oldest():
                    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    stream.write(line)
                    digest.update(line)
                    count += 1
            with archive.open("errors.json", "w") as stream:
                prefix = b"[\n"
                stream.write(prefix)
                error_digest.update(prefix)
                first = True
                for event in store.iter_oldest():
                    if event.get("outcome") not in {"error", "denied"} and not event.get("error"):
                        continue
                    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    chunk = encoded if first else b",\n" + encoded
                    stream.write(chunk)
                    error_digest.update(chunk)
                    first = False
                    error_count += 1
                suffix = b"\n]\n"
                stream.write(suffix)
                error_digest.update(suffix)
            archive.writestr(
                "README.txt",
                "Moonli structured audit export. Timestamps are UTC. Secret fields are centrally redacted.\n",
            )
            manifest = {
                "format": "moonli-log-export",
                "schema_version": 1,
                "service": "moonli",
                "created_at": created_at.isoformat(),
                "event_count": count,
                "error_count": error_count,
                "retention": {
                    "days": store.retention_days,
                    "max_events": store.max_events,
                    "max_bytes": store.max_bytes,
                },
                "files": {
                    "events.jsonl": {"sha256": digest.hexdigest(), "records": count},
                    "errors.json": {
                        "sha256": error_digest.hexdigest(),
                        "records": error_count,
                    },
                },
            }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        store.append(
            action="audit.export",
            outcome="success",
            summary="A bounded audit log archive was created.",
            target_type="archive",
            target_id=path.name,
            request_id=getattr(request.state, "request_id", None),
            context={"event_count": count, "archive_bytes": path.stat().st_size},
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    filename = f"moonli-logs-{created_at.strftime('%Y%m%dT%H%M%SZ')}.zip"
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        headers={"Cache-Control": "no-store, private"},
        background=BackgroundTask(path.unlink, missing_ok=True),
    )
