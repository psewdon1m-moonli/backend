# Client changes to apply later

These changes were derived from the current projects but were not applied. The checked
projects are `main_table_script/Архив/MonliProj/` and `android app/`.

## TouchDesigner / Table 1

Treat `main_table_script/Архив/MonliProj/Player.toe` as the canonical runtime after a
manual operator confirms that it is the installation entry point. Stop editing the
parallel `atelier_*`, `test.*`, `CrashAutoSave.*`, and `Backup/*.toe` copies.

Add external, diffable Python beside the canonical project, for example:

```text
touchdesigner/
  moonli_client.py
  result_loader.py
  config.example.json
```

Inside `Player.toe`, add one `MoonliClient` COMP containing a Web Client DAT and a
small state/status DAT. Its only production sequence should be:

1. Read `MOONLI_API_BASE_URL=https://<domain>` and a client credential from an
   installation-local config or environment outside Git and outside the `.toe`.
2. On first start, generate `td-` plus eight random decimal digits using a secure
   random source, persist that non-secret identifier in the installation-local config,
   and reuse it for every later request. Send it as `X-Moonli-Device-Id`.
3. When text is confirmed, generate one UUID and make one JSON `POST /v1/generate`
   with `type=text`, `pipeline=pipeline-1`, `Authorization: Bearer ...`, and
   `Idempotency-Key`.
4. When recorded audio is confirmed, send the same endpoint once as multipart fields
   `type=audio`, `pipeline=pipeline-1`, and `audio=<file>`.
5. Use a 300-second initial timeout, keep TLS certificate verification enabled, and on
   network retry reuse the same idempotency key.
6. Accept only HTTP 200 plus `Content-Type: image/png`; stream/save the response to a
   temporary file, verify the `X-Moonli-Result-SHA256` digest and PNG decode, then
   atomically rename it and point the final Movie File In TOP at that file.
7. Surface the server error `code`, retry state, and `X-Moonli-Run-Id` in an operator
   status DAT/log. Never display a partially written file.

Remove the client-side calls/knowledge for transcription, candidate selection, palette,
vectorization, segmentation, variants, `/data`, and `/proxy/image`. Do not convert the
new PNG into `NNN_BG.mp4`/`NNN_FG.mp4`; if the old player still requires those, isolate
that conversion in a temporary `LegacyExporter` after the new result loader. Leave
`script_set.tsv` as legacy installation logic, not as a generation contract.

## Android / Table 2

The current application is asset-only: `DrawingLoader.kt` reads bundled lessons and
`DrawingMenuScreen.kt` opens `DrawingLessonScreen`; there is no HTTP generation layer.
Add a separate generated-drawing flow instead of teaching `LessonStepLoader` about the
server ZIP.

File-level changes:

- `app/src/main/AndroidManifest.xml`: add `android.permission.INTERNET`; add
  `android.permission.RECORD_AUDIO` only when the voice UI is enabled; keep cleartext
  traffic disabled so the base URL must be HTTPS.
- `app/build.gradle.kts`: add a pinned OkHttp dependency for streaming JSON/multipart
  requests and responses. Expose only the HTTPS base domain through `BuildConfig` or a
  managed kiosk config; never place the device credential in source/resources.
- Add `network/MoonliApiClient.kt`: implement exactly one `POST /v1/generate`; include
  `pipeline=pipeline-2` in both text JSON and audio multipart, use a 300-second
  call/read timeout, Bearer auth, UUID idempotency, and retry with the same key only
  after transport failure.
- Add `security/DeviceCredentialStore.kt`: provision an individual client token and
  wrap it with Android Keystore. Do not use one global token in every APK.
- Add `identity/DeviceIdentityStore.kt`: on first start, generate `aa-` plus eight
  random decimal digits with `SecureRandom`, persist it with DataStore, and send the
  same value as `X-Moonli-Device-Id` on every generation request. This identifier is
  not a credential and must remain stable across application restarts and updates.
- Add `model/MoonliLayerManifest.kt`: parse contract `1.0`, twelve ordered palette and
  layer slots, canvas, `used`, paths, and SHA-256 fields.
- Add `storage/LayerPackageStore.kt`: stream the body to an internal staging file;
  verify the response digest, ZIP CRC, safe relative entry paths (Zip Slip defense),
  entry count/uncompressed-size limits, manifest values, per-layer checksums, PNG
  decode/dimensions/color, and only then atomically move to completed storage.
- Add `ui/generation/GenerationScreen.kt` (or a ViewModel plus composables) for text,
  recording, progress, retry, and errors. It must not poll server stages.
- Add `ui/drawing/GeneratedLayerScreen.kt`: load `composite.png` and/or compose the
  twelve local RGBA layers in manifest index order. Cumulative frames can be assembled
  in memory; do not request them from the backend.
- Update `MainActivity.kt` routing and, if desired, `assets/ui/home/menu.json` so a new
  menu item opens `GenerationScreen`. Keep existing static lessons and
  `DrawingLessonScreen` unchanged.

On cancellation, cancel the OkHttp call and remove only the local staging file. On a
timeout where the server may have completed, retry with the original idempotency key so
the stored ZIP is returned without another paid generation.

The two tags are request routing values, not app identities or credential roles. Both
`td-*` and `aa-*` devices may call either pipeline. The backend permits the same valid
credential to call either tag; using separate per-device credentials is still
recommended because a client-generated identifier is not an authentication secret.
