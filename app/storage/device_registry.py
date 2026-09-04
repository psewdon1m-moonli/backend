from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.api.errors import MoonliError

DEVICE_ID = re.compile(r"^(td|aa)-[0-9]{8}$")
CONNECTION_TYPES = {
    "td": ("touch_designer", "touch designer client"),
    "aa": ("android_app", "android app client"),
}


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    connection_type: str
    type_label: str
    request_count: int
    registered_at: str
    last_seen_at: str
    blocked: bool
    blocked_at: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DeviceRegistry:
    """Persistent registry for client-generated, non-secret device identifiers."""

    table_name = "devices"

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    connection_type TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
                    registered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
                    blocked_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_devices_registered_at "
                "ON devices(registered_at DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def validate_identifier(device_id: str | None) -> str:
        value = (device_id or "").strip().lower()
        if not DEVICE_ID.fullmatch(value):
            raise MoonliError(
                "INVALID_DEVICE_ID",
                "X-Moonli-Device-Id must be td-######## or aa-########.",
                422,
            )
        return value

    def record_request(self, device_id: str | None) -> tuple[DeviceRecord, bool]:
        identifier = self.validate_identifier(device_id)
        prefix = identifier[:2]
        connection_type = CONNECTION_TYPES[prefix][0]
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT device_id FROM devices WHERE device_id = ?", (identifier,)
            ).fetchone()
            created = existing is None
            if created:
                connection.execute(
                    """
                    INSERT INTO devices (
                        device_id, connection_type, request_count, registered_at,
                        last_seen_at, blocked, blocked_at
                    ) VALUES (?, ?, 1, ?, ?, 0, NULL)
                    """,
                    (identifier, connection_type, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE devices
                    SET request_count = request_count + 1, last_seen_at = ?
                    WHERE device_id = ?
                    """,
                    (now, identifier),
                )
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (identifier,)
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("Device registration did not persist")
        return self._record(row), created

    def list(self, *, limit: int = 500, offset: int = 0) -> tuple[list[DeviceRecord], int]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0])
            rows = connection.execute(
                """
                SELECT * FROM devices
                ORDER BY registered_at DESC, device_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._record(row) for row in rows], total

    def set_blocked(self, device_id: str, blocked: bool) -> DeviceRecord:
        identifier = self.validate_identifier(device_id)
        blocked_at = datetime.now(UTC).isoformat() if blocked else None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE devices SET blocked = ?, blocked_at = ? WHERE device_id = ?",
                (int(blocked), blocked_at, identifier),
            )
            if cursor.rowcount != 1:
                raise MoonliError("DEVICE_NOT_FOUND", "Device is not registered.", 404)
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (identifier,)
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite invariant
            raise RuntimeError("Device status update did not persist")
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> DeviceRecord:
        connection_type = str(row["connection_type"])
        label = next(
            label for value, label in CONNECTION_TYPES.values() if value == connection_type
        )
        return DeviceRecord(
            device_id=str(row["device_id"]),
            connection_type=connection_type,
            type_label=label,
            request_count=int(row["request_count"]),
            registered_at=str(row["registered_at"]),
            last_seen_at=str(row["last_seen_at"]),
            blocked=bool(row["blocked"]),
            blocked_at=str(row["blocked_at"]) if row["blocked_at"] is not None else None,
        )
