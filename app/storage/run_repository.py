from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.api.errors import MoonliError
from app.domain.runs import GenerationRun, RunStatus


class SqliteRunRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    pipeline_profile TEXT NOT NULL,
                    input_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    original_text TEXT,
                    input_asset_key TEXT,
                    normalized_text TEXT,
                    transcription TEXT,
                    visual_brief_json TEXT,
                    prompt TEXT,
                    provider TEXT,
                    palette_version TEXT,
                    source_asset_key TEXT,
                    result_asset_key TEXT,
                    result_media_type TEXT,
                    result_sha256 TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    trace_json TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(generation_runs)").fetchall()
            }
            if "client_profile" in columns and "pipeline_profile" not in columns:
                connection.execute(
                    "ALTER TABLE generation_runs RENAME COLUMN client_profile TO pipeline_profile"
                )
            if "original_text" not in columns:
                connection.execute("ALTER TABLE generation_runs ADD COLUMN original_text TEXT")
            if "input_asset_key" not in columns:
                connection.execute("ALTER TABLE generation_runs ADD COLUMN input_asset_key TEXT")

    @staticmethod
    def _to_run(row: sqlite3.Row) -> GenerationRun:
        return GenerationRun(
            run_id=row["run_id"],
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            pipeline_profile=row["pipeline_profile"],
            input_type=row["input_type"],
            status=row["status"],
            result_asset_key=row["result_asset_key"],
            result_media_type=row["result_media_type"],
            result_sha256=row["result_sha256"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def reserve(
        self,
        run_id: str,
        idempotency_key: str,
        request_hash: str,
        pipeline_profile: str,
        input_type: str,
        palette_version: str,
    ) -> tuple[GenerationRun, bool]:
        now = datetime.now(UTC).isoformat()
        trace = json.dumps([{"stage": "RECEIVED", "at": now}], ensure_ascii=False)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO generation_runs (
                        run_id, idempotency_key, request_hash, pipeline_profile, input_type,
                        status, created_at, updated_at, palette_version, trace_json
                    ) VALUES (?, ?, ?, ?, ?, 'RECEIVED', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        idempotency_key,
                        request_hash,
                        pipeline_profile,
                        input_type,
                        now,
                        now,
                        palette_version,
                        trace,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM generation_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                assert row is not None
                return self._to_run(row), True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM generation_runs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is None:
                    raise
                existing = self._to_run(row)
                if (
                    existing.request_hash != request_hash
                    or existing.pipeline_profile != pipeline_profile
                ):
                    raise MoonliError(
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency-Key was already used for a different request.",
                        409,
                    )
                return existing, False

    def get(self, run_id: str) -> GenerationRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM generation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._to_run(row) if row else None

    def get_artifact_trace(self, run_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM generation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        visual_brief = json.loads(row["visual_brief_json"]) if row["visual_brief_json"] else None
        execution_trace = json.loads(row["trace_json"]) if row["trace_json"] else []
        return {
            "run_id": row["run_id"],
            "pipeline": row["pipeline_profile"],
            "input_type": row["input_type"],
            "original_text": row["original_text"],
            "normalized_text": row["normalized_text"],
            "transcription": row["transcription"],
            "visual_brief": visual_brief,
            "prompt": row["prompt"],
            "image_provider": row["provider"],
            "palette_version": row["palette_version"],
            "execution_trace": execution_trace,
            "created_at": row["created_at"],
            "completed_at": row["updated_at"],
        }

    def set_stage(self, run_id: str, status: RunStatus) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT trace_json FROM generation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            trace = json.loads(row["trace_json"])
            trace.append({"stage": status, "at": now})
            connection.execute(
                "UPDATE generation_runs SET status = ?, updated_at = ?, trace_json = ? WHERE run_id = ?",
                (status, now, json.dumps(trace, ensure_ascii=False), run_id),
            )

    def set_prompt_trace(
        self,
        run_id: str,
        normalized_text: str,
        transcription: str | None,
        visual_brief: dict[str, object],
        prompt: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE generation_runs
                SET normalized_text = ?, transcription = ?, visual_brief_json = ?, prompt = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    normalized_text,
                    transcription,
                    json.dumps(visual_brief, ensure_ascii=False),
                    prompt,
                    now,
                    run_id,
                ),
            )

    def set_input(self, run_id: str, original_text: str | None, input_asset_key: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE generation_runs
                SET original_text = ?, input_asset_key = ?, updated_at = ? WHERE run_id = ?
                """,
                (original_text, input_asset_key, datetime.now(UTC).isoformat(), run_id),
            )

    def set_source(self, run_id: str, provider: str, source_asset_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE generation_runs SET provider = ?, source_asset_key = ?, updated_at = ? WHERE run_id = ?
                """,
                (provider, source_asset_key, datetime.now(UTC).isoformat(), run_id),
            )

    def complete(self, run_id: str, asset_key: str, media_type: str, sha256: str) -> GenerationRun:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT trace_json FROM generation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            trace = json.loads(row["trace_json"])
            trace.append({"stage": "COMPLETE", "at": now})
            connection.execute(
                """
                UPDATE generation_runs
                SET status = 'COMPLETE', result_asset_key = ?, result_media_type = ?,
                    result_sha256 = ?, updated_at = ?, trace_json = ?
                WHERE run_id = ?
                """,
                (asset_key, media_type, sha256, now, json.dumps(trace, ensure_ascii=False), run_id),
            )
        result = self.get(run_id)
        assert result is not None
        return result

    def fail(self, run_id: str, code: str, message: str) -> GenerationRun:
        self.set_stage(run_id, "FAILED")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE generation_runs
                SET error_code = ?, error_message = ?, updated_at = ? WHERE run_id = ?
                """,
                (code, message, datetime.now(UTC).isoformat(), run_id),
            )
        result = self.get(run_id)
        assert result is not None
        return result

    def fail_stale_in_progress(self, stale_minutes: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).isoformat()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, trace_json FROM generation_runs WHERE status NOT IN ('COMPLETE', 'FAILED') AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                trace = json.loads(row["trace_json"])
                trace.append({"stage": "FAILED", "at": now})
                connection.execute(
                    """
                    UPDATE generation_runs
                    SET status = 'FAILED', error_code = 'INTERNAL_ERROR',
                        error_message = 'Generation was interrupted before completion.',
                        updated_at = ?, trace_json = ? WHERE run_id = ?
                    """,
                    (now, json.dumps(trace, ensure_ascii=False), row["run_id"]),
                )
        return len(rows)

    def apply_retention(self, input_hours: int, completed_days: int) -> dict[str, int]:
        input_cutoff = (datetime.now(UTC) - timedelta(hours=input_hours)).isoformat()
        run_cutoff = (datetime.now(UTC) - timedelta(days=completed_days)).isoformat()
        with self._connect() as connection:
            redacted = connection.execute(
                """
                UPDATE generation_runs
                SET original_text = NULL, transcription = NULL, normalized_text = NULL,
                    visual_brief_json = NULL, prompt = NULL, input_asset_key = NULL
                WHERE updated_at < ? AND (
                    original_text IS NOT NULL OR transcription IS NOT NULL OR
                    normalized_text IS NOT NULL OR visual_brief_json IS NOT NULL OR
                    prompt IS NOT NULL OR input_asset_key IS NOT NULL
                )
                """,
                (input_cutoff,),
            ).rowcount
            deleted = connection.execute(
                """
                DELETE FROM generation_runs
                WHERE updated_at < ? AND status IN ('COMPLETE', 'FAILED')
                """,
                (run_cutoff,),
            ).rowcount
        return {"redacted": redacted, "deleted": deleted}
