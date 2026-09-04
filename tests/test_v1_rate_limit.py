from __future__ import annotations

import asyncio

import pytest

from app.api.errors import MoonliError
from app.api.rate_limit import ClientRateLimiter


def test_rate_limiter_rejects_excess_requests() -> None:
    limiter = ClientRateLimiter(requests_per_minute=1, concurrent_requests=1)

    async def exercise() -> None:
        async with limiter.limit("device"):
            pass
        with pytest.raises(MoonliError) as captured:
            async with limiter.limit("device"):
                pass
        assert captured.value.code == "RATE_LIMITED"
        assert captured.value.status_code == 429

    asyncio.run(exercise())
