from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from urllib.parse import unquote

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models.db import Base


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    in_memory = is_sqlite and (
        unquote(url.database or "") in {"", ":memory:", "file::memory:"}
        or url.query.get("mode") == "memory"
    )
    return create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
        poolclass=StaticPool if in_memory else None,
    )


engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


async def get_session(request: Request) -> AsyncIterator[Session]:
    session = SessionLocal()
    async with AsyncExitStack() as stack:
        try:
            if isinstance(session.get_bind().pool, StaticPool):
                # One in-memory connection must not share overlapping transactions.
                await stack.enter_async_context(request.app.state.database_lock)
            yield session
        except Exception:
            await run_in_threadpool(session.rollback)
            raise
        finally:
            await run_in_threadpool(session.close)
