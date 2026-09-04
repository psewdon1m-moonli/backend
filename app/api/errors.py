from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@dataclass
class MoonliError(Exception):
    code: str
    message: str
    status_code: int = 400
    retry_after: int | None = None

    def __str__(self) -> str:
        return self.message


def error_payload(code: str, message: str, run_id: str | None = None) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if run_id:
        error["run_id"] = run_id
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(MoonliError)
    async def handle_moonli_error(request: Request, exc: MoonliError) -> JSONResponse:
        store = getattr(request.app.state, "audit_store", None)
        if store is not None:
            store.append(
                action="http.request.rejected",
                outcome="denied" if exc.status_code in {401, 403, 429} else "error",
                severity="warning" if exc.status_code < 500 else "error",
                summary=f"Request failed with {exc.code}.",
                actor_type="request",
                actor_id="unknown",
                target_type="route",
                target_id=request.url.path,
                request_id=getattr(request.state, "request_id", None),
                transport=request.url.scheme,
                context={"method": request.method, "status_code": exc.status_code},
                error={
                    "type": type(exc).__name__,
                    "code": exc.code,
                    "message": exc.message,
                    "cause": None,
                    "stack_trace": None,
                },
            )
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        message = str(errors[0].get("msg", "Invalid request")) if errors else "Invalid request"
        store = getattr(request.app.state, "audit_store", None)
        if store is not None:
            store.append(
                action="http.request.validation_failed",
                outcome="error",
                severity="warning",
                summary="Request schema validation failed.",
                actor_type="request",
                actor_id="unknown",
                target_type="route",
                target_id=request.url.path,
                request_id=getattr(request.state, "request_id", None),
                transport=request.url.scheme,
                context={"method": request.method, "status_code": 422},
                error={
                    "type": type(exc).__name__,
                    "code": "INVALID_INPUT",
                    "message": message,
                    "cause": None,
                    "stack_trace": None,
                },
            )
        return JSONResponse(status_code=422, content=error_payload("INVALID_INPUT", message))
