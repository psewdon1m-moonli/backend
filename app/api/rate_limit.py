from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.api.errors import MoonliError


@dataclass
class _ClientWindow:
    timestamps: deque[float] = field(default_factory=deque)
    active: int = 0


class ClientRateLimiter:
    """Per-process limiter; the production image intentionally runs one API worker."""

    def __init__(self, requests_per_minute: int, concurrent_requests: int) -> None:
        self._requests_per_minute = requests_per_minute
        self._concurrent_requests = concurrent_requests
        self._clients: dict[str, _ClientWindow] = defaultdict(_ClientWindow)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def limit(self, client_id: str) -> AsyncIterator[None]:
        now = time.monotonic()
        async with self._lock:
            window = self._clients[client_id]
            while window.timestamps and now - window.timestamps[0] >= 60:
                window.timestamps.popleft()
            if window.active >= self._concurrent_requests or len(window.timestamps) >= self._requests_per_minute:
                raise MoonliError(
                    "RATE_LIMITED",
                    "Client generation limit exceeded. Retry later.",
                    429,
                    retry_after=5,
                )
            window.timestamps.append(now)
            window.active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._clients[client_id].active -= 1
