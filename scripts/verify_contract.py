from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str, findings: list[str]) -> None:
    if not condition:
        findings.append(message)


def main() -> int:
    findings: list[str] = []
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "deploy/nginx/default.conf.template").read_text(encoding="utf-8")
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    operations = (ROOT / "app/web/operations.html").read_text(encoding="utf-8")
    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release_workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    development_lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    exposure = json.loads((ROOT / "docs/exposure-registry.json").read_text(encoding="utf-8"))

    require("MOONLI_GOOGLE_API_KEY" not in compose, "Google key must not enter Compose/.env", findings)
    require(
        "requirements-dev.lock" in ci_workflow
        and "requirements-dev.lock" in release_workflow,
        "CI and release jobs must install the development lock",
        findings,
    )
    require(
        "ruff==" in development_lock and "pytest==" in development_lock,
        "development lock must include Ruff and pytest",
        findings,
    )
    require(
        "requirements.lock" in dockerfile and "requirements-dev.lock" not in dockerfile,
        "production image must install only the runtime lock",
        findings,
    )
    require('127.0.0.1:${MOONLI_LOOPBACK_API_PORT:-18000}:8000' in compose, "API loopback binding is missing", findings)
    require("/var/run/docker.sock" not in compose, "web stack must not receive Docker socket", findings)
    require("cap_drop:\n      - ALL" in compose, "API capability drop is missing", findings)
    require("read_only: true" in compose, "API read-only root filesystem is missing", findings)
    require("max-size: 10m" in compose and 'max-file: "3"' in compose, "container log rotation is missing", findings)
    require("location = /internal/settings" in nginx, "server settings route is not exposed", findings)
    require("location /internal/updates/" in nginx, "operator updater routes are not exposed", findings)
    require("location /internal/devices" in nginx, "operator device routes are not exposed", findings)
    require("/api/v1/register/snapshot" not in nginx, "updater catalog must stay loopback-only", findings)
    require("/internal/updater/restore" not in nginx, "updater restore must stay loopback-only", findings)
    require('X-Robots-Tag "noindex, nofollow, noarchive"' in nginx, "non-indexing header is missing", findings)
    require('href="/web/operations.html"' in html, "operator Documentation link is missing", findings)
    require('data-view="devices"' in html, "operator Devices view is missing", findings)
    require('data-view="documentation"' in html, "operator Documentation view is missing", findings)
    documentation_contracts = (
        "POST /v1/generate",
        "X-Moonli-Device-Id",
        "moonli-logical-backup",
        "Nginx",
        "Updates and rollback",
        "Android and TouchDesigner integration",
    )
    require(
        all(item in html and item in operations for item in documentation_contracts),
        "operator documentation is incomplete",
        findings,
    )
    require('<html lang="en">' in html and '<html lang="en">' in operations, "web UI language must be English", findings)
    require(not re.search(r"[\u0400-\u04ff]", html + operations), "web UI contains untranslated Cyrillic text", findings)

    modes = {surface.get("mode") for surface in exposure.get("surfaces", [])}
    require(modes <= {"public/non-indexable", "private", "concealed"}, "unknown exposure mode", findings)
    require("public/indexable" not in modes, "SEO/GEO profile must remain out of scope", findings)
    ids = {surface.get("id") for surface in exposure.get("surfaces", [])}
    for required in ("gateway", "client-api", "operator-api", "updater-socket", "api-loopback"):
        require(required in ids, f"missing exposure classification: {required}", findings)

    action_pattern = re.compile(r"uses:\s*[^@\s]+@([^\s#]+)")
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for reference in action_pattern.findall(workflow.read_text(encoding="utf-8")):
            require(bool(re.fullmatch(r"[0-9a-f]{40}", reference)), f"unpinned action in {workflow.name}: {reference}", findings)

    if findings:
        print("Contract verification failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Deployment, exposure, documentation and workflow contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
