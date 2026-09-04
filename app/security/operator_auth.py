from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request

from app.api.errors import MoonliError

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32


@dataclass(frozen=True)
class OperatorSession:
    token: str
    csrf_token: str
    expires_at: str


@dataclass(frozen=True)
class AuthenticatedOperator:
    actor_id: str = "operator"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _derive(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        value.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN
    )


class OperatorAuthStore:
    """Persistent Access-Key verifier and short-lived, revocable browser sessions."""

    def __init__(self, database_path: Path, bootstrap_access_key: str, ttl_minutes: int) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_minutes = ttl_minutes
        self._lock = threading.RLock()
        self._initialize()
        if bootstrap_access_key:
            self.initialize_verifier(bootstrap_access_key)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operator_credential (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    algorithm TEXT NOT NULL,
                    salt_hex TEXT NOT NULL,
                    verifier_hex TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operator_sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    user_agent_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operator_sessions_expires
                    ON operator_sessions(expires_at);
                """
            )

    def initialized(self) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM operator_credential WHERE singleton = 1"
            ).fetchone() is not None

    def initialize_verifier(self, access_key: str) -> bool:
        self._validate_key(access_key)
        salt = secrets.token_bytes(16)
        verifier = _derive(access_key, salt)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO operator_credential (
                    singleton, algorithm, salt_hex, verifier_hex, updated_at
                ) VALUES (1, 'scrypt-n16384-r8-p1', ?, ?, ?)
                """,
                (salt.hex(), verifier.hex(), datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    def verify_access_key(self, access_key: str) -> bool:
        if not access_key:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT salt_hex, verifier_hex FROM operator_credential WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return False
        try:
            candidate = _derive(access_key, bytes.fromhex(row["salt_hex"]))
            expected = bytes.fromhex(row["verifier_hex"])
        except (ValueError, TypeError):
            return False
        return secrets.compare_digest(candidate, expected)

    def create_session(self, access_key: str, user_agent: str = "") -> OperatorSession:
        if not self.verify_access_key(access_key):
            raise MoonliError("UNAUTHORIZED", "Invalid operator Access Key.", 401)
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=self._ttl_minutes)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM operator_sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                """
                INSERT INTO operator_sessions (
                    token_hash, csrf_hash, created_at, expires_at, user_agent_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _digest(token),
                    _digest(csrf_token),
                    now.isoformat(),
                    expires.isoformat(),
                    _digest(user_agent[:512]) if user_agent else None,
                ),
            )
            connection.execute(
                """
                DELETE FROM operator_sessions WHERE token_hash NOT IN (
                    SELECT token_hash FROM operator_sessions ORDER BY created_at DESC LIMIT 10
                )
                """
            )
        return OperatorSession(token=token, csrf_token=csrf_token, expires_at=expires.isoformat())

    def authenticate_request(self, request: Request, *, require_csrf: bool = False) -> AuthenticatedOperator:
        cookie_name = request.app.state.moonli_settings.operator_cookie_name
        token = request.cookies.get(cookie_name, "")
        if not token:
            raise MoonliError("UNAUTHORIZED", "Operator session is required.", 401)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT csrf_hash FROM operator_sessions
                WHERE token_hash = ? AND expires_at > ?
                """,
                (_digest(token), now),
            ).fetchone()
        if row is None:
            raise MoonliError("UNAUTHORIZED", "Operator session is invalid or expired.", 401)
        if require_csrf:
            provided = request.headers.get("X-CSRF-Token", "")
            if not provided or not secrets.compare_digest(_digest(provided), row["csrf_hash"]):
                raise MoonliError("CSRF_FAILED", "A valid CSRF token is required.", 403)
        return AuthenticatedOperator()

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM operator_sessions WHERE token_hash = ?", (_digest(token),))

    def rotate_access_key(self, current: str, replacement: str) -> None:
        if not self.verify_access_key(current):
            raise MoonliError("UNAUTHORIZED", "Current operator Access Key is invalid.", 401)
        self._validate_key(replacement)
        if secrets.compare_digest(current, replacement):
            raise MoonliError("INVALID_INPUT", "The new Access Key must be different.", 422)
        salt = secrets.token_bytes(16)
        verifier = _derive(replacement, salt)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE operator_credential
                SET algorithm = 'scrypt-n16384-r8-p1', salt_hex = ?, verifier_hex = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (salt.hex(), verifier.hex(), datetime.now(UTC).isoformat()),
            )
            connection.execute("DELETE FROM operator_sessions")

    def export_verifier(self) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT algorithm, salt_hex, verifier_hex, updated_at FROM operator_credential WHERE singleton = 1"
            ).fetchone()
        return dict(row) if row is not None else {}

    def import_verifier(self, value: dict[str, str]) -> None:
        salt = bytes.fromhex(value["salt_hex"])
        verifier = bytes.fromhex(value["verifier_hex"])
        if (
            value.get("algorithm") != "scrypt-n16384-r8-p1"
            or len(salt) != 16
            or len(verifier) != DKLEN
        ):
            raise ValueError("Unsupported operator verifier")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO operator_credential (
                    singleton, algorithm, salt_hex, verifier_hex, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    algorithm=excluded.algorithm,
                    salt_hex=excluded.salt_hex,
                    verifier_hex=excluded.verifier_hex,
                    updated_at=excluded.updated_at
                """,
                (
                    value["algorithm"], value["salt_hex"], value["verifier_hex"],
                    value["updated_at"],
                ),
            )
            connection.execute("DELETE FROM operator_sessions")

    @staticmethod
    def _validate_key(value: str) -> None:
        if len(value) < 24 or len(value) > 512 or any(character.isspace() for character in value):
            raise ValueError("Operator Access Key must contain 24-512 non-whitespace characters")


class LoginRateLimiter:
    """Small in-memory brake for repeated operator login failures."""

    def __init__(self, attempts: int = 5, window_seconds: int = 60) -> None:
        self._attempts = attempts
        self._window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, identifier: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._failures.get(identifier, []) if now - stamp < self._window_seconds]
            self._failures[identifier] = recent
            if len(recent) >= self._attempts:
                raise MoonliError("RATE_LIMITED", "Too many login attempts.", 429, self._window_seconds)

    def fail(self, identifier: str) -> None:
        with self._lock:
            self._failures.setdefault(identifier, []).append(time.monotonic())

    def success(self, identifier: str) -> None:
        with self._lock:
            self._failures.pop(identifier, None)
