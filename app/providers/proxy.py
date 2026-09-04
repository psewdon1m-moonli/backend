from __future__ import annotations

from collections.abc import Callable

ProxyUrlSource = str | Callable[[], str | None] | None


def resolve_proxy_url(source: ProxyUrlSource) -> str | None:
    value = source() if callable(source) else source
    normalized = (value or "").strip()
    return normalized or None
