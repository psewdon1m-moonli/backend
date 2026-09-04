import threading
import os
import sys
import time

# Keep the project-local Python packages available, matching the existing DAT setup.
lib_path = os.path.join(project.folder, "site-packages")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

import http.client
import json
import mimetypes
import re
import secrets
import socket
import urllib.error
import urllib.request
import uuid


# --- SETTINGS ---

API_BASE_URL = "https://moonli.shmoza.net"

# This is the Moonli client access key, not the Google API key.
# Prefer setting MOONLI_ACCESS_KEY in the TouchDesigner process environment.
API_KEY = os.getenv("MOONLI_ACCESS_KEY", "PASTE_MOONLI_ACCESS_KEY_HERE").strip()

AUDIO_PATH = "Q:/projects/monli_table/MonliProj/voice.wav"
REQUEST_TIMEOUT_SECONDS = 300
NETWORK_ATTEMPTS = 3
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_TEXT_RESPONSE_BYTES = 8 * 1024

DEVICE_DIRECTORY = os.path.join(project.folder, ".moonli")
DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")
DEVICE_ID_PATTERN = re.compile(r"^td-[0-9]{8}$")


def _require_access_key():
    if not API_KEY or API_KEY == "PASTE_MOONLI_ACCESS_KEY_HERE":
        raise RuntimeError(
            "Moonli access key is not configured. Set MOONLI_ACCESS_KEY "
            "or replace API_KEY in this script."
        )
    return API_KEY


def _read_device_id():
    try:
        with open(DEVICE_ID_PATH, "r", encoding="ascii") as file:
            value = file.read().strip().lower()
    except FileNotFoundError:
        return None
    return value if DEVICE_ID_PATTERN.fullmatch(value) else None


def _get_or_create_device_id():
    os.makedirs(DEVICE_DIRECTORY, exist_ok=True)
    for _ in range(40):
        existing = _read_device_id()
        if existing:
            return existing
        if os.path.exists(DEVICE_ID_PATH):
            time.sleep(0.05)
            continue
        candidate = f"td-{secrets.randbelow(100_000_000):08d}"
        try:
            descriptor = os.open(
                DEVICE_ID_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            time.sleep(0.05)
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as file:
                file.write(candidate + "\n")
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            try:
                os.remove(DEVICE_ID_PATH)
            except OSError:
                pass
            raise
        return candidate
    existing = _read_device_id()
    if existing:
        return existing
    raise RuntimeError(f"Unable to create a valid Device ID: {DEVICE_ID_PATH}")


def _audio_content_type(audio_path):
    guessed = (mimetypes.guess_type(audio_path)[0] or "audio/wav").lower()
    aliases = {
        "audio/x-wav": "audio/wav",
        "audio/mp3": "audio/mpeg",
        "application/ogg": "audio/ogg",
    }
    return aliases.get(guessed, guessed)


def _multipart_audio(audio_path):
    boundary = "----Moonli" + uuid.uuid4().hex
    filename = os.path.basename(audio_path).replace('"', "_") or "voice.wav"
    with open(audio_path, "rb") as file:
        audio = file.read(MAX_AUDIO_BYTES + 1)
    if len(audio) > MAX_AUDIO_BYTES:
        raise RuntimeError("The audio file exceeds the Moonli 20 MiB limit.")
    content_type = _audio_content_type(audio_path)
    chunks = []

    def field(name, value):
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    field("type", "audio")
    field("pipeline", "pipeline-3")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="audio"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            audio,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _read_limited(response, limit):
    result = bytearray()
    while True:
        chunk = response.read(4096)
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > limit:
            raise RuntimeError("The Moonli response exceeds the allowed size.")
    return bytes(result)


def _http_error_details(error):
    try:
        text = error.read(64 * 1024).decode("utf-8", errors="replace").strip()
    except Exception:
        text = ""
    error_code = ""
    try:
        payload = json.loads(text)
        details = payload.get("error", {})
        error_code = str(details.get("code", "")).strip()
        message = str(details.get("message", "")).strip()
        text = f"{error_code}: {message}" if error_code and message else message or text
    except Exception:
        pass
    return error_code, text or f"HTTP {error.code}"


def _request_normalized_text(audio_path, device_id, operation_id):
    body, content_type = _multipart_audio(audio_path)
    endpoint = API_BASE_URL.rstrip("/") + "/v1/normalize"
    last_error = None
    for attempt in range(NETWORK_ATTEMPTS):
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {_require_access_key()}",
                "X-Moonli-Device-Id": device_id,
                "Idempotency-Key": operation_id,
                "Content-Type": content_type,
                "Accept": "text/plain",
                "User-Agent": "Moonli-TouchDesigner/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                response_type = response.headers.get_content_type()
                content = _read_limited(response, MAX_TEXT_RESPONSE_BYTES)
            if response_type != "text/plain":
                raise RuntimeError(f"Unexpected Content-Type: {response_type}")
            normalized = content.decode("utf-8").strip()
            if not normalized:
                raise RuntimeError("Moonli returned an empty normalized request.")
            return normalized
        except urllib.error.HTTPError as error:
            code, details = _http_error_details(error)
            retryable = error.code == 429 or (
                error.code == 409 and code == "GENERATION_IN_PROGRESS"
            )
            if retryable and attempt + 1 < NETWORK_ATTEMPTS:
                try:
                    delay = float(error.headers.get("Retry-After", attempt + 1))
                except (TypeError, ValueError):
                    delay = float(attempt + 1)
                time.sleep(max(1.0, min(delay, 15.0)))
                continue
            raise RuntimeError(details) from error
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            socket.timeout,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            last_error = error
            if attempt + 1 < NETWORK_ATTEMPTS:
                time.sleep(float(attempt + 1))
                continue
    raise RuntimeError(f"Moonli is unavailable after {NETWORK_ATTEMPTS} attempts: {last_error}")


def transcription_thread(audio_path, answer_node_path, operation_id):
    try:
        file_size = os.path.getsize(audio_path)
        print(f"\n[System] File ready. Size: {file_size} bytes")
        if file_size < 1000:
            print("[Error] The file is too small. Hold the button longer.")
            return
        device_id = _get_or_create_device_id()
        print(f"[System] Device ID: {device_id}")
        print(f"[System] Operation UUID: {operation_id}")
        normalized = _request_normalized_text(audio_path, device_id, operation_id)
        print(f">>> [NORMALIZED]: {normalized}")
        if answer_node_path:
            run(
                "op(args[0]).text = args[1]",
                answer_node_path,
                normalized,
                delayFrames=1,
            )
    except Exception as error:
        print(f"Transcription and normalization error: {error}")


# --- CHOP EXECUTE CALLBACKS ---

def onOffToOn(channel, sampleIndex, val, prev):
    return


def whileOn(channel, sampleIndex, val, prev):
    return


def onOnToOff(channel, sampleIndex, val, prev):
    time.sleep(0.2)
    if os.path.exists(AUDIO_PATH):
        target_op = op("../answer")
        answer_path = target_op.path if target_op else ""
        if not answer_path:
            print("[Warning] A Text DAT named 'answer' was not found beside this script.")
        operation_id = str(uuid.uuid4())
        threading.Thread(
            target=transcription_thread,
            args=(AUDIO_PATH, answer_path, operation_id),
            daemon=True,
        ).start()
    else:
        print(f"File not found: {AUDIO_PATH}")
    return


def whileOff(channel, sampleIndex, val, prev):
    return


def onValueChange(channel, sampleIndex, val, prev):
    return
