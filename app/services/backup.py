from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.api.errors import MoonliError

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
TABLES = {
    "data/generation_runs.jsonl": ("runs", "generation_runs"),
    "data/production_api_requests.jsonl": ("usage", "production_api_requests"),
    "data/production_token_usage.jsonl": ("usage", "production_token_usage"),
    "data/devices.jsonl": ("devices", "devices"),
}


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and not path.drive
        and ".." not in path.parts
        and "\\" not in name
    )


def _rows(database: Path, table: str) -> Iterator[dict[str, Any]]:
    if not database.exists():
        return
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(f"SELECT * FROM {table}")
        while batch := cursor.fetchmany(500):
            for row in batch:
                yield dict(row)
    finally:
        connection.close()


class BackupManager:
    """Bounded logical export/restore owned by the Moonli application."""

    def __init__(
        self,
        *,
        data_dir: Path,
        runs_database: Path,
        usage_database: Path,
        device_database: Path,
        audit_store,
        operator_auth_store,
        server_settings_store,
        max_compressed_bytes: int,
        max_uncompressed_bytes: int,
        app_version: str,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.runs_database = runs_database.resolve()
        self.usage_database = usage_database.resolve()
        self.device_database = device_database.resolve()
        self.audit_store = audit_store
        self.operator_auth_store = operator_auth_store
        self.server_settings_store = server_settings_store
        self.max_compressed_bytes = max_compressed_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.app_version = app_version
        self.spool_dir = self.data_dir / "spool"
        self.restore_points_dir = self.data_dir / "restore-points"
        self._lock = threading.RLock()

    def create(self, *, destination: Path | None = None) -> Path:
        with self._lock:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            if destination is None:
                with tempfile.NamedTemporaryFile(
                    prefix="moonli-backup-", suffix=".zip", dir=self.spool_dir, delete=False
                ) as handle:
                    path = Path(handle.name)
            else:
                path = destination.resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
            files: dict[str, dict[str, Any]] = {}
            total_uncompressed = 0
            try:
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for member, (database_kind, table) in TABLES.items():
                        database = self._database(database_kind)
                        digest = hashlib.sha256()
                        size = 0
                        count = 0
                        with archive.open(member, "w") as stream:
                            for row in _rows(database, table):
                                line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                                size += len(line)
                                self._check_total(total_uncompressed + size)
                                stream.write(line)
                                digest.update(line)
                                count += 1
                        total_uncompressed += size
                        files[member] = {
                            "sha256": digest.hexdigest(),
                            "uncompressed_bytes": size,
                            "records": count,
                        }

                    audit_digest = hashlib.sha256()
                    audit_size = 0
                    audit_count = 0
                    with archive.open("diagnostics/audit.jsonl", "w") as stream:
                        for event in self.audit_store.iter_oldest():
                            line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                            audit_size += len(line)
                            self._check_total(total_uncompressed + audit_size)
                            stream.write(line)
                            audit_digest.update(line)
                            audit_count += 1
                    total_uncompressed += audit_size
                    files["diagnostics/audit.jsonl"] = {
                        "sha256": audit_digest.hexdigest(),
                        "uncompressed_bytes": audit_size,
                        "records": audit_count,
                        "diagnostic_only": False,
                    }

                    verifier_body = json.dumps(
                        self.operator_auth_store.export_verifier(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._check_total(total_uncompressed + len(verifier_body))
                    archive.writestr("data/operator_verifier.json", verifier_body)
                    total_uncompressed += len(verifier_body)
                    files["data/operator_verifier.json"] = self._metadata(verifier_body, records=1)

                    settings_body = json.dumps(
                        self.server_settings_store.get(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._check_total(total_uncompressed + len(settings_body))
                    archive.writestr("data/server_settings.json", settings_body)
                    total_uncompressed += len(settings_body)
                    files["data/server_settings.json"] = self._metadata(settings_body, records=1)

                    for artifact_name, artifact_path in self._result_artifacts():
                        size = artifact_path.stat().st_size
                        self._check_total(total_uncompressed + size)
                        digest = hashlib.sha256()
                        with artifact_path.open("rb") as source, archive.open(artifact_name, "w") as target:
                            while chunk := source.read(1024 * 1024):
                                target.write(chunk)
                                digest.update(chunk)
                        total_uncompressed += size
                        files[artifact_name] = {
                            "sha256": digest.hexdigest(),
                            "uncompressed_bytes": size,
                            "records": 0,
                        }

                    readme = (
                        b"Moonli logical backup. Restore mode is replace. Google API keys, "
                        b"browser sessions, client tokens and deployment .env files are excluded.\n"
                    )
                    archive.writestr("README.txt", readme)
                    files["README.txt"] = self._metadata(readme, records=0)
                    manifest = {
                        "format": "moonli-logical-backup",
                        "schema_version": SCHEMA_VERSION,
                        "created_at": datetime.now(UTC).isoformat(),
                        "scope": "complete-application-state",
                        "source_version": self.app_version,
                        "restore_mode": "replace",
                        "forbidden_content_excluded": [
                            "google_api_key", "client_api_keys", "updater_tokens", ".env", "sessions"
                        ],
                        "files": files,
                    }
                    archive.writestr(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
                if path.stat().st_size > self.max_compressed_bytes:
                    raise MoonliError("BACKUP_TOO_LARGE", "Backup exceeds the compressed size limit.", 413)
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
                return path
            except Exception:
                path.unlink(missing_ok=True)
                raise

    def inspect(self, path: Path) -> dict[str, Any]:
        validated = self._validate(path)
        return {
            "valid": True,
            "format": validated["manifest"]["format"],
            "schema_version": validated["manifest"]["schema_version"],
            "source_version": validated["manifest"]["source_version"],
            "created_at": validated["manifest"]["created_at"],
            "scope": validated["manifest"]["scope"],
            "compressed_bytes": path.stat().st_size,
            "uncompressed_bytes": validated["uncompressed_bytes"],
        }

    def restore(self, path: Path) -> dict[str, Any]:
        with self._lock:
            validated = self._validate(path)
            self.restore_points_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            restore_point = self.restore_points_dir / f"pre-restore-{timestamp}.zip"
            self.create(destination=restore_point)
            try:
                self._apply(path, validated)
            except Exception as exc:
                rollback = self._validate(restore_point)
                self._apply(restore_point, rollback)
                raise MoonliError(
                    "RESTORE_FAILED",
                    "Restore failed; the pre-restore snapshot was reapplied.",
                    500,
                ) from exc
            self._trim_restore_points()
            return {
                "restored": True,
                "source_version": validated["manifest"]["source_version"],
                "pre_restore_snapshot": restore_point.name,
                "sessions_revoked": True,
            }

    def _validate(self, path: Path) -> dict[str, Any]:
        if path.stat().st_size > self.max_compressed_bytes:
            raise MoonliError("BACKUP_TOO_LARGE", "Backup exceeds the compressed size limit.", 413)
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise MoonliError("INVALID_BACKUP", "Backup is not a valid ZIP archive.", 422) from exc
        with archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) > MAX_MEMBERS or len(names) != len(set(names)):
                raise MoonliError("INVALID_BACKUP", "Backup member count or uniqueness is invalid.", 422)
            if "manifest.json" not in names:
                raise MoonliError("INVALID_BACKUP", "Backup manifest is missing.", 422)
            total = 0
            for info in infos:
                if not _safe_member(info.filename) or info.is_dir():
                    raise MoonliError("INVALID_BACKUP", "Backup contains an unsafe member.", 422)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise MoonliError("INVALID_BACKUP", "Backup links are forbidden.", 422)
                if info.file_size > MAX_MEMBER_BYTES:
                    raise MoonliError("INVALID_BACKUP", "A backup member is too large.", 422)
                if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise MoonliError("INVALID_BACKUP", "Backup compression ratio is unsafe.", 422)
                total += info.file_size
                self._check_total(total)
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (json.JSONDecodeError, KeyError) as exc:
                raise MoonliError("INVALID_BACKUP", "Backup manifest is invalid.", 422) from exc
            if (
                manifest.get("format") != "moonli-logical-backup"
                or manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS
                or manifest.get("restore_mode") != "replace"
                or not isinstance(manifest.get("files"), dict)
            ):
                raise MoonliError("UNSUPPORTED_BACKUP", "Backup format or schema is unsupported.", 422)
            expected = set(manifest["files"]) | {"manifest.json"}
            if set(names) != expected:
                raise MoonliError("INVALID_BACKUP", "Backup contains missing or unknown members.", 422)
            for name, metadata in manifest["files"].items():
                digest = hashlib.sha256()
                size = 0
                with archive.open(name) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                if digest.hexdigest() != metadata.get("sha256") or size != metadata.get("uncompressed_bytes"):
                    raise MoonliError("BACKUP_INTEGRITY_FAILED", f"Backup member {name} failed integrity verification.", 422)
            for name, (kind, table) in TABLES.items():
                if name not in manifest["files"]:
                    if manifest["schema_version"] == 1 and kind == "devices":
                        continue
                    raise MoonliError("INVALID_BACKUP", f"Backup member {name} is missing.", 422)
                database = self._database(kind)
                count = self._validate_jsonl(
                    archive,
                    name,
                    expected_columns=set(self._table_columns(database, table)),
                )
                if count != manifest["files"][name].get("records"):
                    raise MoonliError("INVALID_BACKUP", f"Record count mismatch in {name}.", 422)
            audit_count = self._validate_jsonl(
                archive,
                "diagnostics/audit.jsonl",
                expected_columns={
                    "sequence", "event_id", "occurred_at", "severity", "outcome",
                    "action", "actor_type", "actor_id", "target_type", "target_id",
                    "summary", "request_id", "transport", "context", "error",
                },
            )
            if audit_count != manifest["files"]["diagnostics/audit.jsonl"].get("records"):
                raise MoonliError(
                    "INVALID_BACKUP", "Record count mismatch in diagnostics/audit.jsonl.", 422
                )
            verifier = json.loads(archive.read("data/operator_verifier.json"))
            if not self._valid_verifier(verifier):
                raise MoonliError("INVALID_BACKUP", "Operator verifier is invalid.", 422)
            try:
                server_settings = self.server_settings_store.validate(
                    json.loads(archive.read("data/server_settings.json"))
                )
                self.server_settings_store.candidate_settings(server_settings).validate()
            except (KeyError, json.JSONDecodeError, ValueError) as exc:
                raise MoonliError("INVALID_BACKUP", "Server settings are invalid.", 422) from exc
            return {
                "manifest": manifest,
                "verifier": verifier,
                "server_settings": server_settings,
                "uncompressed_bytes": total,
            }

    def _apply(self, path: Path, validated: dict[str, Any]) -> None:
        with zipfile.ZipFile(path) as archive:
            for member, (kind, table) in TABLES.items():
                database = self._database(kind)
                rows = (
                    self._jsonl_rows(archive, member)
                    if member in validated["manifest"]["files"]
                    else iter(())
                )
                self._replace_table(database, table, rows)
            self.audit_store.replace(
                self._jsonl_rows(archive, "diagnostics/audit.jsonl")
            )
            self.operator_auth_store.import_verifier(validated["verifier"])
            self.server_settings_store.set(validated["server_settings"])
            staging = Path(tempfile.mkdtemp(prefix="restore-artifacts-", dir=self.spool_dir))
            previous = self.spool_dir / f"previous-completed-{uuid.uuid4().hex}"
            try:
                staged_completed = staging / "completed"
                staged_completed.mkdir(parents=True, exist_ok=True)
                for name in validated["manifest"]["files"]:
                    if not name.startswith("artifacts/"):
                        continue
                    relative = PurePosixPath(name).relative_to("artifacts")
                    target = staging.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                completed = self.data_dir / "artifacts" / "completed"
                completed.parent.mkdir(parents=True, exist_ok=True)
                if completed.exists():
                    os.replace(completed, previous)
                try:
                    os.replace(staged_completed, completed)
                except Exception:
                    if previous.exists() and not completed.exists():
                        os.replace(previous, completed)
                    raise
                shutil.rmtree(previous, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(previous, ignore_errors=True)

    def _result_artifacts(self) -> Iterator[tuple[str, Path]]:
        for row in _rows(self.runs_database, "generation_runs"):
            key = row.get("result_asset_key")
            if not isinstance(key, str) or not key.startswith("completed/"):
                continue
            relative = PurePosixPath(key)
            if not _safe_member(key):
                continue
            path = self.data_dir / "artifacts" / Path(*relative.parts)
            if path.is_file():
                yield f"artifacts/{relative.as_posix()}", path

    @staticmethod
    def _metadata(body: bytes, records: int) -> dict[str, Any]:
        return {
            "sha256": hashlib.sha256(body).hexdigest(),
            "uncompressed_bytes": len(body),
            "records": records,
        }

    def _database(self, kind: str) -> Path:
        if kind == "runs":
            return self.runs_database
        if kind == "usage":
            return self.usage_database
        if kind == "devices":
            return self.device_database
        raise ValueError(f"Unknown database kind: {kind}")

    def _check_total(self, total: int) -> None:
        if total > self.max_uncompressed_bytes:
            raise MoonliError("BACKUP_TOO_LARGE", "Backup exceeds the uncompressed size limit.", 413)

    @staticmethod
    def _jsonl_rows(
        archive: zipfile.ZipFile, name: str
    ) -> Iterator[dict[str, Any]]:
        with archive.open(name) as stream:
            for number, raw in enumerate(stream, start=1):
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise MoonliError("INVALID_BACKUP", f"Invalid JSONL in {name}:{number}.", 422) from exc
                if not isinstance(value, dict):
                    raise MoonliError("INVALID_BACKUP", f"Invalid record in {name}:{number}.", 422)
                yield value

    @classmethod
    def _validate_jsonl(
        cls,
        archive: zipfile.ZipFile,
        name: str,
        *,
        expected_columns: set[str],
    ) -> int:
        count = 0
        for value in cls._jsonl_rows(archive, name):
            if set(value) != expected_columns:
                raise MoonliError("INVALID_BACKUP", f"Record schema mismatch in {name}.", 422)
            count += 1
        return count

    @staticmethod
    def _table_columns(database: Path, table: str) -> list[str]:
        with sqlite3.connect(database, timeout=30) as connection:
            return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]

    @classmethod
    def _replace_table(
        cls,
        database: Path,
        table: str,
        rows: Iterator[dict[str, Any]],
    ) -> None:
        connection = sqlite3.connect(database, timeout=30)
        try:
            columns = cls._table_columns(database, table)
            if not columns:
                raise ValueError(f"Unknown restore table: {table}")
            with connection:
                connection.execute(f"DELETE FROM {table}")
                batch: list[tuple[Any, ...]] = []
                for row in rows:
                    if set(row) != set(columns):
                        raise ValueError(f"Restore schema mismatch for {table}")
                    batch.append(tuple(row[column] for column in columns))
                    if len(batch) == 500:
                        cls._insert_batch(connection, table, columns, batch)
                        batch.clear()
                if batch:
                    cls._insert_batch(connection, table, columns, batch)
        finally:
            connection.close()

    @staticmethod
    def _insert_batch(
        connection: sqlite3.Connection,
        table: str,
        columns: list[str],
        batch: list[tuple[Any, ...]],
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        names = ",".join(columns)
        connection.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            batch,
        )

    @staticmethod
    def _valid_verifier(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            return (
                value["algorithm"] == "scrypt-n16384-r8-p1"
                and len(bytes.fromhex(value["salt_hex"])) == 16
                and len(bytes.fromhex(value["verifier_hex"])) == 32
                and isinstance(value["updated_at"], str)
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _trim_restore_points(self) -> None:
        paths = sorted(self.restore_points_dir.glob("pre-restore-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in paths[5:]:
            path.unlink(missing_ok=True)
