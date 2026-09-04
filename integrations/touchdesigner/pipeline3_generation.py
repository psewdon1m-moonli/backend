print("image generation script")
op("index").par.value0 += 1
op("sfx_button").par.reloadpulse.pulse()
print("water delivery")
op("/queue").appendRow(["q" + str(parent(1).digits) + "a105b2500c2000d#"])
op("sfx_button").par.reloadpulse.pulse()
op("/queue").appendRow(["q" + str(parent(1).digits) + "a105b2500c2000d#"])

import sys
import os

# Find the project-local site-packages directory dynamically.
lib_path = os.path.join(project.folder, "site-packages")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

import hashlib
import http.client
import io
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile


# --- SETTINGS ---

API_BASE_URL = "https://moonli.shmoza.net"

# This is the Moonli client access key, not the Google API key.
# Paste only the key value: without "Bearer", quotes from .env, or the variable name.
API_KEY = "PASTE_MOONLI_ACCESS_KEY_HERE".strip()

IMAGE_SAVE_BASE = os.path.join(project.folder, "generated", "image")
EXPECTED_IMAGE_NAMES = ("image_1.jpg", "image_2.jpg", "image_3.jpg")
REQUEST_TIMEOUT_SECONDS = 300
NETWORK_ATTEMPTS = 3
MAX_ZIP_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024

DEVICE_DIRECTORY = os.path.join(project.folder, ".moonli")
DEVICE_ID_PATH = os.path.join(DEVICE_DIRECTORY, "device_id.txt")
DEVICE_ID_PATTERN = re.compile(r"^td-[0-9]{8}$")

JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _require_access_key():
    if not API_KEY or API_KEY == "PASTE_MOONLI_ACCESS_KEY_HERE":
        raise RuntimeError(
            "Moonli access key is not configured. Replace API_KEY in this script."
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


def _read_limited(response, limit):
    result = bytearray()
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > limit:
            raise RuntimeError("The Moonli archive exceeds the allowed size.")
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


def _request_image_archive(prompt_text, device_id, operation_id):
    body = json.dumps(
        {"type": "text", "pipeline": "pipeline-3", "text": prompt_text},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    endpoint = API_BASE_URL.rstrip("/") + "/v1/generate"
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
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/zip",
                "User-Agent": "Moonli-TouchDesigner/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get_content_type()
                content = _read_limited(response, MAX_ZIP_RESPONSE_BYTES)
            if content_type not in {"application/zip", "application/x-zip-compressed"}:
                raise RuntimeError(f"Unexpected Content-Type: {content_type}")
            return content
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


def _jpeg_dimensions(content):
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        raise RuntimeError("The received file is not a JPEG.")
    position = 2
    while position < len(content):
        if content[position] != 0xFF:
            position += 1
            continue
        while position < len(content) and content[position] == 0xFF:
            position += 1
        if position >= len(content):
            break
        marker = content[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(content):
            break
        segment_length = int.from_bytes(content[position:position + 2], "big")
        if segment_length < 2 or position + segment_length > len(content):
            raise RuntimeError("The JPEG contains a damaged segment.")
        if marker in JPEG_SOF_MARKERS:
            height = int.from_bytes(content[position + 3:position + 5], "big")
            width = int.from_bytes(content[position + 5:position + 7], "big")
            return width, height
        if marker == 0xDA:
            break
        position += segment_length
    raise RuntimeError("Unable to determine the JPEG dimensions.")


def _validate_archive(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            entries = archive.infolist()
            if len(entries) != 3 or {item.filename for item in entries} != set(
                EXPECTED_IMAGE_NAMES
            ):
                raise RuntimeError(
                    "The response must contain only image_1.jpg, image_2.jpg, and image_3.jpg."
                )
            if any(item.is_dir() or item.flag_bits & 0x1 for item in entries):
                raise RuntimeError("The ZIP contains invalid entries.")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
                raise RuntimeError("The uncompressed response is too large.")
            damaged = archive.testzip()
            if damaged:
                raise RuntimeError(f"Damaged file: {damaged}")
            images = {name: archive.read(name) for name in EXPECTED_IMAGE_NAMES}
    except zipfile.BadZipFile as error:
        raise RuntimeError("Moonli returned a damaged ZIP.") from error
    hashes = set()
    for name, image in images.items():
        if _jpeg_dimensions(image) != (1024, 1024):
            raise RuntimeError(f"{name} is not 1024x1024.")
        hashes.add(hashlib.sha256(image).digest())
    if len(hashes) != 3:
        raise RuntimeError("Moonli returned duplicate images.")
    return images


def _save_images(images, operation_id):
    directory = os.path.dirname(IMAGE_SAVE_BASE)
    os.makedirs(directory, exist_ok=True)
    temporary = {}
    final = {}
    try:
        for name in EXPECTED_IMAGE_NAMES:
            target = os.path.join(directory, name)
            staging = os.path.join(directory, f".{name}.{operation_id}.tmp")
            with open(staging, "wb") as file:
                file.write(images[name])
                file.flush()
                os.fsync(file.fileno())
            temporary[name] = staging
            final[name] = target
        for name in EXPECTED_IMAGE_NAMES:
            os.replace(temporary[name], final[name])
        return final
    finally:
        for path in temporary.values():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def generate_image_thread(prompt_text, operation_id):
    print(f"\n[ImageGen] Worker started. Request: '{prompt_text}'...")
    try:
        device_id = _get_or_create_device_id()
        print(f"[ImageGen] Device ID: {device_id}")
        print(f"[ImageGen] Operation UUID: {operation_id}")
        archive = _request_image_archive(prompt_text, device_id, operation_id)
        images = _validate_archive(archive)
        saved = _save_images(images, operation_id)
        for index in range(1, 4):
            name = f"image_{index}.jpg"
            safe_path = saved[name].replace("\\", "/")
            movie_in_name = f"moviefilein{index}"
            run(
                "op(args[0]).par.file = args[1]; op(args[0]).par.reload.pulse()",
                movie_in_name,
                safe_path,
                delayFrames=1,
            )
            print(f"[ImageGen {index}] Image saved successfully: {saved[name]}")
    except Exception as error:
        print(f"[ImageGen Error]: {error}")


def generate_google_image():
    target_op = op("answer")
    if not target_op or not target_op.text.strip():
        print("[ImageGen] Error: DAT 'answer' is empty or missing.")
        return
    prompt_text = target_op.text.strip()
    operation_id = str(uuid.uuid4())
    print("[ImageGen] Sending one Moonli request for three images...")
    threading.Thread(
        target=generate_image_thread,
        args=(prompt_text, operation_id),
        daemon=True,
    ).start()


generate_google_image()
