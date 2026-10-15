from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
from pydantic import ValidationError

from app.config import settings
from app.models.user import Profile
from app.services import memory_service
from app.services.memory_service import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyRecordDeletedError,
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
    assert "Item" not in ddb_table.get_item(Key={"pk": "USER#alice", "sk": "PROFILE"})
    stored = ddb_table.get_item(Key={"pk": "USER#alice", "sk": f"MEM#{memory.id}"})["Item"]
    assert stored["kind"] == "item_location"
    assert "memory_type" not in stored
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


def test_kind_query_and_limit_filtering(service: MemoryService) -> None:
    service.create_memory("alice", "First", "fact", ["home", "keys"])
    second = service.create_memory("alice", "Second", "fact", ["home"])
    service.create_memory("alice", "Third", "fact", ["homework", 'a"b', "%"])
    service.create_memory("bob", "Other user", "fact", ["home", "keys"])
    assert service.list_memories("alice", kind="fact", query="second") == [second]
    assert service.list_memories("alice", query="missing") == []
    assert len(service.list_memories("alice", limit=2)) == 2


@pytest.mark.parametrize("query", ["", " "])
def test_blank_search_query_returns_unfiltered_memories(service: MemoryService, query: str) -> None:
    older = service.create_memory("alice", "Older memory", "fact", [])
    newer = service.create_memory("alice", "Newer memory", "todo", [])

    assert service.list_memories("alice", query=query) == [newer, older]


def test_recall_ranks_matches_and_falls_back_to_newest(service: MemoryService) -> None:
    older = service.create_memory("alice", "Passport is inside the desk drawer", "fact", [])
    newer = service.create_memory("alice", "Keys are hanging by the door", "item_location", [])
    best_match = service.create_memory(
        "alice", "Passport and passport copies are in a folder", "fact", []
    )

    assert service.recall("alice", "where is my passport", kind="fact")[0] == best_match
    assert service.recall("alice", "where are my keys")[0] == newer
    assert service.recall("alice", "where is it?")[0] == best_match
    assert service.recall("alice", "passport", limit=50)[0] == best_match
    assert service.recall("alice", "no matching terms")[0] == best_match
    assert service.recall("alice", "passport", kind="item_location")[0] == newer
    assert older in service.recall("alice", "desk drawer")


def test_duplicates_remain_separate_records(service: MemoryService) -> None:
    first = service.create_memory("alice", "Same", "fact", ["a", " a "])
    second = service.create_memory("alice", "Same", "fact", ["a"])
    assert first.id != second.id
    assert first.tags == ["a"]
    assert len(service.list_memories("alice")) == 2


def test_event_link_and_media_ids_are_part_of_memory(service: MemoryService) -> None:
    memory = service.create_memory("alice", "Bring charger", "todo", [], event_id="event-1")
    assert memory.event_id == "event-1"
    assert memory.media_ids == []
    updated = service.update_memory("alice", memory.id, event_id=None)
    assert updated.event_id is None


def test_idempotent_create_replays_and_rejects_changed_payload(service: MemoryService) -> None:
    request_id = uuid4()
    created = service.create_memory(
        "alice", "Passport drawer", "item_location", ["Travel"], request_id=request_id
    )
    replay = service.create_memory(
        "alice", "  Passport drawer  ", "item_location", ["travel"], request_id=request_id
    )
    assert replay == created
    updated = service.update_memory("alice", created.id, content="Edited after create")
    latest_replay = service.create_memory(
        "alice", "Passport drawer", "item_location", ["travel"], request_id=request_id
    )
    assert latest_replay == updated
    assert len(service.list_memories("alice")) == 1
    with pytest.raises(IdempotencyConflictError):
        service.create_memory("alice", "Different content", "fact", [], request_id=request_id)


def test_idempotency_keys_are_scoped_to_user(service: MemoryService) -> None:
    request_id = uuid4()
    alice = service.create_memory("alice", "Alice", "fact", [], request_id=request_id)
    bob = service.create_memory("bob", "Bob", "fact", [], request_id=request_id)
    assert alice.id != bob.id
    assert service.get_memory("alice", alice.id).content == "Alice"
    assert service.get_memory("bob", bob.id).content == "Bob"


def test_retry_after_delete_is_gone_and_does_not_recreate(service: MemoryService) -> None:
    request_id = uuid4()
    memory = service.create_memory("alice", "Temporary", "fact", [], request_id=request_id)
    assert service.delete_memory("alice", memory.id)
    with pytest.raises(IdempotencyRecordDeletedError):
        service.create_memory("alice", "Temporary", "fact", [], request_id=request_id)
    assert service.list_memories("alice") == []


def test_failed_idempotent_transaction_leaves_no_records(
    service: MemoryService, ddb_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = uuid4()

    def fail_transaction(**kwargs: Any) -> None:
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWriteItems")

    monkeypatch.setattr(service.table.meta.client, "transact_write_items", fail_transaction)
    with pytest.raises(ClientError):
        service.create_memory("alice", "No partial write", "fact", [], request_id=request_id)
    assert service.list_memories("alice") == []
    assert (
        ddb_table.get_item(
            Key={"pk": "USER#alice", "sk": f"REQUEST#create-memory#{request_id}"}
        ).get("Item")
        is None
    )


def test_cancelled_idempotent_transaction_without_record_is_retryable(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancel_transaction(**kwargs: Any) -> None:
        raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")

    monkeypatch.setattr(service.table.meta.client, "transact_write_items", cancel_transaction)

    with pytest.raises(IdempotencyInProgressError, match="Request in progress, try again"):
        service.create_memory("alice", "In-flight create", "fact", [], request_id=uuid4())


def test_concurrent_idempotent_creates_write_one_memory(ddb_table: Any) -> None:
    request_id = uuid4()
    barrier = Barrier(2)

    def create() -> str:
        srv = MemoryService(table=ddb_table)
        barrier.wait(timeout=5)
        return srv.create_memory("alice", "One memory", "fact", [], request_id=request_id).id

    with ThreadPoolExecutor(max_workers=2) as workers:
        ids = list(workers.map(lambda _: create(), range(2)))
    assert ids[0] == ids[1]
    assert len(MemoryService(table=ddb_table).list_memories("alice")) == 1


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
    ("content", "kind", "tags"),
    [
        ("", "fact", []),
        ("   ", "fact", []),
        ("x" * 2001, "fact", []),
        ("Ok", "", []),
        ("Ok", "secret", []),
        ("Ok", "fact", [""]),
        ("Ok", "fact", ["x"] * 11),
    ],
)
def test_invalid_create(service: MemoryService, content: str, kind: str, tags: list[str]) -> None:
    with pytest.raises(ValidationError):
        service.create_memory("alice", content, kind, tags)
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

    memories = service.list_memories("alice", kind="fact", limit=10)
    assert len(memories) == 1
    assert adapter_inits == []


def test_module_level_adapters_defined() -> None:
    from app.services import memory_service

    assert hasattr(memory_service, "_user_sub_adapter")
    assert hasattr(memory_service, "_limit_adapter")
    assert hasattr(memory_service, "_kind_adapter")


@pytest.mark.parametrize("limit", [0, 201])
def test_invalid_list_limit(service: MemoryService, limit: int) -> None:
    with pytest.raises(ValidationError):
        service.list_memories("alice", limit=limit)


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
    memory_id = str(uuid4())
    iso_now = datetime.now(UTC).isoformat()
    ddb_table.put_item(
        Item={
            "pk": "USER#orphan",
            "sk": f"MEM#{memory_id}",
            "id": memory_id,
            "user_sub": "orphan",
            "content": "Memory",
            "kind": "fact",
            "tags": [],
            "created_at": iso_now,
            "updated_at": iso_now,
        }
    )
    assert service.get_memory("orphan", memory_id).timezone == "UTC"
    assert service.list_memories("orphan")[0].timezone == "UTC"
    assert service.update_memory("orphan", memory_id, content="Updated").timezone == "UTC"


def test_concurrent_creates(ddb_table: Any) -> None:
    barrier = Barrier(2)

    def create(content: str) -> str:
        srv = MemoryService(table=ddb_table)
        barrier.wait(timeout=5)
        return srv.create_memory("alice", content, "fact", []).id

    with ThreadPoolExecutor(max_workers=2) as workers:
        ids = list(workers.map(create, ["First", "Second"]))
    assert len(set(ids)) == 2
    assert len(MemoryService(table=ddb_table).list_memories("alice")) == 2


def test_ensure_table(isolated_env: None) -> None:
    with mock_aws():
        ensure_table()
        ddb = get_dynamodb_resource()
        table = ddb.Table(settings.dynamodb_table)
        assert table.table_status == "ACTIVE"
        # Calling ensure_table again handles existing table gracefully
        ensure_table()


def test_list_is_newest_first(service: MemoryService, ddb_table: Any) -> None:
    created = [service.create_memory("alice", f"M{i}", "fact", []) for i in range(30)]
    limits: list[int] = []
    original_query = service.table.query

    def spy_query(**kwargs: Any) -> Any:
        limits.append(kwargs.get("Limit"))
        return original_query(**kwargs)

    service.table.query = spy_query
    result = service.list_memories("alice", limit=10)
    assert [m.id for m in result] == [m.id for m in reversed(created)][:10]
    assert limits == [10]


def test_query_search_spans_dynamodb_pages(service: MemoryService, ddb_table: Any) -> None:
    matched = [service.create_memory("alice", f"Keys are kept T{i}", "fact", []) for i in range(3)]
    for i in range(20):
        service.create_memory("alice", f"Other memory {i}", "fact", [])
    original_query = service.table.query
    calls = 0

    def small_pages(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        kwargs["Limit"] = 5
        return original_query(**kwargs)

    service.table.query = small_pages
    result = service.list_memories("alice", query="keys")
    assert [m.id for m in result] == [m.id for m in reversed(matched)]
    assert calls > 1


def test_invalid_stored_timezone_falls_back_to_utc(service: MemoryService, ddb_table: Any) -> None:
    ddb_table.put_item(Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "Not/AZone"})
    assert service.create_memory("alice", "Memory", "fact", []).timezone == "UTC"


def test_profile_read_errors_are_not_swallowed(service: MemoryService) -> None:
    memory = service.create_memory("alice", "Memory", "fact", [])
    error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
    original_get_item = service.table.get_item

    def throttled_profile(**kwargs: Any) -> Any:
        if kwargs["Key"]["sk"] == "PROFILE":
            raise error
        return original_get_item(**kwargs)

    service.table.get_item = throttled_profile
    with pytest.raises(ClientError):
        service.get_memory("alice", memory.id)


def test_ensure_table_waits_for_dynamodb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class FakeResource:
        def create_table(self, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise EndpointConnectionError(endpoint_url="http://dynamodb:8000")

    monkeypatch.setattr(memory_service, "get_dynamodb_resource", FakeResource)
    ensure_table(delay=0)
    assert calls == 3
    calls = -100
    with pytest.raises(EndpointConnectionError):
        ensure_table(attempts=2, delay=0)


def test_table_is_cached_per_thread(ddb_table: Any) -> None:
    first = memory_service.get_table()
    assert memory_service.get_table() is first
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(memory_service.get_table).result() is not first


def test_idempotency_uses_real_dynamodb_transaction(ddb_local_table: Any) -> None:
    service = MemoryService(table=ddb_local_table)
    request_id = uuid4()

    created = service.create_memory(
        "integration-user", "Spare key", "item_location", ["home"], request_id=request_id
    )
    replay = service.create_memory(
        "integration-user", "Spare key", "item_location", ["home"], request_id=request_id
    )

    assert replay.id == created.id
    assert len(service.list_memories("integration-user")) == 1
    with pytest.raises(IdempotencyConflictError):
        service.create_memory(
            "integration-user", "Different payload", "fact", [], request_id=request_id
        )
