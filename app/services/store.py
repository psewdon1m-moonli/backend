from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import JOBS_DIR, SESSIONS_DIR
from app.models import JobRecord, SessionRecord


def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _session_file(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def save_job(record: JobRecord) -> None:
    record.updated_at = datetime.now(UTC)
    job_path = _job_file(record.job_id)
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text(
        record.model_dump_json(indent=2),
        encoding="utf-8",
    )


def load_job(job_id: str) -> JobRecord | None:
    path = _job_file(job_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return JobRecord(**data)


def save_session(record: SessionRecord) -> None:
    record.updated_at = datetime.now(UTC)
    session_path = _session_file(record.session_id)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def load_session(session_id: str) -> SessionRecord | None:
    path = _session_file(session_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SessionRecord(**data)


def find_job_by_pack_id(pack_id: str) -> JobRecord | None:
    for path in JOBS_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("pack_id") == pack_id:
            return JobRecord(**data)
    return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
