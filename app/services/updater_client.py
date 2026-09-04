from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.api.errors import MoonliError

SEMVER_TAG = re.compile(r"^moonli-v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class UpdaterClient:
    """Narrow client for the local updater Unix socket; never accepts commands or URLs."""

    def __init__(
        self,
        socket_path: Path,
        control_token: str,
        repository_url: str,
        backup_manager,
    ) -> None:
        self.socket_path = socket_path
        self.control_token = control_token
        self.repository_url = repository_url
        self.backup_manager = backup_manager
        parsed = urlparse(repository_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parsed.path.strip("/").split("/")) != 2:
            raise ValueError("Moonli backend repository must be an HTTPS GitHub repository")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.socket_path)),
            base_url="http://updater.local",
            timeout=15,
            headers={"Host": "updater.local"},
        )

    def status(self) -> dict[str, object]:
        if not self.socket_path.exists():
            return {"installed": False, "available": False, "busy": False, "jobs": []}
        try:
            with self._client() as client:
                health = client.get("/v1/health")
                health.raise_for_status()
                jobs = client.get("/v1/jobs")
                jobs.raise_for_status()
                payload = health.json()
                return {
                    "installed": True,
                    "available": True,
                    "busy": bool(payload.get("busy")),
                    "version": payload.get("version"),
                    "jobs": jobs.json().get("jobs", [])[:20],
                }
        except (httpx.HTTPError, ValueError):
            return {"installed": True, "available": False, "busy": False, "jobs": []}

    def start(self, version: str = "") -> dict[str, object]:
        if not self.socket_path.exists() or not self.control_token:
            raise MoonliError("UPDATER_UNAVAILABLE", "The local updater is not installed or configured.", 503)
        if version and not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
            raise MoonliError("INVALID_INPUT", "Update version must be semantic version text.", 422)
        backup = self.backup_manager.create()
        try:
            body = backup.read_bytes()
            if len(body) > 128 * 1024 * 1024:
                raise MoonliError("BACKUP_TOO_LARGE", "Updater backup exceeds 128 MiB.", 413)
            digest = hashlib.sha256(body).hexdigest()
            payload = {
                "request_id": f"moonli-{uuid.uuid4().hex}",
                "head_id": "moonli",
                "service": "moonli",
                "version": version,
                "backup": {
                    "filename": backup.name,
                    "sha256": digest,
                    "data_base64": base64.b64encode(body).decode("ascii"),
                    "restore_url": "http://127.0.0.1:18000/internal/updater/restore",
                },
            }
            try:
                with self._client() as client:
                    response = client.post(
                        "/v1/updates",
                        json=payload,
                        headers={"X-Updater-Token": self.control_token, "Host": "updater.local"},
                        timeout=30,
                    )
            except httpx.HTTPError as exc:
                raise MoonliError("UPDATER_UNAVAILABLE", "The local updater request failed.", 503) from exc
            if response.status_code != 202:
                detail = response.json().get("error", "Update request was rejected.") if response.headers.get("content-type", "").startswith("application/json") else "Update request was rejected."
                raise MoonliError("UPDATE_REJECTED", str(detail)[:500], 409)
            return response.json()
        finally:
            backup.unlink(missing_ok=True)

    def job(self, job_id: str) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", job_id):
            raise MoonliError("INVALID_INPUT", "Invalid update job ID.", 422)
        with self._client() as client:
            response = client.get(f"/v1/jobs/{job_id}")
        if response.status_code == 404:
            raise MoonliError("NOT_FOUND", "Update job was not found.", 404)
        if response.status_code != 200:
            raise MoonliError("UPDATER_UNAVAILABLE", "Could not read update status.", 503)
        return response.json()

    def rollback(self, job_id: str) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", job_id):
            raise MoonliError("INVALID_INPUT", "Invalid update job ID.", 422)
        with self._client() as client:
            response = client.post(
                f"/v1/jobs/{job_id}/rollback",
                headers={"X-Updater-Token": self.control_token, "Host": "updater.local"},
            )
        if response.status_code != 202:
            raise MoonliError("UPDATE_ROLLBACK_REJECTED", "Update rollback was rejected.", 409)
        return response.json()

    def releases(self) -> dict[str, object]:
        owner, repository = self.repository_url.removesuffix(".git").strip("/").split("/")[-2:]
        url = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=30"
        try:
            response = httpx.get(
                url,
                timeout=10,
                follow_redirects=False,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "moonli"},
            )
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MoonliError("RELEASE_CHECK_FAILED", "Could not read Moonli releases from GitHub.", 502) from exc
        releases = []
        for item in raw if isinstance(raw, list) else []:
            tag = item.get("tag_name", "")
            match = SEMVER_TAG.fullmatch(tag)
            if not match or item.get("draft"):
                continue
            releases.append(
                {
                    "version": tag.removeprefix("moonli-v"),
                    "tag": tag,
                    "prerelease": bool(item.get("prerelease")),
                    "published_at": item.get("published_at"),
                }
            )
        return {"repository": self.repository_url, "releases": releases}
