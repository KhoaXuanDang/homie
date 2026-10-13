from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws
from pydantic import ValidationError

from app.config import settings
from app.models.user import Profile
from app.services.memory_service import (
    MemoryNotFoundError,
    MemoryService,
    ensure_table,
    get_dynamodb_resource,
)


def test_crud(service: MemoryService, ddb_table: Any) -> None:
    memory = service.create_memory("alice", "  Keys in drawer  ", "item_location", ["home", "home"])
    assert memory.content == "Keys in drawer"
    assert memory.tags == ["home"]
    assert memory.user_sub == "alice"
    assert memory.timezone == "UTC"
    assert memory.created_at.utcoffset().total_seconds() == 0
    assert service.get_memory("alice", memory.id) == memory
    assert service.list_memories("alice") == [memory]
    user_item = ddb_table.get_item(Key={"pk": "USER#alice", "sk": "PROFILE"}).get("Item")
    assert user_item is not None
    assert Profile(timezone=user_item["timezone"]).timezone == "UTC"
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


def test_list_memories_does_not_rebuild_type_adapters(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pydantic

    original_init = pydantic.TypeAdapter.__init__
    adapter_inits: list[tuple[object, ...]] = []

    def tracking_init(self: object, *args: object, **kwargs: object) -> None:
        adapter_inits.append(args)
        original_init(self, *args, **kwargs)

    service.create_memory("alice", "Test memory", "fact", ["tag1"])
    monkeypatch.setattr(pydantic.TypeAdapter, "__init__", tracking_init)

    memories = service.list_memories("alice", skip=0, limit=10, tags=["tag1"])
    assert len(memories) == 1
    assert adapter_inits == []


def test_module_level_adapters_defined() -> None:
    from app.services import memory_service

    assert hasattr(memory_service, "_user_sub_adapter")
    assert hasattr(memory_service, "_skip_adapter")
    assert hasattr(memory_service, "_limit_adapter")
    assert hasattr(memory_service, "_tags_adapter")


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
    service: MemoryService, ddb_table: Any, instant: datetime, offset: int
) -> None:
    ddb_table.put_item(Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "America/New_York"})
    memory = service.create_memory("alice", "Timezone", "fact", [])
    tokyo_iso = instant.astimezone(ZoneInfo("Asia/Tokyo")).isoformat()
    ddb_table.update_item(
        Key={"pk": "USER#alice", "sk": f"MEM#{memory.id}"},
        UpdateExpression="SET created_at = :cat",
        ExpressionAttributeValues={":cat": tokyo_iso},
    )
    result = service.get_memory("alice", memory.id)
    assert result.timezone == "America/New_York"
    assert result.created_at.utcoffset().total_seconds() == offset
    assert result.created_at.astimezone(UTC) == instant


def test_invalid_timezone() -> None:
    with pytest.raises(ValidationError):
        Profile(timezone="Not/AZone")


def test_list_uses_one_profile_lookup(service: MemoryService, ddb_table: Any) -> None:
    ddb_table.put_item(Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "Asia/Tokyo"})
    for i in range(5):
        service.create_memory("alice", f"Memory {i}", "fact", [])

    original_get_item = service.table.get_item
    profile_lookups = 0

    def spy_get_item(*args: Any, **kwargs: Any) -> Any:
        nonlocal profile_lookups
        key = kwargs.get("Key", {})
        if key.get("sk") == "PROFILE":
            profile_lookups += 1
        return original_get_item(*args, **kwargs)

    service.table.get_item = spy_get_item
    memories = service.list_memories("alice")
    assert len(memories) == 5
    assert profile_lookups == 1
    assert all(memory.timezone == "Asia/Tokyo" for memory in memories)
    assert all(memory.created_at.utcoffset().total_seconds() == 32400 for memory in memories)


def test_missing_profile_uses_utc(service: MemoryService, ddb_table: Any) -> None:
    memory_id = "orphan-123"
    iso_now = datetime.now(UTC).isoformat()
    ddb_table.put_item(
        Item={
            "pk": "USER#orphan",
            "sk": f"MEM#{memory_id}",
            "id": memory_id,
            "user_sub": "orphan",
            "content": "Memory",
            "memory_type": "fact",
            "kind": "fact",
            "tags": [],
            "created_at": iso_now,
            "updated_at": iso_now,
        }
    )
    assert service.get_memory("orphan", memory_id).timezone == "UTC"
    assert service.list_memories("orphan")[0].timezone == "UTC"
    assert service.update_memory("orphan", memory_id, content="Updated").timezone == "UTC"


def test_concurrent_first_memories_share_one_profile(ddb_table: Any) -> None:
    barrier = Barrier(2)

    def create(content: str) -> str:
        srv = MemoryService(table=ddb_table)
        barrier.wait(timeout=5)
        return srv.create_memory("alice", content, "fact", []).id

    with ThreadPoolExecutor(max_workers=2) as workers:
        ids = list(workers.map(create, ["First", "Second"]))
    assert len(set(ids)) == 2
    response = ddb_table.query(
        KeyConditionExpression=Key("pk").eq("USER#alice") & Key("sk").eq("PROFILE")
    )
    assert len(response.get("Items", [])) == 1
    assert len(MemoryService(table=ddb_table).list_memories("alice")) == 2


def test_ensure_table(isolated_env: None) -> None:
    with mock_aws():
        ensure_table()
        ddb = get_dynamodb_resource()
        table = ddb.Table(settings.dynamodb_table)
        assert table.table_status == "ACTIVE"
        # Calling ensure_table again handles existing table gracefully
        ensure_table()
