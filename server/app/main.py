import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import settings
from app.mcp_server import mcp, mcp_app
from app.middleware.auth import McpAuthGate, RateLimiter
from app.routes import health, me, media, memories
from app.services.media_service import (
    MediaNotFoundError,
    MediaStorageError,
    UploadNotReadyError,
)
from app.services.memory_service import (
    AccountDeletingError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyRecordDeletedError,
    MemoryNotFoundError,
    ensure_table,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.dynamodb_endpoint_url:
        ensure_table()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Homie API", lifespan=lifespan)
app.state.api_rate_limiter = RateLimiter(max_requests=120)
app.state.mcp_rate_limiter = RateLimiter(max_requests=60)
app.state.media_rate_limiter = RateLimiter(max_requests=30)


@app.exception_handler(MediaNotFoundError)
async def media_not_found(request: Request, exc: MediaNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Media not found"})


@app.exception_handler(UploadNotReadyError)
async def upload_not_ready(request: Request, exc: UploadNotReadyError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": "Upload is missing or incomplete. Please upload again."}
    )


@app.exception_handler(MediaStorageError)
async def media_storage_error(request: Request, exc: MediaStorageError) -> JSONResponse:
    return JSONResponse(
        status_code=503, content={"detail": "Media storage is unavailable. Please try again."}
    )


@app.exception_handler(MemoryNotFoundError)
async def memory_not_found(request: Request, exc: MemoryNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Memory not found"})


@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": "Idempotency key conflicts with an existing request."}
    )


@app.exception_handler(IdempotencyInProgressError)
async def idempotency_in_progress(
    request: Request, exc: IdempotencyInProgressError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "Request in progress, try again"})


@app.exception_handler(IdempotencyRecordDeletedError)
async def idempotency_record_deleted(
    request: Request, exc: IdempotencyRecordDeletedError
) -> JSONResponse:
    return JSONResponse(
        status_code=410, content={"detail": "The memory created by this request was deleted."}
    )


@app.exception_handler(AccountDeletingError)
async def account_deleting(request: Request, exc: AccountDeletingError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": "Your account is being deleted. Try again later."}
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422, content={"detail": "Invalid request fields.", "errors": errors}
    )


@app.middleware("http")
async def internal_error(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    try:
        response = await call_next(request)
    except Exception as exc:
        # Omit exception messages, local variables, and source lines which may contain secrets.
        frames = "".join(
            f'  File "{frame.f_code.co_filename}", line {line}, in {frame.f_code.co_name}\n'
            for frame, line in traceback.walk_tb(exc.__traceback__)
        )
        logger.error("Unhandled %s\n%s", type(exc).__name__, frames)
        response = JSONResponse(
            status_code=500, content={"detail": "Something went wrong. Please try again."}
        )
    return response


@app.middleware("http")
async def no_store_api(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    # Private data: browsers and the PWA service worker must never cache it (spec §9).
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(McpAuthGate)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(memories.router)
app.include_router(media.router)
app.include_router(me.router)
app.add_route("/mcp", mcp_app, methods=["GET", "POST", "DELETE"])


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema["components"]["schemas"]["HTTPValidationError"] = {
        "title": "HTTPValidationError",
        "type": "object",
        "required": ["detail", "errors"],
        "properties": {
            "detail": {"title": "Detail", "type": "string"},
            "errors": {
                "title": "Errors",
                "type": "array",
                "items": {"$ref": "#/components/schemas/ValidationError"},
            },
        },
    }
    schema["components"]["schemas"]["ValidationError"] = {
        "title": "ValidationError",
        "type": "object",
        "required": ["loc", "msg", "type"],
        "properties": {
            "loc": {
                "title": "Location",
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "msg": {"title": "Message", "type": "string"},
            "type": {"title": "Error Type", "type": "string"},
        },
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
