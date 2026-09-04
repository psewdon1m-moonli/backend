from __future__ import annotations

import httpx

from app.api.errors import MoonliError
from app.providers.proxy import ProxyUrlSource, resolve_proxy_url


class GoogleKeyValidator:
    """Validate a Google API key without sending user content or mutating provider state."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        proxy_url: ProxyUrlSource = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/models?pageSize=1"
        self._timeout = min(max(timeout_seconds, 2.0), 15.0)
        self._proxy_url = proxy_url

    async def validate(self, api_key: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                proxy=resolve_proxy_url(self._proxy_url),
                trust_env=False,
            ) as client:
                response = await client.get(
                    self._url,
                    headers={
                        "x-goog-api-key": api_key,
                        "accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise MoonliError(
                "GOOGLE_KEY_VALIDATION_UNAVAILABLE",
                "Google key validation is temporarily unavailable.",
                502,
            ) from exc
        if response.status_code != 200:
            raise MoonliError(
                "GOOGLE_KEY_INVALID",
                "Google rejected this API key or it cannot access the configured API.",
                422,
            )
