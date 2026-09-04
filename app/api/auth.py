from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from app.api.errors import MoonliError


@dataclass(frozen=True)
class AuthenticatedClient:
    client_id: str


class ClientAuthenticator:
    def __init__(self, api_keys: tuple[str, ...]) -> None:
        self._api_keys = api_keys

    def authenticate(self, authorization: str | None, x_api_key: str | None) -> AuthenticatedClient:
        credential = x_api_key or ""
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer" and token:
                credential = token.strip()
        if not credential:
            raise MoonliError("UNAUTHORIZED", "Missing client credential.", 401)

        if any(secrets.compare_digest(credential, candidate) for candidate in self._api_keys):
            return AuthenticatedClient(
                client_id=hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
            )
        raise MoonliError("UNAUTHORIZED", "Invalid client credential.", 401)
