from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.errors import MoonliError
from app.composition import apply_runtime_configuration

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
        store.candidate_settings(validated).validate()
        stored = store.set(validated)
        stored = apply_runtime_configuration(request.app)
    except (TypeError, ValueError) as exc:
        raise MoonliError("INVALID_SETTINGS", str(exc), 422) from exc
    effective = request.app.state.moonli_settings
    request.app.state.audit_store.append(
        action="settings.update",
        outcome="success",
        summary="Non-secret runtime settings were validated and applied.",
        target_type="configuration",
        target_id="server-settings",
        request_id=getattr(request.state, "request_id", None),
        context={
            "image_provider": effective.image_provider,
            "transcription_provider": effective.transcription_provider,
            "normalization_provider": effective.normalization_provider,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return stored
