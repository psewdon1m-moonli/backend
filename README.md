# Moonli backend

Moonli owns the complete tagged generation state machine. A client makes one
authenticated request with `pipeline-1`, `pipeline-2`, or `pipeline-3` and
receives the final artifact in the same response.

- `pipeline-1` returns `image/png`.
- `pipeline-2` returns `application/vnd.moonli.layers+zip`.
- `pipeline-3` uses two client operations: audio normalization returns concise Russian
  plain text, then the server translates it into an English image prompt and returns
  a ZIP containing exactly three 1024×1024 JPEG files.
- JSON text and multipart audio use the same endpoint.
- Every resolved text passes through prompt normalization before Prompt Builder.
- Credentials authenticate; only the request tag selects the pipeline.
- Every production call carries a persistent client-generated device identity:
  `td-########` for TouchDesigner or `aa-########` for Android. The prefix records
  the client type and does not restrict any pipeline.
- `Idempotency-Key` prevents duplicate paid generations.
- Provider images are converted to PNG, quantized to the versioned palette, cleaned,
  validated with zero palette tolerance, vectorized/segmented where applicable, and
  published atomically.
- A full-run download contains the applicable input, normalized text, prompt,
  generated/quantized image, validation report, vector and layer artifacts.

Legacy jobs/sessions/step routes remain as a temporary development compatibility
path, but Nginx does not expose them in production. Android and TouchDesigner sources
are not modified by this repository; required integration changes are documented in
`docs/client-changes.md`.

## Local development

Development uses explicit mock providers that produce real PNG artifacts and exercise
the complete processing chain. Production client pipelines reject mock providers;
authenticated Test Calls may use them without changing production configuration.

```powershell
Copy-Item .env.example .env.local
$env:MOONLI_DATA_DIR = "$PWD/data/v1"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Default development credentials are deliberately separate:

- operator UI Access Key: `dev-moonli-operator-key-01`;
- client API key: `dev-moonli-client-key`.

Do not use either value on a server.

## Verification

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -m ruff check app tests scripts
python -m pytest -q
python scripts/security_scan.py
python scripts/verify_contract.py
docker compose config --quiet
docker compose up --build -d --wait
```

The test suite covers profiles, normalization, palette processing, text/audio HTTP
contracts, device registration/blocking, authentication, CSRF, idempotency, atomic
failure, ZIP composition, backup/restore, audit and production configuration. CI
additionally inspects the final image and probes Nginx health, host rejection and
concealed routes.

## Docker Compose and exposure

```powershell
docker compose up --build -d --wait
```

Nginx is the only public listener. The API also has one loopback-only host binding on
`127.0.0.1:18000` for readiness, the local updater catalog and rollback restore. With
`MOONLI_DOMAIN` and `MOONLI_ALLOWED_HOSTS` set to the production hostname, Nginx
terminates TLS and exposes only the UI, health, client generation API and allow-listed
operator routes. Metrics, framework docs, updater machine routes, storage and legacy
routes are not public.

See:

- `docs/api-v1.md` — client HTTP and artifact contracts;
- `docs/deployment.md` — domain-only deployment, recovery and updates;
- `docs/client-changes.md` — required Android and TouchDesigner changes;
- `docs/implementation-record.md` — specification applicability and trust boundaries;
- `docs/exposure-registry.json` — route/listener classification.

## Google activation

Set the three Google providers and exact enabled model names for first production
startup. Then sign in to Moonli and save the separate Google API key and model
configuration for each pipeline on the **Production** page.
The backend validates the key against Google and writes it atomically to the dedicated
`moonli_secrets` volume. It is never persisted in `.env`, browser storage, logs, API
responses, logical backups or release artifacts. Generation returns
`GOOGLE_KEY_NOT_CONFIGURED` until the key exists.

If Google rejects the server region, use **Configuration → Routing** to paste a
VLESS Reality/TCP connection and enable proxy routing. The connection is persisted
only in `moonli_secrets` and is never returned to the browser. Google transcription,
normalization, image generation and key validation then use the internal Xray HTTP
proxy; disabling the switch restores direct routing immediately. The proxy has no
host port and is not a general public gateway.

## Pre-push and releases

```bash
sh scripts/pre-push.sh
git config core.hooksPath .githooks
```

CI repeats the machine-verifiable gate. A stable `moonli-vX.Y.Z` tag builds and
publishes one immutable backend image, bundles the updater pinned to commit
`75eff9607dac4f7dbe9f841bd428f53ee01274a1`, and publishes a checksummed deployment
bundle plus `moonli-release.json`. The web application never receives the Docker
socket and may request only a semantic version from the root-owned updater over its
Unix socket.
