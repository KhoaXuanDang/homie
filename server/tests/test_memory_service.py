from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.models.db import Base, MemoryRecord, UserRecord
from app.models.user import Profile, User
from app.services.memory_service import MemoryNotFoundError, MemoryService, UnsupportedFilterError


@pytest.fixture
def service(session: Session) -> MemoryService:
    return MemoryService(session)


def test_crud(service: MemoryService, session: Session) -> None:
    memory = service.create_memory("alice", "  Keys in drawer  ", "item_location", ["home", "home"])
    assert memory.content == "Keys in drawer"
    assert memory.tags == ["home"]
    assert memory.user_sub == "alice"
    assert memory.timezone == "UTC"
    assert memory.created_at.utcoffset().total_seconds() == 0
    assert service.get_memory("alice", memory.id) == memory
    assert service.list_memories("alice") == [memory]
    user = session.scalar(select(UserRecord).where(UserRecord.user_sub == "alice"))
    assert User.model_validate(user).timezone == "UTC"
    updated = service.update_memory("alice", memory.id, content="Keys in bag", tags=[])
    assert updated.content == "Keys in bag"
    assert updated.tags == []
    assert updated.created_at == memory.created_at
    assert updated.updated_at >= memory.updated_at
    assert service.delete_memory("alice", memory.id)
    assert service.list_memories("alice") == []
    assert not service.delete_memory("alice", memory.id)
    with pytest.raises(MemoryNotFoundError):
        service.get_memory("alice", memory.id)


def test_user_isolation(service: MemoryService) -> None:
    memory = service.create_memory("alice", "Private", "fact", [])
    service.create_memory("bob", "Other", "fact", [])
    assert len(service.list_memories("bob")) == 1
    with pytest.raises(MemoryNotFoundError):
        service.get_memory("bob", memory.id)
    with pytest.raises(MemoryNotFoundError):
        service.update_memory("bob", memory.id, content="Stolen")
    assert not service.delete_memory("bob", memory.id)
    assert service.get_memory("alice", memory.id).content == "Private"


def test_pagination_and_exact_tag_filtering(service: MemoryService) -> None:
    first = service.create_memory("alice", "First", "fact", ["home", "keys"])
    second = service.create_memory("alice", "Second", "fact", ["home"])
    service.create_memory("alice", "Third", "fact", ["homework", 'a"b', "%"])
    service.create_memory("bob", "Other user", "fact", ["home", "keys"])
    assert service.list_memories("alice", tags=[" home ", "keys"]) == [first]
    assert service.list_memories("alice", tags=["home"], skip=1, limit=1) == [first]
    assert service.list_memories("alice", tags=["home"], limit=1) == [second]
    assert service.list_memories("alice", skip=100) == []
    assert service.list_memories("alice", tags=["missing"]) == []
    assert len(service.list_memories("alice", tags=['a"b', "%"])) == 1
    assert len(service.list_memories("alice", tags=[])) == 3


def test_duplicates_remain_separate_records(service: MemoryService) -> None:
    first = service.create_memory("alice", "Same", "fact", ["a", " a "])
    second = service.create_memory("alice", "Same", "fact", ["a"])
    assert first.id != second.id
    assert first.tags == ["a"]
    assert len(service.list_memories("alice")) == 2


@pytest.mark.parametrize(
    "updates",
    [{}, {"content": ""}, {"content": None}, {"tags": None}, {"id": "new"}],
)
def test_invalid_updates(service: MemoryService, updates: dict[str, object]) -> None:
    memory = service.create_memory("alice", "Original", "fact", [])
    with pytest.raises(ValidationError):
        service.update_memory("alice", memory.id, **updates)
    assert service.get_memory("alice", memory.id).content == "Original"


@pytest.mark.parametrize(
    ("content", "memory_type", "tags"),
    [
        ("", "fact", []),
        ("   ", "fact", []),
        ("x" * 10001, "fact", []),
        ("Ok", "", []),
        ("Ok", "fact", [""]),
        ("Ok", "fact", ["x"] * 21),
    ],
)
def test_invalid_create(
    service: MemoryService, content: str, memory_type: str, tags: list[str]
) -> None:
    with pytest.raises(ValidationError):
        service.create_memory("alice", content, memory_type, tags)
    assert service.list_memories("alice") == []


@pytest.mark.parametrize(("skip", "limit"), [(-1, 10), (0, 0), (0, 101), (2**63, 10)])
def test_invalid_pagination(service: MemoryService, skip: int, limit: int) -> None:
    with pytest.raises(ValidationError):
        service.list_memories("alice", skip, limit)


def test_missing_ids_and_invalid_sub(service: MemoryService) -> None:
    for memory_id in ["missing", ""]:
        with pytest.raises(MemoryNotFoundError):
            service.get_memory("alice", memory_id)
        with pytest.raises(MemoryNotFoundError):
            service.update_memory("alice", memory_id, content="New")
        assert not service.delete_memory("alice", memory_id)
    with pytest.raises(ValidationError):
        service.create_memory("", "Content", "fact", [])
    with pytest.raises(ValidationError):
        service.list_memories(" ")


@pytest.mark.parametrize(
    ("instant", "offset"),
    [(datetime(2026, 1, 1, tzinfo=UTC), -18000), (datetime(2026, 7, 1, tzinfo=UTC), -14400)],
)
def test_profile_timezone_and_utc_storage(
    service: MemoryService, session: Session, instant: datetime, offset: int
) -> None:
    session.add(UserRecord(user_sub="alice", timezone="America/New_York"))
    session.commit()
    memory = service.create_memory("alice", "Timezone", "fact", [])
    record = session.get(MemoryRecord, memory.id)
    record.created_at = instant.astimezone(ZoneInfo("Asia/Tokyo"))
    session.commit()
    session.expire_all()
    stored = session.get(MemoryRecord, memory.id)
    assert stored.created_at == instant
    assert stored.created_at.utcoffset().total_seconds() == 0
    result = service.get_memory("alice", memory.id)
    assert result.timezone == "America/New_York"
    assert result.created_at.utcoffset().total_seconds() == offset
    assert result.created_at.astimezone(UTC) == instant


def test_invalid_timezone() -> None:
    with pytest.raises(ValidationError):
        Profile(timezone="Not/AZone")


def test_list_uses_one_profile_lookup(service: MemoryService, session: Session) -> None:
    session.add(UserRecord(user_sub="alice", timezone="Asia/Tokyo"))
    session.commit()
    for i in range(5):
        service.create_memory("alice", f"Memory {i}", "fact", [])
    statements: list[str] = []

    def capture(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        memories = service.list_memories("alice")
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    assert len(memories) == 5
    assert len(statements) == 2
    assert sum("FROM users" in statement for statement in statements) == 1
    assert all(memory.timezone == "Asia/Tokyo" for memory in memories)
    assert all(memory.created_at.utcoffset().total_seconds() == 32400 for memory in memories)


def test_missing_profile_uses_utc(service: MemoryService, session: Session) -> None:
    record = MemoryRecord(user_sub="orphan", content="Memory", memory_type="fact", tags=[])
    session.add(record)
    session.commit()
    assert service.get_memory("orphan", record.id).timezone == "UTC"
    assert service.list_memories("orphan")[0].timezone == "UTC"
    assert service.update_memory("orphan", record.id, content="Updated").timezone == "UTC"


def test_failed_memory_insert_rolls_back_profile(
    service: MemoryService,
    session: Session,
) -> None:
    def fail_flush(session: Session, context: object, instances: object) -> None:
        raise RuntimeError("Simulated memory insertion failure")

    event.listen(session, "before_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="Simulated"):
            service.create_memory("alice", "Memory", "fact", [])
    finally:
        session.rollback()
        event.remove(session, "before_flush", fail_flush)
    assert session.scalar(select(UserRecord).where(UserRecord.user_sub == "alice")) is None
    assert service.list_memories("alice") == []


def test_concurrent_first_memories_share_one_profile(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'memories.db'}")
    Base.metadata.create_all(engine)
    barrier = Barrier(2)

    def create(content: str) -> str:
        with Session(engine) as session:
            barrier.wait(timeout=5)
            return MemoryService(session).create_memory("alice", content, "fact", []).id

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            ids = list(workers.map(create, ["First", "Second"]))
        assert len(set(ids)) == 2
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
            assert len(MemoryService(session).list_memories("alice")) == 2
    finally:
        engine.dispose()


def test_postgres_profile_upsert() -> None:
    session = MagicMock(spec=Session)
    session.get_bind.return_value.dialect.name = "postgresql"
    session.execute.side_effect = RuntimeError("Stop after upsert")
    service = MemoryService(session)
    with pytest.raises(RuntimeError, match="Stop after upsert"):
        service.create_memory("alice", "Memory", "fact", [])
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO users" in sql
    assert "ON CONFLICT (user_sub) DO NOTHING" in sql
    assert statement.compile().params["user_sub"] == "alice"
    session.commit.assert_not_called()
    session.begin_nested.assert_not_called()


def test_unsupported_tag_filter(
    service: MemoryService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session.get_bind().dialect, "name", "postgresql")
    with pytest.raises(UnsupportedFilterError):
        service.list_memories("alice", tags=["home", "keys"])
    assert service.list_memories("alice", tags=[]) == []
