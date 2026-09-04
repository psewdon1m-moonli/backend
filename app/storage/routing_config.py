from __future__ import annotations

import json
import os
import re
import socket
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

PROXY_URL = "http://vless-proxy:18080"
_ALLOWED_QUERY_FIELDS = {
    "encryption",
    "flow",
    "security",
    "sni",
    "fp",
    "pbk",
    "sid",
    "type",
    "headerType",
    "spx",
}
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_SHORT_ID = re.compile(r"^(?:[0-9A-Fa-f]{2}){0,8}$")


class RoutingConfigStore:
    """Persist the private VLESS route and materialize Xray's runtime config."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._settings_path = self._root / "routing.json"
        self._xray_path = self._root / "xray-routing.json"
        self._lock = threading.RLock()
        with self._lock:
            current = self._read_private()
            self._write_xray_config(self._xray_config(current))

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    @property
    def xray_path(self) -> Path:
        return self._xray_path

    def status(self) -> dict[str, object]:
        with self._lock:
            current = self._read_private()
        return {
            "enabled": bool(current["enabled"]),
            "configured": bool(current["vless_uri"]),
            "mode": "vless" if current["enabled"] else "direct",
        }

    def proxy_url(self) -> str | None:
        with self._lock:
            current = self._read_private()
        if current["enabled"] and current["vless_uri"]:
            return PROXY_URL
        return None

    @staticmethod
    def proxy_available(timeout_seconds: float = 1.0) -> bool:
        try:
            with socket.create_connection(
                ("vless-proxy", 18080), timeout=timeout_seconds
            ):
                return True
        except OSError:
            return False

    def update(self, *, enabled: bool, vless_uri: str | None = None) -> dict[str, object]:
        with self._lock:
            current = self._read_private()
            replacement = (vless_uri or "").strip()
            if replacement:
                self._parse_vless_uri(replacement)
                current["vless_uri"] = replacement
            if enabled and not current["vless_uri"]:
                raise ValueError("A valid VLESS connection is required before enabling proxy routing")
            current["enabled"] = bool(enabled)
            self._write_xray_config(self._xray_config(current))
            self._write_atomic(
                self._settings_path,
                json.dumps(current, ensure_ascii=True, separators=(",", ":")) + "\n",
            )
        return self.status()

    def _read_private(self) -> dict[str, object]:
        try:
            payload = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"enabled": False, "vless_uri": ""}
        if not isinstance(payload, dict):
            return {"enabled": False, "vless_uri": ""}
        uri = payload.get("vless_uri")
        enabled = payload.get("enabled") is True
        if not isinstance(uri, str):
            uri = ""
        try:
            if uri:
                self._parse_vless_uri(uri)
        except ValueError:
            return {"enabled": False, "vless_uri": ""}
        return {"enabled": enabled and bool(uri), "vless_uri": uri}

    @classmethod
    def _parse_vless_uri(cls, value: str) -> dict[str, object]:
        if not 1 <= len(value) <= 4096 or any(ord(character) < 32 for character in value):
            raise ValueError("The VLESS connection is empty, too long, or contains control characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("The VLESS connection contains an invalid port or address") from exc
        if parsed.scheme.lower() != "vless" or not parsed.hostname or port is None:
            raise ValueError("Use a complete vless:// connection with a server and port")
        if parsed.password is not None:
            raise ValueError("VLESS passwords are not supported")
        try:
            user_id = str(uuid.UUID(unquote(parsed.username or "")))
        except (ValueError, AttributeError) as exc:
            raise ValueError("The VLESS user identifier must be a UUID") from exc
        host = parsed.hostname
        if not _HOST.fullmatch(host) or not 1 <= port <= 65535:
            raise ValueError("The VLESS server address is invalid")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query: dict[str, str] = {}
        for key, item in pairs:
            if key not in _ALLOWED_QUERY_FIELDS:
                raise ValueError(f"Unsupported VLESS option: {key}")
            if key in query:
                raise ValueError(f"Duplicate VLESS option: {key}")
            query[key] = item
        expected = {
            "encryption": "none",
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "type": "tcp",
            "headerType": "none",
        }
        for key, expected_value in expected.items():
            if query.get(key) != expected_value:
                raise ValueError(f"VLESS option {key} must be {expected_value}")
        server_name = query.get("sni", "")
        fingerprint = query.get("fp", "")
        public_key = query.get("pbk", "")
        short_id = query.get("sid", "")
        if not _HOST.fullmatch(server_name):
            raise ValueError("The VLESS Reality SNI is invalid")
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("The VLESS Reality fingerprint is invalid")
        if not _PUBLIC_KEY.fullmatch(public_key):
            raise ValueError("The VLESS Reality public key is invalid")
        if not _SHORT_ID.fullmatch(short_id):
            raise ValueError("The VLESS Reality short ID must contain up to 16 hex characters")
        return {
            "id": user_id,
            "address": host,
            "port": port,
            "flow": query["flow"],
            "server_name": server_name,
            "fingerprint": fingerprint,
            "public_key": public_key,
            "short_id": short_id,
            "spider_x": query.get("spx", ""),
        }

    @classmethod
    def _xray_config(cls, current: dict[str, object]) -> dict[str, object]:
        inbound = {
            "tag": "moonli-http-in",
            "listen": "0.0.0.0",
            "port": 18080,
            "protocol": "http",
            "settings": {"auth": "noauth", "udp": False},
        }
        if not current["enabled"]:
            outbound: dict[str, object] = {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {},
            }
        else:
            connection = cls._parse_vless_uri(str(current["vless_uri"]))
            reality = {
                "serverName": connection["server_name"],
                "fingerprint": connection["fingerprint"],
                "publicKey": connection["public_key"],
                "shortId": connection["short_id"],
                "show": False,
            }
            if connection["spider_x"]:
                reality["spiderX"] = connection["spider_x"]
            outbound = {
                "tag": "vless-out",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": connection["address"],
                            "port": connection["port"],
                            "users": [
                                {
                                    "id": connection["id"],
                                    "encryption": "none",
                                    "flow": connection["flow"],
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": reality,
                    "tcpSettings": {"header": {"type": "none"}},
                },
            }
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [inbound],
            "outbounds": [outbound],
        }

    def _write_xray_config(self, payload: dict[str, object]) -> None:
        self._write_atomic(
            self._xray_path,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
        )

    def _write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._root.chmod(0o700)
        except OSError:
            pass
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(content, encoding="utf-8")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
