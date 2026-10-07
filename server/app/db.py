from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.db import Base


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    in_memory = is_sqlite and (
        url.database in {None, "", ":memory:"} or url.query.get("mode") == "memory"
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


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
