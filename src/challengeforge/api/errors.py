from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from challengeforge.domain.exceptions import DomainError, Unauthenticated


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def error_body(request: Request, code: str, message: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=error_body(request, exc.code, exc.message),
        )

    @app.exception_handler(Unauthenticated)
    async def unauthenticated_handler(request: Request, exc: Unauthenticated) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=error_body(request, exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(request, "request_validation_failed", str(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 401:
            code = "unauthenticated"
        elif exc.status_code == 403:
            code = "permission_denied"
        elif exc.status_code == 404:
            code = "not_found"
        else:
            code = "http_error"
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, code, detail),
        )

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body(request, "internal_error", "An unexpected error occurred."),
        )
