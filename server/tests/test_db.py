from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import create_db_engine, get_session
from app.models.db import Base, UserRecord


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite+pysqlite://",
        "sqlite+pysqlite:///:memory:",
        "sqlite:///file:homie-test?mode=memory&cache=shared&uri=true",
    ],
)
def test_in_memory_database_is_shared_across_threads(url: str) -> None:
    engine = create_db_engine(url)
    try:
        assert isinstance(engine.pool, StaticPool)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(UserRecord(user_sub="alice"))
            session.commit()

        def fetch_user() -> str:
            with Session(engine) as session:
                return session.scalar(select(UserRecord.user_sub))

        with ThreadPoolExecutor(max_workers=2) as workers:
            assert list(workers.map(lambda _: fetch_user(), range(2))) == ["alice", "alice"]
    finally:
        engine.dispose()


def test_session_rolls_back_and_closes(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.db.SessionLocal", lambda: session)
    monkeypatch.setattr(session, "rollback", lambda: calls.append("rollback"))
    monkeypatch.setattr(session, "close", lambda: calls.append("close"))
    dependency = get_session()
    assert next(dependency) is session
    with pytest.raises(RuntimeError, match="failure"):
        dependency.throw(RuntimeError("failure"))
    assert calls == ["rollback", "close"]
