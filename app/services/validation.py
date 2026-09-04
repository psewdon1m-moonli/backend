from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.config import SCHEMAS_DIR
from app.models import ValidationReport


def _load_manifest_schema() -> dict:
    schema_path = SCHEMAS_DIR / "manifest_v1.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_manifest(manifest_path: Path) -> ValidationReport:
    schema = _load_manifest_schema()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda x: x.path)
    if not errors:
        return ValidationReport(valid=True, schema_name="manifest_v1", errors=[])
    messages = []
    for error in errors:
        path = ".".join([str(item) for item in error.path]) or "$"
        messages.append(f"{path}: {error.message}")
    return ValidationReport(valid=False, schema_name="manifest_v1", errors=messages)
