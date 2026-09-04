from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".repo-audit",
    ".ruff_cache",
    ".temp",
    ".venv",
    "__pycache__",
    "artifacts",
    "audio_handler",
    "data",
    "video_project",
}
ALLOWED_SPECIAL_FILES = {".env.example", "deploy/.env.production.example"}
HIGH_RISK_SUFFIXES = {".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".ogg", ".wav", ".mp4", ".bin"}
SECRET_PATTERNS = {
    "Google API key": re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    "GitHub token": re.compile(rb"gh[pousr]_[0-9A-Za-z]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Slack token": re.compile(rb"xox[baprs]-[0-9A-Za-z-]{20,}"),
}


def candidate_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        paths = [ROOT / line for line in result.stdout.splitlines() if line]
        if paths:
            return paths
    except (OSError, subprocess.CalledProcessError):
        pass
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts)
    ]


def main() -> int:
    findings: list[str] = []
    for path in candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative == ".env" or (
            relative.startswith(".env.") and relative not in ALLOWED_SPECIAL_FILES
        ):
            continue
        if any(part in IGNORED_PARTS for part in Path(relative).parts):
            continue
        lower = relative.lower()
        if (
            (Path(lower).name == ".env" or Path(lower).suffix in HIGH_RISK_SUFFIXES)
            and relative not in ALLOWED_SPECIAL_FILES
        ):
            findings.append(f"high-risk file: {relative}")
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            body = path.read_bytes()
        except OSError as exc:
            findings.append(f"unreadable candidate: {relative}: {exc}")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(body):
                findings.append(f"{label}: {relative}")
        if relative not in ALLOWED_SPECIAL_FILES:
            for name in (b"MOONLI_GOOGLE_API_KEY", b"TABLE_GEN_NANO_BANANA_PRO_API_KEY"):
                if re.search(rb"(?m)^" + name + rb"\s*=\s*\S+", body):
                    findings.append(f"provider key assignment: {relative}")

    if findings:
        print("Security scan failed:", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Security scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
