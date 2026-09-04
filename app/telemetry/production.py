from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def google_token_usage(response_body: dict[str, object]) -> dict[str, int]:
    raw = response_body.get("usageMetadata") or response_body.get("usage_metadata")
    metadata = raw if isinstance(raw, dict) else {}
    prompt = _non_negative_int(
        metadata.get("promptTokenCount", metadata.get("prompt_token_count"))
    )
    output = _non_negative_int(
        metadata.get("candidatesTokenCount", metadata.get("candidates_token_count"))
    )
    thoughts = _non_negative_int(
        metadata.get("thoughtsTokenCount", metadata.get("thoughts_token_count"))
    )
    total = _non_negative_int(
        metadata.get("totalTokenCount", metadata.get("total_token_count"))
    )
    if total == 0:
        total = prompt + output + thoughts
    return {
        "prompt_tokens": prompt,
        "output_tokens": output,
        "thought_tokens": thoughts,
        "total_tokens": total,
    }


class ProductionUsageStore:
    """Persistent counters for calls made through the public production endpoint."""

    def __init__(self, database_path: Path, retention_days: int = 365, max_rows: int = 1_000_000) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.max_rows = max_rows
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS production_api_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    pipeline TEXT NOT NULL,
                    input_type TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_production_api_requests_created_at
                    ON production_api_requests(created_at);

                CREATE TABLE IF NOT EXISTS production_token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    thought_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_production_token_usage_created_at
                    ON production_token_usage(created_at);
                """
            )

    def record_request(self, pipeline: str, input_type: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO production_api_requests (created_at, pipeline, input_type)
                VALUES (?, ?, ?)
                """,
                (datetime.now(UTC).isoformat(), pipeline, input_type),
            )
            self._trim(connection, "production_api_requests")

    def record_google_response(self, stage: str, response_body: dict[str, object]) -> None:
        usage = google_token_usage(response_body)
        if usage["total_tokens"] <= 0:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO production_token_usage (
                    created_at, stage, prompt_tokens, output_tokens,
                    thought_tokens, total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    stage,
                    usage["prompt_tokens"],
                    usage["output_tokens"],
                    usage["thought_tokens"],
                    usage["total_tokens"],
                ),
            )
            self._trim(connection, "production_token_usage")

    def trim(self) -> None:
        with self._connect() as connection:
            self._trim(connection, "production_api_requests")
            self._trim(connection, "production_token_usage")

    def _trim(self, connection: sqlite3.Connection, table: str) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        connection.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
        connection.execute(
            f"""
            DELETE FROM {table} WHERE id NOT IN (
                SELECT id FROM {table} ORDER BY id DESC LIMIT ?
            )
            """,
            (self.max_rows,),
        )

    def summary(self, hours: int = 24) -> dict[str, object]:
        hours = min(max(hours, 1), 168)
        current_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        first_hour = current_hour - timedelta(hours=hours - 1)
        buckets = [first_hour + timedelta(hours=index) for index in range(hours)]
        request_counts = {bucket.strftime("%Y-%m-%dT%H"): 0 for bucket in buckets}
        token_counts = {bucket.strftime("%Y-%m-%dT%H"): 0 for bucket in buckets}
        cutoff = first_hour.isoformat()

        with self._connect() as connection:
            total_requests = int(
                connection.execute("SELECT COUNT(*) FROM production_api_requests").fetchone()[0]
            )
            total_tokens = int(
                connection.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) FROM production_token_usage"
                ).fetchone()[0]
            )
            request_rows = connection.execute(
                """
                SELECT substr(created_at, 1, 13) AS bucket, COUNT(*) AS value
                FROM production_api_requests
                WHERE created_at >= ?
                GROUP BY bucket
                """,
                (cutoff,),
            ).fetchall()
            token_rows = connection.execute(
                """
                SELECT substr(created_at, 1, 13) AS bucket,
                       COALESCE(SUM(total_tokens), 0) AS value
                FROM production_token_usage
                WHERE created_at >= ?
                GROUP BY bucket
                """,
                (cutoff,),
            ).fetchall()

        for row in request_rows:
            if row["bucket"] in request_counts:
                request_counts[row["bucket"]] = int(row["value"])
        for row in token_rows:
            if row["bucket"] in token_counts:
                token_counts[row["bucket"]] = int(row["value"])

        series = [
            {
                "at": bucket.isoformat().replace("+00:00", "Z"),
                "requests": request_counts[bucket.strftime("%Y-%m-%dT%H")],
                "tokens": token_counts[bucket.strftime("%Y-%m-%dT%H")],
            }
            for bucket in buckets
        ]
        return {
            "requests": total_requests,
            "tokens": total_tokens,
            "window_hours": hours,
            "window_requests": sum(item["requests"] for item in series),
            "window_tokens": sum(item["tokens"] for item in series),
            "series": series,
        }


class SystemMonitor:
    """Small dependency-free Linux server monitor with safe test fallbacks."""

    def __init__(self, disk_path: Path) -> None:
        self._disk_path = disk_path.resolve()
        self._lock = threading.Lock()
        self._previous_cpu = self._read_cpu_times()

    @staticmethod
    def _read_cpu_times() -> tuple[int, int] | None:
        try:
            fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
            values = [int(value) for value in fields[1:]]
        except (OSError, ValueError, IndexError):
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def _cpu_percent(self) -> float:
        current = self._read_cpu_times()
        if current is None:
            try:
                return round(min(100.0, os.getloadavg()[0] / max(1, os.cpu_count() or 1) * 100), 1)
            except (AttributeError, OSError):
                return 0.0
        with self._lock:
            previous = self._previous_cpu
            self._previous_cpu = current
        if previous is None:
            previous = (0, 0)
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100)), 1)

    @staticmethod
    def _memory() -> tuple[int, int]:
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
            total = values["MemTotal"]
            available = values.get("MemAvailable", values.get("MemFree", 0))
            return max(0, total - available), total
        except (OSError, ValueError, KeyError):
            return 0, 0

    @staticmethod
    def _uptime_seconds() -> int:
        try:
            return max(0, int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])))
        except (OSError, ValueError, IndexError):
            return 0

    def snapshot(self) -> dict[str, object]:
        memory_used, memory_total = self._memory()
        try:
            disk = shutil.disk_usage(self._disk_path)
            disk_used, disk_total = disk.used, disk.total
        except OSError:
            disk_used, disk_total = 0, 0
        return {
            "cpu_percent": self._cpu_percent(),
            "ram": {
                "used_bytes": memory_used,
                "total_bytes": memory_total,
                "percent": round(memory_used / memory_total * 100, 1) if memory_total else 0.0,
            },
            "disk": {
                "used_bytes": disk_used,
                "total_bytes": disk_total,
                "percent": round(disk_used / disk_total * 100, 1) if disk_total else 0.0,
            },
            "uptime_seconds": self._uptime_seconds(),
        }
