# Domain-only Docker Compose deployment

## Before deployment

1. Point an `A`/`AAAA` record for the Moonli hostname to the server.
2. Allow inbound TCP 80/443. Do not expose 8000 or 18000 externally.
3. Put a trusted certificate at `/srv/moonli/tls/fullchain.pem` and private key at
   `/srv/moonli/tls/privkey.pem`.
4. Use independent random operator and per-client credentials.
5. Configure exact Google image, transcription and normalization model names. Do not
   place the Google API key in `.env`; save it from the authenticated Production page.
6. Never copy a workstation `.env` to the server.

Production rejects mock providers, development credentials, missing model names,
wildcard hosts and `MOONLI_GOOGLE_API_KEY`. It can start without a Google key so the
operator can enter it in the browser; generation stays unavailable until then.

## Bootstrap and install

Use the bootstrap from an immutable reviewed backend commit:

```bash
curl --proto '=https' --tlsv1.2 --fail --show-error --location \
  'https://raw.githubusercontent.com/psewdon1m-moonli/backend/<trusted-commit-sha>/deploy/bootstrap.sh' \
  | sudo sh
sudoedit /opt/moonli/.env
sudo moonli-admin install
```

Bootstrap supports Linux amd64, selects the highest stable `moonli-vX.Y.Z` release
from the fixed backend repository, validates its manifest, downloads a size-bounded
bundle, verifies SHA-256 and rejects traversal, links, duplicates and unknown members.
It creates `/opt/moonli/.env` once with mode `0600`; edit only `OPERATOR INPUT`.

Install validates Docker/Compose, placeholders, Access Key strength and separation,
TLS files, the immutable GHCR digest and Compose configuration. It installs the
pinned updater, starts the stack and gates success on loopback readiness and verified
public HTTPS. After the first successful start the plaintext operator seed is blanked
in `.env`; the data volume retains only its scrypt verifier.

## Exposure and authentication

Nginx is the sole public listener. `api` also binds `127.0.0.1:18000` for the local
updater. The public gateway forwards `/health`, `/v1/generate`, UI assets and the
allow-listed authenticated operator routes. `/metrics`, `/docs`, `/data`,
`/proxy/image`, updater catalog/restore, test and legacy routes return `404` publicly.
Unknown hosts return `421`.

No VPN or mTLS is used. The boundary is normal domain TLS:

- client generation uses an independent client API key plus a persistent `td-########`
  or `aa-########` device identifier;
- operator routes use an HttpOnly SameSite browser session plus CSRF;
- updater mutations require Unix-socket access and an independent per-service token;
- updater catalog/restore use separate machine tokens over loopback.

Generation may take several minutes. Nginx's upstream timeout is 300 seconds; Android
and TouchDesigner timeouts must be at least as large.

## Persistence, retention and recovery

`moonli_data` stores SQLite run/audit/usage/device state, non-secret settings and artifacts.
The application trims stale and completed runs, retained input and staging artifacts,
usage rows and audit records. Baselines are:

- audit: 10,000 events, 30 days and 64 MiB estimated payload;
- usage: 1,000,000 rows and 365 days;
- container stdout: three rotated files of 10 MiB;
- logical backup: 128 MiB compressed and 256 MiB uncompressed;
- restore points: newest five.

The Production page creates and downloads a logical ZIP. It includes runs, usage,
registered devices and their block state,
non-secret settings, the operator verifier, retained audit and referenced completed
artifacts. It excludes Google/client/updater keys, cookies, sessions, `.env`, staging
and release files. Restore validates every member, schema, size, ratio and digest
before mutation, creates a pre-restore snapshot and reapplies it on failure.

`moonli_secrets` stores `/app/secrets/google-api-key`. APIs return only configured
state/source—never the value or a mask. The logical backup intentionally excludes it;
protect it with a separate encrypted disaster-recovery procedure.

## Updates and rollback

Release builds consume `https://github.com/psewdon1m-exocortex/updater.git` at commit
`75eff9607dac4f7dbe9f841bd428f53ee01274a1`. One root-owned updater daemon runs per
host. Moonli receives only its Unix socket, never Docker's socket. The UI can select a
semantic version but cannot supply commands, images, URLs, repositories or services.

Before mutation Moonli creates a logical ZIP and sends its checksum. The updater
independently resolves the fixed backend repository, validates the release identity,
compose-bundle checksum and immutable image digest, pulls the candidate, changes
image/version atomically and checks loopback/public health. Failure restores the
previous image/version and calls the loopback-only backup restore. Updater jobs and
backups are bounded to 20 completed items and 30 days.

The pinned upstream updater verifies but does not apply the downloaded Compose bundle.
Normal in-app updates are therefore **image-only**. A release that changes Compose,
Nginx topology, mounted paths or host units must declare an explicit maintenance
upgrade and be applied through the reviewed repair/install path after an operator-held
snapshot. It must not be advertised as a safe one-click update.

## Manual verification

```bash
docker compose --env-file /opt/moonli/.env -f docker-compose.yml -f compose.production.yml config
docker compose --env-file /opt/moonli/.env -f docker-compose.yml -f compose.production.yml ps
curl --fail https://moonli.example.com/health
curl --fail --header 'Host: moonli.example.com' http://127.0.0.1:18000/readyz
```

On failed installation, the installer prints only service status and the newest 80
log lines. It does not modify the firewall. Certificate renewal remains a host-level
operation; recreate/reload `gateway` after renewal.
