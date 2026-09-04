from __future__ import annotations

import json
import queue
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SECRET_KEYS = re.compile(
    r"(?:password|passwd|secret|token|authorization|cookie|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
MAX_STRING = 2048
MAX_CONTEXT_BYTES = 16 * 1024


def redact(value: Any, *, key: str = "") -> Any:
    """Recursively remove credentials before an object reaches a persistent sink."""
    if key and SECRET_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:100]]
    if isinstance(value, str):
        cleaned = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
        cleaned = BEARER_VALUE.sub("Bearer [REDACTED]", cleaned)
        if len(cleaned) > MAX_STRING:
            cleaned = cleaned[:MAX_STRING] + "…[TRUNCATED]"
        return cleaned
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    occurred_at: str
    severity: str
    outcome: str
    action: str
    actor_type: str
    actor_id: str
    target_type: str
    target_id: str
    summary: str
    request_id: str | None
    transport: str | None
    context: dict[str, Any]
    error: dict[str, Any] | None


class AuditStore:
    def __init__(self, database_path: Path, retention_days: int, max_events: int, max_bytes: int) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._write_lock = threading.RLock()
        self._queue: queue.Queue[
            tuple[AuditEvent, str, str | None, int] | threading.Event | None
        ] = queue.Queue(maxsize=1000)
        self._worker_error: Exception | None = None
        self._initialize()
        self._worker = threading.Thread(
            target=self._write_loop,
            name="moonli-audit-writer",
            daemon=True,
        )
        self._worker.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    request_id TEXT,
                    transport TEXT,
                    context_json TEXT NOT NULL,
                    error_json TEXT,
                    estimated_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_occurred_at ON audit_events(occurred_at);
                """
            )

    def append(
        self,
        *,
        action: str,
        outcome: str,
        summary: str,
        actor_type: str = "operator",
        actor_id: str = "operator",
        target_type: str = "service",
        target_id: str = "moonli",
        severity: str = "info",
        request_id: str | None = None,
        transport: str | None = "https",
        context: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> AuditEvent:
        safe_context = redact(context or {})
        context_json = json.dumps(safe_context, ensure_ascii=False, separators=(",", ":"))
        if len(context_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
            safe_context = {"truncated": True, "summary": "Audit context exceeded 16 KiB."}
            context_json = json.dumps(safe_context, separators=(",", ":"))
        safe_error = redact(error) if error else None
        error_json = json.dumps(safe_error, ensure_ascii=False, separators=(",", ":")) if safe_error else None
        event = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            occurred_at=datetime.now(UTC).isoformat(),
            severity=severity,
            outcome=outcome,
            action=action[:128],
            actor_type=actor_type[:32],
            actor_id=actor_id[:128],
            target_type=target_type[:64],
            target_id=target_id[:256],
            summary=str(redact(summary))[:MAX_STRING],
            request_id=request_id[:128] if request_id else None,
            transport=transport[:32] if transport else None,
            context=safe_context,
            error=safe_error,
        )
        estimated = len(json.dumps(asdict(event), ensure_ascii=False).encode("utf-8"))
        payload = (event, context_json, error_json, estimated)
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # Preserve security events without allowing an unbounded memory queue.
            self._persist(payload)
        return event

    def _persist(self, payload: tuple[AuditEvent, str, str | None, int]) -> None:
        event, context_json, error_json, estimated = payload
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, occurred_at, severity, outcome, action, actor_type, actor_id,
                    target_type, target_id, summary, request_id, transport, context_json,
                    error_json, estimated_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id, event.occurred_at, event.severity, event.outcome,
                    event.action, event.actor_type, event.actor_id, event.target_type,
                    event.target_id, event.summary, event.request_id, event.transport,
                    context_json, error_json, estimated,
                ),
            )
            self._trim(connection)

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, threading.Event):
                    item.set()
                    continue
                try:
                    self._persist(item)
                except (OSError, sqlite3.Error) as exc:  # pragma: no cover - surfaced by flush
                    self._worker_error = exc
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        marker = threading.Event()
        try:
            self._queue.put(marker, timeout=timeout)
        except queue.Full as exc:
            raise TimeoutError("Audit writer queue did not accept a flush barrier") from exc
        if not marker.wait(timeout):
            raise TimeoutError("Audit writer did not flush within the bounded timeout")
        if self._worker_error is not None:
            error = self._worker_error
            self._worker_error = None
            raise RuntimeError("Audit writer failed") from error

    def close(self) -> None:
        self.flush()
        self._queue.put(None, timeout=1)
        self._worker.join(timeout=2)

    def _trim(self, connection: sqlite3.Connection) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        connection.execute("DELETE FROM audit_events WHERE occurred_at < ?", (cutoff,))
        connection.execute(
            """
            DELETE FROM audit_events WHERE sequence NOT IN (
                SELECT sequence FROM audit_events ORDER BY sequence DESC LIMIT ?
            )
            """,
            (self.max_events,),
        )
        total = int(connection.execute("SELECT COALESCE(SUM(estimated_bytes), 0) FROM audit_events").fetchone()[0])
        while total > self.max_bytes:
            row = connection.execute(
                "SELECT sequence, estimated_bytes FROM audit_events ORDER BY sequence LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM audit_events WHERE sequence = ?", (row["sequence"],))
            total -= int(row["estimated_bytes"])

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "severity": row["severity"],
            "outcome": row["outcome"],
            "action": row["action"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "summary": row["summary"],
            "request_id": row["request_id"],
            "transport": row["transport"],
            "context": json.loads(row["context_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    def page(self, limit: int = 200, before: int | None = None) -> dict[str, Any]:
        self.flush()
        limit = min(max(limit, 1), 1000)
        query = "SELECT * FROM audit_events"
        parameters: tuple[Any, ...]
        if before is not None:
            query += " WHERE sequence < ?"
            parameters = (before, limit + 1)
        else:
            parameters = (limit + 1,)
        query += " ORDER BY sequence DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        return {
            "events": [self._event(row) for row in selected],
            "next_before": int(selected[-1]["sequence"]) if has_more and selected else None,
            "limits": {
                "max_events": self.max_events,
                "retention_days": self.retention_days,
                "max_bytes": self.max_bytes,
            },
        }

    def iter_oldest(self) -> Iterator[dict[str, Any]]:
        self.flush()
        last = 0
        while True:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM audit_events WHERE sequence > ? ORDER BY sequence LIMIT 500",
                    (last,),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last = int(row["sequence"])
                yield self._event(row)

    def replace(self, events: Iterable[dict[str, Any]]) -> None:
        self.flush()
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM audit_events")
            for item in events:
                context_json = json.dumps(redact(item.get("context", {})), ensure_ascii=False)
                error = redact(item.get("error")) if item.get("error") else None
                error_json = json.dumps(error, ensure_ascii=False) if error else None
                estimated = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, occurred_at, severity, outcome, action, actor_type, actor_id,
                        target_type, target_id, summary, request_id, transport, context_json,
                        error_json, estimated_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["event_id"], item["occurred_at"], item["severity"], item["outcome"],
                        item["action"], item["actor_type"], item["actor_id"], item["target_type"],
                        item["target_id"], item["summary"], item.get("request_id"), item.get("transport"),
                        context_json, error_json, estimated,
                    ),
                )
            self._trim(connection)
