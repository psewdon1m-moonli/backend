from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/internal/devices", include_in_schema=False)


class DeviceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocked: bool


def _authorize(request: Request, *, mutate: bool = False) -> None:
    request.app.state.operator_auth_store.authenticate_request(
        request, require_csrf=mutate
    )


@router.get("")
def list_devices(
    request: Request,
    response: Response,
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    _authorize(request)
    devices, total = request.app.state.device_registry.list(limit=limit, offset=offset)
    response.headers["Cache-Control"] = "no-store"
    return {
        "devices": [device.as_dict() for device in devices],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/{device_id}")
def update_device(
    device_id: str,
    payload: DeviceStatusUpdate,
    request: Request,
    response: Response,
) -> dict[str, object]:
    _authorize(request, mutate=True)
    device = request.app.state.device_registry.set_blocked(device_id, payload.blocked)
    action = "block" if payload.blocked else "unblock"
    request.app.state.audit_store.append(
        action=f"device.{action}",
        outcome="success",
        summary=f"Device was {action}ed by an operator.",
        target_type="device",
        target_id=device.device_id,
        request_id=getattr(request.state, "request_id", None),
        context={"connection_type": device.connection_type},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"device": device.as_dict()}
