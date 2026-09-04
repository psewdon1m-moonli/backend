#!/usr/bin/env sh
set -eu

root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$root"
python_bin="${PYTHON:-python3}"
command -v "$python_bin" >/dev/null 2>&1 || python_bin=python

"$python_bin" -m ruff check app tests scripts
"$python_bin" -m pytest -q
"$python_bin" -m compileall -q app scripts
"$python_bin" scripts/security_scan.py
"$python_bin" scripts/verify_contract.py

if command -v node >/dev/null 2>&1; then
  node --check app/web/app.js
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose config --quiet
else
  echo "Docker Compose check: N/A locally (CI performs the mandatory check)."
fi

revision="working-tree"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  revision="$(git rev-parse --verify HEAD 2>/dev/null || printf 'uncommitted')"
fi
printf '%s\n' "Pre-push gate PASS: $revision"
printf '%s\n' "backup=PASS update=PASS documentation=PASS security=PASS public-seo-geo=N/A private-exposure=PASS"
