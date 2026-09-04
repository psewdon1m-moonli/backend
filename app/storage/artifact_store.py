from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Protocol


class ArtifactStore(Protocol):
    def put(self, asset_key: str, content: bytes) -> Path: ...

    def open(self, asset_key: str, mode: str = "rb") -> BinaryIO: ...

    def exists(self, asset_key: str) -> bool: ...

    def delete(self, asset_key: str) -> None: ...


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.staging_root = self.root / "staging"
        self.inputs_root = self.root / "inputs"
        self.completed_root = self.root / "completed"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.inputs_root.mkdir(parents=True, exist_ok=True)
        self.completed_root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, asset_key: str) -> Path:
        candidate = (self.root / asset_key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Artifact key escapes storage root")
        return candidate

    def put(self, asset_key: str, content: bytes) -> Path:
        target = self._resolve(asset_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
        return target

    def open(self, asset_key: str, mode: str = "rb") -> BinaryIO:
        if mode not in {"rb", "r"}:
            raise ValueError("ArtifactStore.open is read-only")
        return self._resolve(asset_key).open(mode)

    def exists(self, asset_key: str) -> bool:
        return self._resolve(asset_key).is_file()

    def delete(self, asset_key: str) -> None:
        target = self._resolve(asset_key)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def begin_run(self, run_id: str) -> Path:
        path = self._resolve(f"staging/{run_id}")
        path.mkdir(parents=True, exist_ok=False)
        return path

    def staging_path(self, run_id: str, relative: str = "") -> Path:
        return self._resolve(f"staging/{run_id}/{relative}")

    def publish_run(self, run_id: str) -> Path:
        staging = self._resolve(f"staging/{run_id}")
        completed = self._resolve(f"completed/{run_id}")
        if not staging.is_dir():
            raise FileNotFoundError(f"Missing staging directory for {run_id}")
        if completed.exists():
            raise FileExistsError(f"Completed directory already exists for {run_id}")
        staging.rename(completed)
        return completed

    def completed_asset_key(self, run_id: str, filename: str) -> str:
        return f"completed/{run_id}/{filename}"

    def path_for(self, asset_key: str) -> Path:
        return self._resolve(asset_key)

    def cleanup(self, staging_hours: int, input_hours: int, completed_days: int) -> dict[str, int]:
        now = datetime.now(UTC)
        removed = {"staging": 0, "inputs": 0, "completed": 0}
        policies = (
            (self.staging_root, timedelta(hours=staging_hours), "staging"),
            (self.inputs_root, timedelta(hours=input_hours), "inputs"),
            (self.completed_root, timedelta(days=completed_days), "completed"),
        )
        for root, max_age, label in policies:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                if now - modified > max_age:
                    resolved = entry.resolve()
                    if resolved.parent != root.resolve():
                        raise ValueError("Refusing to clean an unexpected artifact path")
                    shutil.rmtree(resolved)
                    removed[label] += 1
        return removed
