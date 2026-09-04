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
server-owned profile with the canonical `pipeline-1` or `pipeline-2` tag. A missing or
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

Successful responses include:

```text
X-Moonli-Run-Id
X-Moonli-Result-SHA256
X-Moonli-Device-Id
X-Idempotent-Replay: true|false
```

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
