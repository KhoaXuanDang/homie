import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import settings
from app.middleware.auth import RateLimiter
from app.routes import health, memories
from app.services.memory_service import (
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
    yield


app = FastAPI(title="Homie API", lifespan=lifespan)
app.state.rate_limiter = RateLimiter()


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
        return await call_next(request)
    except Exception as exc:
        # Omit exception messages, local variables, and source lines which may contain secrets.
        frames = "".join(
            f'  File "{frame.f_code.co_filename}", line {line}, in {frame.f_code.co_name}\n'
            for frame, line in traceback.walk_tb(exc.__traceback__)
        )
        logger.error("Unhandled %s\n%s", type(exc).__name__, frames)
        return JSONResponse(
            status_code=500, content={"detail": "Something went wrong. Please try again."}
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(memories.router)
