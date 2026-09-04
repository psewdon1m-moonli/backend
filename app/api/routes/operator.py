from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.errors import MoonliError

router = APIRouter(prefix="/internal/auth", include_in_schema=False)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_key: SecretStr = Field(min_length=16, max_length=512)


class RotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_access_key: SecretStr = Field(min_length=16, max_length=512)
    new_access_key: SecretStr = Field(min_length=16, max_length=512)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _audit(request: Request, **fields) -> None:
    request.app.state.audit_store.append(request_id=_request_id(request), **fields)


@router.post("/session")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    remote = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_rate_limiter
    limiter.check(remote)
    try:
        session = request.app.state.operator_auth_store.create_session(
            payload.access_key.get_secret_value(), request.headers.get("user-agent", "")
        )
    except MoonliError:
        limiter.fail(remote)
        _audit(
            request,
            action="operator.login",
            outcome="denied",
            severity="warning",
            summary="Operator login was denied.",
            actor_type="anonymous",
            actor_id="unauthenticated",
            target_type="session",
            target_id="operator",
        )
        raise
    limiter.success(remote)
    settings = request.app.state.moonli_settings
    response.set_cookie(
        key=settings.operator_cookie_name,
        value=session.token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    _audit(
        request,
        action="operator.login",
        outcome="success",
        summary="Operator browser session was created.",
        target_type="session",
        target_id="operator",
    )
    return {"csrf_token": session.csrf_token, "expires_at": session.expires_at}


@router.get("/session")
def session_status(request: Request, response: Response) -> dict[str, object]:
    request.app.state.operator_auth_store.authenticate_request(request)
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@router.delete("/session")
def logout(request: Request, response: Response) -> dict[str, object]:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=True)
    settings = request.app.state.moonli_settings
    request.app.state.operator_auth_store.revoke_session(
        request.cookies.get(settings.operator_cookie_name, "")
    )
    response.delete_cookie(settings.operator_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    _audit(
        request,
        action="operator.logout",
        outcome="success",
        summary="Operator browser session was revoked.",
        target_type="session",
        target_id="operator",
    )
    return {"authenticated": False}


@router.post("/rotate-access-key")
def rotate_access_key(payload: RotateRequest, request: Request, response: Response) -> dict[str, object]:
    request.app.state.operator_auth_store.authenticate_request(request, require_csrf=True)
    try:
        request.app.state.operator_auth_store.rotate_access_key(
            payload.current_access_key.get_secret_value(),
            payload.new_access_key.get_secret_value(),
        )
    except ValueError as exc:
        raise MoonliError("INVALID_INPUT", str(exc), 422) from exc
    settings = request.app.state.moonli_settings
    response.delete_cookie(settings.operator_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    _audit(
        request,
        action="operator.access_key.rotate",
        outcome="success",
        summary="Operator Access Key was rotated and all sessions were revoked.",
        target_type="credential",
        target_id="operator-access-key",
    )
    return {"rotated": True, "reauthentication_required": True}
