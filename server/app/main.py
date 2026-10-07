from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from app.config import settings
from app.db import engine, init_db
from app.middleware.auth import RateLimiter
from app.routes import health, memories
from app.services.memory_service import MemoryNotFoundError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        await run_in_threadpool(init_db)
        yield
    finally:
        await run_in_threadpool(engine.dispose)


app = FastAPI(title="Homie API", lifespan=lifespan)
app.state.rate_limiter = RateLimiter()


@app.exception_handler(MemoryNotFoundError)
async def memory_not_found(request: Request, exc: MemoryNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Memory not found"})


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": "Invalid request fields."})


@app.middleware("http")
async def internal_error(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    try:
        return await call_next(request)
    except Exception:
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
