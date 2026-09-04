from __future__ import annotations

import json
import zipfile
from dataclasses import replace

import pytest

from app.api.errors import MoonliError
from app.composition import build_components
from app.settings import Settings


def _components(tmp_path):
    settings = replace(
        Settings.from_env(),
        environment="test",
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        operator_access_key="operator-access-key-1234",
        api_keys=("client-api-key-5678",),
    )
    return build_components(settings)


def _seed(components) -> None:
    run, created = components.run_repository.reserve(
        "run_backup", "backup-idempotency-key", "request-hash", "pipeline-1", "text", "palette-v1"
    )
    assert created and run.run_id == "run_backup"
    components.run_repository.set_input("run_backup", "yellow car", None)
    result_key = "completed/run_backup/moonli.png"
    components.artifact_store.put(result_key, b"final-png-bytes")
    components.run_repository.complete("run_backup", result_key, "image/png", "a" * 64)
    components.production_usage_store.record_request("pipeline-1", "text")
    components.device_registry.record_request("td-02941846")
    components.audit_store.append(
        action="test.seed",
        outcome="success",
        summary="Seeded backup state.",
        context={"api_key": "must-not-appear"},
    )


def test_backup_round_trip_and_forbidden_secret_exclusion(tmp_path) -> None:
    components = _components(tmp_path)
    _seed(components)
    components.production_secret_store.set_google_api_key("google-secret-key-123456789")
    archive_path = components.backup_manager.create()
    archive_bytes = archive_path.read_bytes()

    assert b"google-secret-key-123456789" not in archive_bytes
    assert b"must-not-appear" not in archive_bytes
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "moonli-logical-backup"
        assert "data/operator_verifier.json" in manifest["files"]
        assert "data/server_settings.json" in manifest["files"]
        assert "data/devices.jsonl" in manifest["files"]
        assert manifest["schema_version"] == 2
        assert "artifacts/completed/run_backup/moonli.png" in manifest["files"]

    components.production_usage_store.record_request("pipeline-2", "audio")
    components.device_registry.record_request("aa-26093758")
    stale = components.artifact_store.root / "completed" / "stale" / "old.png"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"must-be-replaced")
    result = components.backup_manager.restore(archive_path)
    summary = components.production_usage_store.summary()
    assert result["restored"] is True
    assert summary["requests"] == 1
    assert components.run_repository.get("run_backup") is not None
    assert components.operator_auth_store.verify_access_key("operator-access-key-1234")
    devices, total = components.device_registry.list()
    assert total == 1
    assert devices[0].device_id == "td-02941846"
    assert not stale.exists()


def test_corrupt_member_fails_before_restore_mutation(tmp_path) -> None:
    components = _components(tmp_path)
    _seed(components)
    archive_path = components.backup_manager.create()
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(corrupt, "w") as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == "data/generation_runs.jsonl":
                body += b"{}\n"
            target.writestr(info.filename, body)
    components.production_usage_store.record_request("pipeline-2", "audio")

    with pytest.raises(MoonliError) as raised:
        components.backup_manager.restore(corrupt)

    assert raised.value.code == "BACKUP_INTEGRITY_FAILED"
    assert components.production_usage_store.summary()["requests"] == 2


def test_traversal_member_is_rejected(tmp_path) -> None:
    components = _components(tmp_path)
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape", b"x")
        archive.writestr("manifest.json", b"{}")
    with pytest.raises(MoonliError) as raised:
        components.backup_manager.inspect(archive_path)
    assert raised.value.code == "INVALID_BACKUP"


def test_schema_one_backup_restores_with_empty_device_registry(tmp_path) -> None:
    components = _components(tmp_path)
    _seed(components)
    current = components.backup_manager.create()
    legacy = tmp_path / "schema-one.zip"
    with zipfile.ZipFile(current) as source, zipfile.ZipFile(legacy, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["schema_version"] = 1
        manifest["files"].pop("data/devices.jsonl")
        for info in source.infolist():
            if info.filename in {"manifest.json", "data/devices.jsonl"}:
                continue
            target.writestr(info, source.read(info.filename))
        target.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))

    components.device_registry.record_request("aa-26093758")
    result = components.backup_manager.restore(legacy)
    devices, total = components.device_registry.list()

    assert result["restored"] is True
    assert total == 0
    assert devices == []
