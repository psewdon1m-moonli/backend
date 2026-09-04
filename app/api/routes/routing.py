from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.errors import MoonliError

router = APIRouter(prefix="/internal/routing", include_in_schema=False)


class RoutingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    vless_uri: SecretStr | None = Field(default=None, max_length=4096)


def _authorize(request: Request, *, mutate: bool = False) -> None:
    request.app.state.operator_auth_store.authenticate_request(
        request, require_csrf=mutate
    )


def save_routing_configuration(
    request: Request, *, enabled: bool, vless_uri: str | None
) -> dict[str, object]:
    store = request.app.state.routing_config_store
    if enabled and not store.proxy_available():
        raise MoonliError(
            "ROUTING_PROXY_UNAVAILABLE",
            "The Xray sidecar is not available. Apply the Compose maintenance update before enabling proxy routing.",
            503,
        )
    try:
        stored = store.update(enabled=enabled, vless_uri=vless_uri)
    except ValueError as exc:
        raise MoonliError("INVALID_ROUTING_CONFIGURATION", str(exc), 422) from exc
    request.app.state.audit_store.append(
        action="routing.update",
        outcome="success",
        summary="Outbound Google API routing settings were updated.",
        target_type="configuration",
        target_id="google-routing",
        request_id=getattr(request.state, "request_id", None),
        context={
            "enabled": stored["enabled"],
            "configured": stored["configured"],
        },
    )
    return stored


@router.get("")
def get_routing(request: Request, response: Response) -> dict[str, object]:
    _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.routing_config_store.status()


@router.put("")
def update_routing(
    payload: RoutingUpdate, request: Request, response: Response
) -> dict[str, object]:
    _authorize(request, mutate=True)
    stored = save_routing_configuration(
        request,
        enabled=payload.enabled,
        vless_uri=(
            payload.vless_uri.get_secret_value()
            if payload.vless_uri is not None
            else None
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return stored
