# Moonli API v1

## Endpoint

```http
POST /v1/generate
Authorization: Bearer <device credential>
X-Moonli-Device-Id: td-02941846
Idempotency-Key: <8-128 safe characters; UUID recommended>
```

The client creates and persists its identifier before its first call: `td-` plus
eight random decimal digits for TouchDesigner, or `aa-` plus eight random decimal
digits for Android. The server registers a valid identifier on first use and counts
every subsequent production API attempt. A missing or malformed identifier returns
`INVALID_DEVICE_ID`; an operator-blocked identifier returns `DEVICE_BLOCKED` before
the generation pipeline starts. The identifier is not a secret and does not replace
the Bearer credential.

The credential only authenticates the caller. Every request must select exactly one
server-owned profile with the canonical `pipeline-1`, `pipeline-2`, or `pipeline-3` tag. A missing or
unknown tag (including `pipelien-2`) is rejected. `table_id`, palette, provider, and
processing-stage fields are also rejected.

### Text request

```http
Content-Type: application/json

{"type":"text","pipeline":"pipeline-1","text":"A red tree on a small island, without people"}
```

### Audio request

```http
Content-Type: multipart/form-data

type=audio
pipeline=pipeline-2
audio=<audio file>
```

Supported audio MIME types and the byte limit are configured with
`MOONLI_SUPPORTED_AUDIO_TYPES` and `MOONLI_MAX_AUDIO_SIZE`. The declared request size
is checked before multipart parsing and the upload is read in bounded chunks.

After direct text validation or audio transcription, the server runs prompt
normalization. This removes greetings, filler and test commentary, translates the
visual intent to a concise English phrase, and only then invokes `PromptBuilder`.
The run trace stores the original text/transcription, normalized text, visual brief,
and final technical image prompt separately.

The generated provider image is then converted to the selected profile's exact
palette by nearest-color matching in CIE Lab (CIE76). A configurable 3×3 mode cleanup
removes isolated palette speckles without changing the transparency mask. The result
must pass a final strict validator with zero color tolerance before output building.
`MOONLI_PALETTE_CLEANUP_PASSES` controls the cleanup count (`0..3`, default `1`).

## Responses

`pipeline-1`:

```http
HTTP/1.1 200 OK
Content-Type: image/png
Content-Disposition: inline; filename="moonli.png"
```

`pipeline-2`:

```http
HTTP/1.1 200 OK
Content-Type: application/vnd.moonli.layers+zip
Content-Disposition: attachment; filename="moonli-layers.zip"
```

## `pipeline-3`: TouchDesigner two-request contract

The same persistent `td-########` device identifier is sent on both requests. Each
new user operation creates a fresh UUID `Idempotency-Key`; automatic network retries
reuse that operation's UUID.

First, audio is transcribed and normalized:

```http
POST /v1/generate
Authorization: Bearer <device credential>
X-Moonli-Device-Id: td-02941846
Idempotency-Key: <new UUID>
Content-Type: multipart/form-data

type=audio
pipeline=pipeline-3
audio=<voice.wav>
```

The successful body is only the normalized phrase:

```http
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

cute penguin icon
```

Second, TouchDesigner sends that phrase to the normal generation endpoint:

```http
POST /v1/generate
Authorization: Bearer <device credential>
X-Moonli-Device-Id: td-02941846
Idempotency-Key: <another new UUID>
Content-Type: application/json

{"type":"text","pipeline":"pipeline-3","text":"cute penguin icon"}
```

The server performs three parallel image-provider calls. It uses
`gemini-3-pro-image-preview`, `1:1`, `1K`, and the production TouchDesigner system
instruction by default. It does not run prompt building, palette quantization,
palette validation, vectorization, or segmentation. The response is a ZIP whose
complete file list is exactly:

```text
image_1.jpg
image_2.jpg
image_3.jpg
```

All three files are distinct RGB JPEG images at exactly 1024×1024. There is no
manifest or other file in the response. The replacement TouchDesigner script keeps
the ZIP in memory, atomically writes only the three JPEGs into `generated/`, and maps
them to `moviefilein1`, `moviefilein2`, and `moviefilein3` respectively.

Successful `pipeline-1` and `pipeline-2` responses include:

```text
X-Moonli-Run-Id
X-Moonli-Result-SHA256
X-Moonli-Device-Id
X-Idempotent-Replay: true|false
```

Pipeline-3 response bodies remain deliberately minimal: normalized text for the first
operation and the three-member ZIP for the second. Standard HTTP headers such as
`Content-Type`, `Content-Length`, `Content-Disposition`, `Cache-Control`, and
`X-Request-ID` may still be present.

The same key and same authenticated request return the stored artifact. Reusing the
key with a different pipeline tag or payload returns `IDEMPOTENCY_CONFLICT`. A concurrent
duplicate returns `GENERATION_IN_PROGRESS` with `Retry-After`; retry with the same key.

## `pipeline-2` package

```text
manifest.json
composite.png
layers/00.png
...
layers/11.png
```

Every layer is a full-canvas RGBA PNG. Transparent pixels are `(0,0,0,0)` and every
visible pixel is fully opaque and exactly the slot color. Unused slots still have a
transparent PNG and `used:false`, keeping the 12-slot contract stable.

Manifest shape:

```json
{
  "contract_version": "1.0",
  "run_id": "run_...",
  "output_mode": "layered_image",
  "palette_version": "pipeline_2_palette_v1",
  "canvas": {"width": 1024, "height": 1024},
  "composite": "composite.png",
  "composite_sha256": "...",
  "palette": [
    {"index": 0, "color": "#4A9AD4", "used": true}
  ],
  "layers": [
    {
      "index": 0,
      "color": "#4A9AD4",
      "used": true,
      "image": "layers/00.png",
      "sha256": "..."
    }
  ]
}
```

The server checks safe relative paths, checksums, dimensions, slot order, exact colors,
alpha, ZIP integrity, and pixel-perfect `compose(layers) == composite` before publish.

## Errors

No traceback or partial artifact is returned:

```json
{
  "error": {
    "code": "PALETTE_VALIDATION_FAILED",
    "message": "Unable to generate an image that matches the allowed palette."
  }
}
```

Codes include `INVALID_INPUT`, `INVALID_DEVICE_ID`, `DEVICE_BLOCKED`,
`AUDIO_TOO_LARGE`, `UNAUTHORIZED`,
`IDEMPOTENCY_CONFLICT`, `GENERATION_IN_PROGRESS`, `TRANSCRIPTION_FAILED`,
`PROMPT_NORMALIZATION_FAILED`, `PROMPT_BUILD_FAILED`, `IMAGE_GENERATION_FAILED`,
`PALETTE_QUANTIZATION_FAILED`, `PALETTE_VALIDATION_FAILED`,
`OUTPUT_VALIDATION_FAILED`, `RESULT_EXPIRED`, and `INTERNAL_ERROR`.
`RATE_LIMITED` is returned with HTTP 429 and `Retry-After`.

## Operator routing API

For compatibility with image-only updates behind a gateway from 0.0.2, the browser
reads routing state from `GET /internal/production/config` and submits a
CSRF-protected routing action to `PUT /internal/production/config`. The dedicated
`GET|PUT /internal/routing` route remains an alias for gateways that expose it. These
are not client-generation endpoints. The routing object contains only:

```json
{"enabled": false, "configured": true, "mode": "direct"}
```

The routing action accepts `action="routing"`, `enabled`, and an optional `vless_uri`.
Omitting or sending an empty URI retains the saved private connection. The server
never returns the URI. Enabling is rejected until the Xray sidecar is reachable. When enabled,
all Google adapters resolve the internal proxy route at request time, so production
pipelines and authenticated Test Calls follow the new route without rebuilding their
provider objects.
