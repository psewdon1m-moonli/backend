# Moonli specification implementation record

Date: 2026-09-04. Backend repository policy:
`https://github.com/psewdon1m-moonli/backend.git`.

## Operator decisions

- Production Google API key remains a browser-entered secret. The backend validates
  it before atomically saving it in `moonli_secrets`; it never enters `.env`, browser
  persistence, logs, API responses, logical backup or release files.
- Optional Google API proxy routing is browser-controlled from Configuration. Its
  VLESS Reality/TCP/Vision connection and generated Xray config are stored only in
  `moonli_secrets` with restricted permissions. API responses expose status only.
- Public deployment uses Nginx and ordinary domain HTTPS. VPN and mTLS are not part of
  the selected topology.
- The local updater is based on
  `https://github.com/psewdon1m-exocortex/updater.git`, pinned for release builds at
  commit `75eff9607dac4f7dbe9f841bd428f53ee01274a1`.
  The release build applies the versioned `deploy/updater-moonli.patch`, which adds a
  tested restart reconciliation rule: interrupted jobs become explicit terminal
  failures and retain manual rollback availability.
- General Part I UI/UX reconciliation is deferred. Only functional Settings,
  Production, Documentation, backup, audit and updater controls are changed here.
- Telegram integration and SEO/GEO are not applicable by operator decision.
- Android and TouchDesigner sources remain unchanged. Their required changes stay in
  `docs/client-changes.md`.

## Applicability matrix

| Specification | Status | Evidence |
| --- | --- | --- |
| Part I — general interface unification | Deferred by operator | Existing layout preserved; no broad redesign |
| Part I — functional Settings/Documentation | Applied | `app/web/index.html`, `app/web/app.js`, `app/web/operations.html` |
| Part II — observability/audit/export | Applied | `app/observability/audit.py`, `/internal/operations/*`, bounded retention/spooled ZIP |
| Part III — backup/recovery | Applied | `app/services/backup.py`, `/internal/backups`, manifest/ZIP/pre-restore tests |
| Part IV — bootstrap/deployment | Applied | `deploy/bootstrap.sh`, `deploy/install.sh`, Compose and Nginx controls |
| Part V — CI/releases/local updater | Applied | pinned workflows, release builder, Unix-socket adapter and UI |
| Part VI — acceptance/pre-push | Applied | `scripts/pre-push.sh`, `.githooks/pre-push`, CI repeat |
| Part VII — security/exposure | Applied | split identities, sessions/CSRF, scan, route registry and Nginx allow-list |
| Telegram outer connection | N/A by operator decision | no receiver, token, callback, webhook or Telegram route |
| SEO/GEO | N/A by operator decision | no public/indexable surface; global non-indexing policy |

## Trust and data flow

| Boundary | Identity | Secret source | Rotation | Exposure |
| --- | --- | --- | --- | --- |
| Android/TouchDesigner → generation | independent client key + persistent device ID | deployment input + client local state | replace key; retain device ID | public/non-indexable HTTPS |
| Browser → operator routes | Access Key → HttpOnly session + CSRF | one-time seed, then scrypt verifier | Settings rotation revokes sessions | private content via HTTPS login |
| Backend → Google | Google API key | browser → dedicated volume | browser delete/replace | direct Google HTTPS or internal Xray route |
| Backend → Xray | Compose-private HTTP proxy | no user credential on the internal hop | Configuration switch | no host port; VLESS secret remains in the shared secrets volume |
| Backend → updater | Unix group + per-service token | root-owned `.env` | root deployment rotation | concealed Unix socket |
| Updater → catalog/restore | separate machine tokens | root-owned `.env` | root deployment rotation | loopback-only API |
| Updater → GitHub/GHCR | fixed repository/release contract | public metadata | repository administration | outbound HTTPS |

The route/listener source of truth is `docs/exposure-registry.json`. Nginx uses an
explicit allow-list and final real `404`; authenticated operator Test Calls are
routed with session and CSRF enforcement, while framework docs, metrics, raw data,
legacy APIs, updater catalog and updater restore are not publicly routed.

## Persistence classification

- Mandatory: generation runs, production usage/token rows, registered devices and
  block state, non-secret settings,
  operator verifier, retained audit records and referenced completed artifacts.
- Derived/excluded: staging/cache files and reproducible images.
- Forbidden: Google/client/updater keys, VLESS routing state, cookies, sessions,
  `.env`, plaintext passwords and temporary update files.
- Restore mode: replace after complete validation; pre-restore snapshot is reapplied
  on mutation failure and active sessions are revoked.

Current backup/database schema generation is `2`. Backup schema `1` remains accepted
as a migration input and restores an empty device registry. Unknown schema or members, unsafe
paths, links, duplicates, oversized content, excessive compression ratios and digest
mismatches are rejected before mutation. Stable releases use `moonli-vX.Y.Z`; the
application updater boundary accepts only a semantic version.

## Verification command

```text
sh scripts/pre-push.sh
```

It runs Ruff, Pytest, compilation, JavaScript syntax, secret/high-risk scanning,
exposure/deployment/documentation contract checks and Compose validation when Docker
is available. CI requires Compose and additionally builds/inspects the image and
probes health, unknown hosts and concealed paths. The release job tests the pinned
updater source and the exact published backend digest, requests OCI SBOM/provenance,
keyless-signs the image and attests the release bundle.

## Explicit supply-chain boundary

The server manifest and bundle are checksum-bound under the fixed GitHub repository;
the OCI image is keyless-signed with provenance/SBOM. The current upstream updater
verifies manifest identity, immutable image digest and compose-bundle checksum but
does not verify a detached asymmetric signature on the server manifest. This is not
claimed as signed-manifest enforcement. Adding it requires a compatible updater
protocol revision and migration/rollback test.

The pinned updater also verifies the Compose archive but reuses the installed Compose
project. In-app update compatibility is therefore limited to image-only releases.
Compose/Nginx/host-unit changes require an explicit maintenance repair/install with an
operator-held snapshot; CI and release notes must classify such a tag accordingly.

The updater's current request contract carries a bounded backup as base64 JSON (128
MiB decoded maximum). Moonli creates the ZIP on disk and deletes its spool copy after
handoff, but the handoff itself has base64 memory overhead. A future streaming updater
protocol is required before raising this limit or claiming fully streaming transport.
