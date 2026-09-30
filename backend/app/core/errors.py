from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError


class AppError(Exception):
    """An expected failure with a stable machine code and a message that is safe to show to the user."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
    """Every error the API returns has the same shape: {"error": {"code": ..., "message": ...}}."""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"][1:])  # drop the leading "body" / "query"
        message = f"{field}: {first['msg']}" if field else str(first["msg"])
        return JSONResponse(status_code=422, content=error_body("INVALID_REQUEST", message))

    @app.exception_handler(OperationalError)
    async def handle_database_unavailable(_request: Request, _exc: OperationalError) -> JSONResponse:
        # Connection refused, timeout, server restarting. Transient, so 503 (retry) rather than 500 (bug). The
        # exception text can contain connection details, so it goes to the server log (uvicorn), never the client.
        return JSONResponse(
            status_code=503,
            content=error_body("DATABASE_UNAVAILABLE", "The service is temporarily unavailable. Please try again."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        # Last resort so even a bug returns the standard shape. The traceback is still logged by the server
        # (Starlette re-raises after sending this response); the client only ever sees this generic message.
        return JSONResponse(
            status_code=500,
            content=error_body("INTERNAL_ERROR", "Something went wrong on our side. Please try again."),
        )
