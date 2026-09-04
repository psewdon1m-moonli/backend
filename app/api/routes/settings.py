from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.errors import MoonliError

router = APIRouter(prefix="/internal/settings", include_in_schema=False)


def _authorize(request: Request, *, mutate: bool = False) -> None:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=mutate)


@router.get("")
def get_settings(request: Request, response: Response) -> dict[str, object]:
    _authorize(request)
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.server_settings_store.get()


@router.put("")
def update_settings(
    payload: dict[str, object], request: Request, response: Response
) -> dict[str, object]:
    _authorize(request, mutate=True)
    try:
        store = request.app.state.server_settings_store
        validated = store.validate(payload)
        stored = store.set(validated)
    except (TypeError, ValueError) as exc:
        raise MoonliError("INVALID_SETTINGS", str(exc), 422) from exc
    request.app.state.audit_store.append(
        action="settings.update",
        outcome="success",
        summary="Non-secret runtime settings were validated and applied.",
        target_type="configuration",
        target_id="server-settings",
        request_id=getattr(request.state, "request_id", None),
        context={
            "image_provider": stored["image_provider"],
            "transcription_provider": stored["transcription_provider"],
            "normalization_provider": stored["normalization_provider"],
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return stored
