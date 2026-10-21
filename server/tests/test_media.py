from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4, uuid7

import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.middleware.auth import RateLimiter
from app.models.media import MediaCreate, MediaUpdate
from app.routes.media import get_media_service
from app.services.media_service import MediaNotFoundError, MediaService, MediaStorageError
from app.services.memory_service import (
    IdempotencyRecordDeletedError,
    MemoryNotFoundError,
    MemoryService,
)
from app.services.storage import existing_keys
from scripts import maintain_media
from tests.test_memories import auth
from tests.test_memories import client as client


@pytest.fixture
def media_service(ddb_table: Any, monkeypatch: pytest.MonkeyPatch) -> MediaService:
    monkeypatch.setattr(settings, "media_bucket", "homie-test-media")
    s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
    s3.create_bucket(Bucket=settings.media_bucket)
    return MediaService(table=ddb_table, s3=s3)


@pytest.fixture
def media_client(
    client: TestClient,
    media_service: MediaService,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(main.app.state, "media_rate_limiter", RateLimiter(max_requests=30))
    main.app.dependency_overrides[get_media_service] = lambda: media_service
    try:
        yield client
    finally:
        main.app.dependency_overrides.pop(get_media_service, None)


def create(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.post(
        "/api/media",
        headers=auth(),
        json={"content_type": "image/jpeg", "size": 4, "caption": " Keys ", **changes},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def upload(service: MediaService, media_id: str, **changes: Any) -> dict[str, Any]:
    item = service.table.get_item(Key={"pk": "USER#alice", "sk": f"MEDIA#{media_id}"})["Item"]
    service.s3.put_object(
        **{
            "Bucket": settings.media_bucket,
            "Key": item["s3_key"],
            "ContentType": item["content_type"],
            "Body": b"data",
            **changes,
        }
    )
    return item


def test_pending_upload_contract(media_client: TestClient, media_service: MediaService) -> None:
    result = create(media_client)
    media = result["media"]
    assert media["status"] == "pending"
    assert media["caption"] == "Keys"
    assert media["tags"] == []
    assert media["memory_id"] is None
    assert media["training_consent"] is False
    assert media["url"] == ""
    assert result["expires_in"] == 300
    assert result["upload_headers"] == {"Content-Type": "image/jpeg", "Content-Length": "4"}
    url = urlparse(result["upload_url"])
    assert url.path == f"/users/alice/media/{media['id']}.jpg"
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]
    item = media_service.table.get_item(Key={"pk": "USER#alice", "sk": f"MEDIA#{media['id']}"})[
        "Item"
    ]
    assert item["status"] == "pending"
    assert "upload_url" not in item and "url" not in item
    assert media_client.get("/api/media", headers=auth()).json() == []


def test_complete_list_patch_delete(media_client: TestClient, media_service: MediaService) -> None:
    media_id = create(media_client)["media"]["id"]
    path = f"/api/media/{media_id}"
    item = upload(media_service, media_id)
    completed = media_client.post(path + "/complete", headers=auth())
    assert completed.status_code == 200
    assert completed.json()["status"] == "uploaded"
    query = parse_qs(urlparse(completed.json()["url"]).query)
    assert query["X-Amz-Expires"] == ["900"]
    assert query["response-cache-control"] == ["no-store"]
    # A completed record remains idempotent even if storage is temporarily unavailable.
    original_head = media_service.s3.head_object
    media_service.s3.head_object = Mock(side_effect=AssertionError("Unexpected HEAD"))
    assert media_client.post(path + "/complete", headers=auth()).status_code == 200
    media_service.s3.head_object = original_head
    listed = media_client.get("/api/media?limit=1", headers=auth())
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert [media["id"] for media in listed.json()] == [media_id]
    memory = MemoryService(table=media_service.table).create_memory("alice", "Keys", "fact", [])
    response = media_client.patch(
        path,
        headers=auth(),
        json={
            "caption": " New caption ",
            "tags": ["Home", "home"],
            "memory_id": str(memory.id),
            "training_consent": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["caption"] == "New caption"
    assert response.json()["tags"] == ["home"]
    assert response.json()["memory_id"] == str(memory.id)
    assert response.json()["training_consent"] is True
    assert (
        media_client.patch(path, headers=auth(), json={"memory_id": None}).json()["memory_id"]
        is None
    )
    response = media_client.delete(path, headers=auth())
    assert response.status_code == 204 and response.content == b""
    assert media_client.delete(path, headers=auth()).status_code == 404
    assert media_client.post(path + "/complete", headers=auth()).status_code == 404
    assert media_client.get("/api/media", headers=auth()).json() == []
    with pytest.raises(ClientError):
        media_service.s3.head_object(Bucket=settings.media_bucket, Key=item["s3_key"])


def test_complete_missing_or_mismatched_object(
    media_client: TestClient, media_service: MediaService
) -> None:
    media_id = create(media_client)["media"]["id"]
    path = f"/api/media/{media_id}/complete"
    missing = media_client.post(path, headers=auth())
    assert missing.status_code == 409
    assert missing.headers["cache-control"] == "no-store"
    for changes in [{"Body": b"too big"}, {"ContentType": "text/html"}]:
        upload(media_service, media_id, **changes)
        assert media_client.post(path, headers=auth()).status_code == 409
        assert media_service._get("alice", media_id)["status"] == "pending"
    upload(media_service, media_id)
    assert media_client.post(path, headers=auth()).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"content_type": "text/html", "size": 1},
        {"content_type": "image/jpeg", "size": 0},
        {"content_type": "image/jpeg", "size": -1},
        {"content_type": "image/jpeg", "size": True},
        {"content_type": "image/jpeg", "size": 50 * 1024 * 1024 + 1},
        {"content_type": "video/mp4", "size": 500 * 1024 * 1024 + 1},
        {"content_type": "image/png", "size": 1, "user_sub": "bob"},
        {"content_type": "image/png", "size": 1, "training_consent": True},
    ],
)
def test_invalid_create(media_client: TestClient, body: dict[str, Any]) -> None:
    assert media_client.post("/api/media", headers=auth(), json=body).status_code == 422


@pytest.mark.parametrize(
    ("content_type", "extension", "size"),
    [
        ("image/jpeg", "jpg", 50 * 1024 * 1024),
        ("image/png", "png", 50 * 1024 * 1024),
        ("image/webp", "webp", 50 * 1024 * 1024),
        ("image/heic", "heic", 50 * 1024 * 1024),
        ("video/mp4", "mp4", 500 * 1024 * 1024),
        ("video/quicktime", "mov", 500 * 1024 * 1024),
    ],
)
def test_supported_uploads(
    media_client: TestClient, content_type: str, extension: str, size: int
) -> None:
    result = create(media_client, content_type=content_type, size=size)
    assert urlparse(result["upload_url"]).path.endswith(f".{extension}")


def test_validation_ownership_and_auth(
    media_client: TestClient, media_service: MediaService
) -> None:
    media_id = create(media_client)["media"]["id"]
    path = f"/api/media/{media_id}"
    for user_sub, target_id in [("bob", media_id), ("alice", str(uuid4()))]:
        # Other users' and unknown IDs are indistinguishable.
        headers = auth(user_sub)
        target = f"/api/media/{target_id}"
        assert media_client.post(target + "/complete", headers=headers).status_code == 404
        assert media_client.patch(target, headers=headers, json={"caption": "x"}).status_code == 404
        assert media_client.delete(target, headers=headers).status_code == 404
    assert media_client.get("/api/media", headers=auth("bob")).json() == []
    for body in [
        {},
        {"caption": None},
        {"tags": None},
        {"training_consent": None},
        {"training_consent": "yes"},
        {"status": "uploaded"},
        {"size": 10},
        {"tags": ["x"] * 11},
        {"caption": "x" * 2001},
    ]:
        assert media_client.patch(path, headers=auth(), json=body).status_code == 422
    foreign = MemoryService(table=media_service.table).create_memory("bob", "Private", "fact", [])
    for memory_id in [str(foreign.id), str(uuid4())]:
        response = media_client.patch(path, headers=auth(), json={"memory_id": memory_id})
        assert response.status_code == 404
    for limit in [0, 201, "bad"]:
        assert media_client.get(f"/api/media?limit={limit}", headers=auth()).status_code == 422
    assert media_client.post("/api/media/not-a-uuid/complete", headers=auth()).status_code == 422
    assert media_client.get("/api/media").status_code == 401
    response = media_client.post("/api/media", json={"content_type": "image/png", "size": 1})
    assert response.status_code == 401


def test_upload_rate_limit(media_client: TestClient) -> None:
    for _ in range(30):
        create(media_client)
    response = media_client.post(
        "/api/media", headers=auth(), json={"content_type": "image/png", "size": 1}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert (
        media_client.post(
            "/api/media", headers=auth("bob"), json={"content_type": "image/png", "size": 1}
        ).status_code
        == 201
    )


def test_cleanup_pending(media_client: TestClient, media_service: MediaService) -> None:
    now = datetime.now(UTC)
    stale = create(media_client)["media"]["id"]
    missing = create(media_client)["media"]["id"]
    fresh = create(media_client)["media"]["id"]
    completed = create(media_client)["media"]["id"]
    item = upload(media_service, stale)
    upload(media_service, completed)
    media_service.complete_media("alice", completed)
    for media_id in [stale, missing, completed]:
        media_service.table.update_item(
            Key={"pk": "USER#alice", "sk": f"MEDIA#{media_id}"},
            UpdateExpression="SET created_at = :old",
            ExpressionAttributeValues={":old": (now - timedelta(hours=2)).isoformat()},
        )
    # A different user's stale upload must not be cleaned up.
    foreign = media_service.create_media("bob", MediaCreate(content_type="image/png", size=1))
    media_service.table.update_item(
        Key={"pk": "USER#bob", "sk": f"MEDIA#{foreign.media.id}"},
        UpdateExpression="SET created_at = :old",
        ExpressionAttributeValues={":old": (now - timedelta(hours=2)).isoformat()},
    )
    result = media_client.get("/api/media?limit=1", headers=auth())
    assert result.status_code == 200
    assert [entry["id"] for entry in result.json()] == [completed]
    assert media_service.cleanup_pending("alice") == 2
    assert media_service._get("alice", fresh)["status"] == "pending"
    assert media_service._get("bob", str(foreign.media.id))["status"] == "pending"
    for media_id in [stale, missing]:
        assert "Item" not in media_service.table.get_item(
            Key={"pk": "USER#alice", "sk": f"MEDIA#{media_id}"}
        )
    with pytest.raises(ClientError):
        media_service.s3.head_object(Bucket=settings.media_bucket, Key=item["s3_key"])
    assert media_service.cleanup_pending("alice") == 0


def test_cleanup_does_not_delete_racing_completion(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    item = upload(media_service, media_id)
    media_service.table.update_item(
        Key={"pk": item["pk"], "sk": item["sk"]},
        UpdateExpression="SET created_at = :old",
        ExpressionAttributeValues={":old": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
    )
    claim = media_service._claim_delete

    def complete_before_claim(item: dict[str, Any], **kwargs: Any) -> bool:
        media_service.complete_media("alice", media_id)
        return claim(item, **kwargs)

    monkeypatch.setattr(media_service, "_claim_delete", complete_before_claim)
    assert media_service.cleanup_pending("alice") == 0
    assert media_service._get("alice", media_id)["status"] == "uploaded"
    assert (
        media_service.s3.head_object(Bucket=settings.media_bucket, Key=item["s3_key"])[
            "ContentLength"
        ]
        == 4
    )


def test_storage_failure_and_delete_retry(
    media_client: TestClient, media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_id = create(media_client)["media"]["id"]
    upload(media_service, media_id)
    path = f"/api/media/{media_id}"
    with monkeypatch.context() as patch:
        patch.setattr(
            media_service.s3,
            "head_object",
            Mock(side_effect=EndpointConnectionError(endpoint_url="https://s3.invalid")),
        )
        assert media_client.post(path + "/complete", headers=auth()).status_code == 503
    media_service.complete_media("alice", media_id)
    with monkeypatch.context() as patch:
        patch.setattr(
            media_service.s3,
            "delete_object",
            Mock(side_effect=EndpointConnectionError(endpoint_url="https://s3.invalid")),
        )
        assert media_client.delete(path, headers=auth()).status_code == 503
    assert media_client.post(path + "/complete", headers=auth()).status_code == 404
    assert media_service.cleanup_pending("alice") == 1
    assert media_client.get("/api/media", headers=auth()).json() == []


def test_cleanup_claim_blocks_racing_completion(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    item = upload(media_service, media_id)
    media_service.table.update_item(
        Key={"pk": item["pk"], "sk": item["sk"]},
        UpdateExpression="SET created_at = :old",
        ExpressionAttributeValues={":old": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
    )
    head = media_service.s3.head_object

    def claim_after_head(**kwargs: Any) -> Any:
        metadata = head(**kwargs)
        assert media_service._claim_delete(item)
        return metadata

    monkeypatch.setattr(media_service.s3, "head_object", claim_after_head)
    with pytest.raises(MediaNotFoundError):
        media_service.complete_media("alice", media_id)
    assert media_service.cleanup_pending("alice") == 1
    assert "Item" not in media_service.table.get_item(Key={"pk": item["pk"], "sk": item["sk"]})


def test_pagination_order_cleanup_boundary_and_timezone(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 19, 20, tzinfo=UTC)
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: now)
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    oldest = str(result.media.id)
    item = upload(media_service, oldest)
    media_service.complete_media("alice", oldest)
    ids = []
    # A full filtered-out page must not hide older uploaded or stale items.
    with media_service.table.batch_writer() as batch:
        for _ in range(201):
            media_id = str(uuid7())
            ids.append(media_id)
            batch.put_item(
                Item={
                    **item,
                    "id": media_id,
                    "sk": f"MEDIA#{media_id}",
                    "s3_key": f"users/alice/media/{media_id}.jpg",
                }
            )
    boundary = ids[0]
    stale = ids[1]
    newest = ids[-1]
    for media_id, created_at in [
        (boundary, now - timedelta(hours=1)),
        (stale, now - timedelta(hours=1, microseconds=1)),
    ]:
        media_service.table.update_item(
            Key={"pk": "USER#alice", "sk": f"MEDIA#{media_id}"},
            UpdateExpression="SET created_at = :date",
            ExpressionAttributeValues={":date": created_at.isoformat()},
        )
    assert [str(media.id) for media in media_service.list_media("alice", 1)] == [oldest]
    # Maintenance pages are bounded, even when all records fail a status filter.
    for _ in range(4):
        media_service.maintain("alice", 200)
    assert media_service._get("alice", boundary)["status"] == "pending"
    with pytest.raises(MediaNotFoundError):
        media_service._get("alice", stale)
    upload(media_service, newest)
    media_service.complete_media("alice", newest)
    media_service.table.put_item(
        Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "America/New_York"}
    )
    listed = media_service.list_media("alice", 2)
    assert [str(media.id) for media in listed] == [newest, oldest]
    assert listed[0].created_at.utcoffset() == timedelta(hours=-4)


def test_delete_reconciles_late_upload(
    media_client: TestClient, media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: now)
    result = create(media_client)
    media_id = result["media"]["id"]
    path = f"/api/media/{media_id}"
    assert media_client.delete(path, headers=auth()).status_code == 204
    key = {"pk": "USER#alice", "sk": f"MEDIA#{media_id}"}
    assert media_service.table.get_item(Key=key)["Item"]["deletion_complete"] is True
    # An already-authorized PUT may finish after DELETE has returned.
    response = requests.put(
        result["upload_url"], data=b"late", headers={"Content-Type": "image/jpeg"}, timeout=5
    )
    assert response.status_code == 200
    assert media_client.post(path + "/complete", headers=auth()).status_code == 404
    assert media_client.get("/api/media", headers=auth()).json() == []
    assert media_service.cleanup_pending("alice") == 0
    assert (
        media_service.s3.head_object(
            Bucket=settings.media_bucket, Key=f"users/alice/media/{media_id}.jpg"
        )["ContentLength"]
        == 4
    )
    assert "Item" in media_service.table.get_item(Key=key)
    monkeypatch.setattr(
        "app.services.media_service.utc_now", lambda: now + timedelta(hours=1, seconds=1)
    )
    assert media_service.cleanup_pending("alice") == 1
    assert "Item" not in media_service.table.get_item(Key=key)
    # Model a PUT that started while authorized but finished after tombstone expiry.
    media_service.s3.put_object(
        Bucket=settings.media_bucket,
        Key=f"users/alice/media/{media_id}.jpg",
        Body=b"late",
        ContentType="image/jpeg",
    )
    foreign_key = f"users/bob/media/{uuid4()}.jpg"
    media_service.s3.put_object(Bucket=settings.media_bucket, Key=foreign_key, Body=b"private")
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: now + timedelta(hours=2))
    assert media_client.get("/api/media", headers=auth()).json() == []
    media_service.maintain("alice")
    media_service.maintain("alice")
    with pytest.raises(ClientError):
        media_service.s3.head_object(
            Bucket=settings.media_bucket, Key=f"users/alice/media/{media_id}.jpg"
        )
    assert (
        media_service.s3.head_object(Bucket=settings.media_bucket, Key=foreign_key)["ContentLength"]
        == 7
    )


def test_orphan_sweep_paginates_and_preserves_uploaded_media(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    item = upload(media_service, media_id)
    media_service.complete_media("alice", media_id)
    for _ in range(201):
        media_service.s3.put_object(
            Bucket=settings.media_bucket,
            Key=f"users/alice/media/{uuid4()}.jpg",
            Body=b"orphan",
        )
    later = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: later)
    assert [str(media.id) for media in media_service.list_media("alice")] == [media_id]
    # One media page followed by three bounded object pages.
    assert media_service.maintain("alice", 100)["next_phase"] == "orphans"
    for _ in range(3):
        media_service.maintain("alice", 100)
    remaining = media_service.s3.list_objects_v2(
        Bucket=settings.media_bucket, Prefix="users/alice/media/"
    )
    assert [obj["Key"] for obj in remaining["Contents"]] == [item["s3_key"]]


def test_list_is_read_only_with_one_profile_and_one_link_batch(
    media_client: TestClient, media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = MemoryService(table=media_service.table).create_memory("alice", "Keys", "fact", [])
    ids = []
    for _ in range(3):
        media_id = create(media_client)["media"]["id"]
        upload(media_service, media_id)
        media_service.complete_media("alice", media_id)
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=memory.id))
        ids.append(media_id)
    stale = create(media_client)["media"]["id"]
    key = {"pk": "USER#alice", "sk": f"MEDIA#{stale}"}
    media_service.table.update_item(
        Key=key,
        UpdateExpression="SET created_at = :old",
        ExpressionAttributeValues={":old": "2000-01-01T00:00:00Z"},
    )
    for operation in ("head_object", "list_objects_v2", "delete_object", "get_object"):
        monkeypatch.setattr(
            media_service.s3, operation, Mock(side_effect=AssertionError("GET performed S3 I/O"))
        )
    original_get = media_service.table.get_item
    reads = []

    def profile_only(**kwargs: Any) -> Any:
        reads.append(kwargs)
        assert kwargs["Key"] == {"pk": "USER#alice", "sk": "PROFILE"}
        return original_get(**kwargs)

    batch = Mock(wraps=media_service.table.meta.client.batch_get_item)
    monkeypatch.setattr(media_service.table, "get_item", profile_only)
    monkeypatch.setattr(media_service.table.meta.client, "batch_get_item", batch)
    result = media_client.get("/api/media", headers=auth())
    assert result.status_code == 200
    assert [entry["id"] for entry in result.json()] == list(reversed(ids))
    assert len(reads) == 1
    assert batch.call_count == 1
    request = batch.call_args.kwargs["RequestItems"][media_service.table.name]
    assert request["ConsistentRead"] is True and len(request["Keys"]) == 1
    assert original_get(Key=key)["Item"]["status"] == "pending"


def test_missing_bucket_blocks_presigning_not_empty_reads(
    media_client: TestClient, media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_id = create(media_client)["media"]["id"]
    upload(media_service, media_id)
    media_service.complete_media("alice", media_id)
    monkeypatch.setattr(settings, "media_bucket", "")
    assert media_client.get("/api/media", headers=auth()).status_code == 503
    assert media_client.get("/api/media", headers=auth("bob")).json() == []
    assert (
        media_client.post(
            "/api/media", headers=auth(), json={"content_type": "image/jpeg", "size": 4}
        ).status_code
        == 503
    )


@pytest.mark.parametrize(
    ("instant", "offset"),
    [("2026-01-01T12:00:00Z", -5), ("2026-07-01T12:00:00+00:00", -4)],
)
def test_media_shared_timezone_dst_and_canonical_writes(
    media_service: MediaService,
    monkeypatch: pytest.MonkeyPatch,
    instant: str,
    offset: int,
) -> None:
    now = datetime.fromisoformat(instant)
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: now)
    monkeypatch.setattr("app.services.memory_service.utc_now", lambda: now)
    media_service.table.put_item(
        Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "America/New_York"}
    )
    memory_service = MemoryService(table=media_service.table)
    memory = memory_service.create_memory("alice", "Keys", "fact", [])
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    record = upload(media_service, media_id)
    expected = now.isoformat(timespec="microseconds")
    assert record["created_at"] == expected
    stored_memory = media_service.table.get_item(
        Key={"pk": "USER#alice", "sk": f"MEM#{memory.id}"}
    )["Item"]
    assert stored_memory["created_at"] == stored_memory["updated_at"] == expected
    assert "media_ids" not in stored_memory
    memory_service.update_memory("alice", str(memory.id), content="Changed")
    media_service.complete_media("alice", media_id)
    assert media_service.list_media("alice")[0].created_at.utcoffset() == timedelta(hours=offset)


def test_reverse_links_are_derived_for_all_memory_responses(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MemoryService(table=media_service.table)
    request_id = uuid4()
    first = service.create_memory("alice", "First", "fact", [], request_id=request_id)
    second = service.create_memory("alice", "Second", "fact", [])
    foreign = service.create_memory("bob", "Foreign", "fact", [])
    media_id = str(
        media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4)).media.id
    )
    media_service.update_media("alice", media_id, MediaUpdate(memory_id=first.id))
    # Deliberately corrupt the legacy reverse list; it must not become authoritative.
    media_service.table.update_item(
        Key={"pk": "USER#alice", "sk": f"MEM#{second.id}"},
        UpdateExpression="SET media_ids = :stale",
        ExpressionAttributeValues={":stale": [media_id, "obsolete"]},
    )
    assert service.get_memory("alice", str(first.id)).media_ids == [media_id]
    assert service.get_memory("alice", str(second.id)).media_ids == []
    assert service.update_memory("alice", str(first.id), content="Edited").media_ids == [media_id]
    assert service.create_memory("alice", "First", "fact", [], request_id=request_id).media_ids == [
        media_id
    ]
    query = Mock(wraps=media_service.table.query)
    monkeypatch.setattr(media_service.table, "query", query)
    listed = service.list_memories("alice")
    assert {str(memory.id): memory.media_ids for memory in listed} == {
        str(first.id): [media_id],
        str(second.id): [],
    }
    assert query.call_count == 2  # Not one reverse query per returned memory.
    with pytest.raises(MemoryNotFoundError):
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=foreign.id))
    media_service.update_media("alice", media_id, MediaUpdate(memory_id=second.id))
    assert service.delete_memory("alice", str(first.id))
    assert (
        media_service.update_media("alice", media_id, MediaUpdate(caption="Relinked")).memory_id
        == second.id
    )
    assert service.get_memory("alice", str(second.id)).media_ids == [media_id]
    with pytest.raises(IdempotencyRecordDeletedError):
        service.create_memory("alice", "First", "fact", [], request_id=request_id)
    assert service.delete_memory("alice", str(second.id))
    # Stored authority can retain the old ID, but no response exposes a dangling link.
    assert (
        media_service.update_media("alice", media_id, MediaUpdate(caption="No memory")).memory_id
        is None
    )
    upload(media_service, media_id)
    assert media_service.complete_media("alice", media_id).memory_id is None
    assert media_service.complete_media("alice", media_id).memory_id is None
    assert media_service.list_media("alice")[0].memory_id is None
    media_service.update_media("alice", media_id, MediaUpdate(memory_id=None))
    media_service.delete_media("alice", media_id)


def test_link_transaction_prevents_check_then_write_deletion(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MemoryService(table=media_service.table)
    memory = service.create_memory("alice", "Memory", "fact", [])
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    transact = media_service.table.meta.client.transact_write_items

    def delete_before_link(**kwargs: Any) -> Any:
        assert service.delete_memory("alice", str(memory.id))
        return transact(**kwargs)

    monkeypatch.setattr(media_service.table.meta.client, "transact_write_items", delete_before_link)
    with pytest.raises(MemoryNotFoundError):
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=memory.id))
    assert media_service._get("alice", media_id).get("memory_id") is None


def test_concurrent_delete_claim_has_only_one_winner(
    media_client: TestClient, media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_id = create(media_client)["media"]["id"]
    claim = media_service._claim_delete
    winner = []
    delete_object = Mock(wraps=media_service.s3.delete_object)
    monkeypatch.setattr(media_service.s3, "delete_object", delete_object)

    def claim_after_competing_delete(item: dict[str, Any], **kwargs: Any) -> bool:
        # Both requests have read pending before either claim; deterministically
        # let the competing request win before the first conditional update.
        monkeypatch.setattr(media_service, "_claim_delete", claim)
        winner.append(media_client.delete(f"/api/media/{media_id}", headers=auth()).status_code)
        return claim(item, **kwargs)

    monkeypatch.setattr(media_service, "_claim_delete", claim_after_competing_delete)
    assert media_client.delete(f"/api/media/{media_id}", headers=auth()).status_code == 404
    assert winner == [204]
    assert delete_object.call_count == 1
    assert media_client.delete(f"/api/media/{media_id}", headers=auth()).status_code == 404
    assert media_service.cleanup_pending("alice") == 0
    assert delete_object.call_count == 1


@pytest.mark.parametrize(
    "created",
    [
        "2026-10-21T12:00:00Z",
        "2026-10-21T14:00:00+02:00",
        "2026-10-21T07:00:00-05:00",
        "2026-10-21T12:00:00",
        "2026-10-21T12:00:00.000001+00:00",
    ],
)
def test_cleanup_uses_datetime_cutoff_and_snapshot(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch, created: str
) -> None:
    now = datetime(2026, 10, 21, 13, 0, 0, 2, tzinfo=UTC)
    monkeypatch.setattr("app.services.media_service.utc_now", lambda: now)
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    key = {"pk": "USER#alice", "sk": f"MEDIA#{result.media.id}"}
    media_service.table.update_item(
        Key=key,
        UpdateExpression="SET created_at = :created",
        ExpressionAttributeValues={":created": created},
    )
    assert media_service.cleanup_pending("alice") == 1
    assert "Item" not in media_service.table.get_item(Key=key)
    # A snapshot observed before a timestamp correction cannot claim the new record.
    media_service.table.put_item(
        Item={
            **key,
            "status": "pending",
            "created_at": now.isoformat(),
        }
    )
    assert not media_service._claim_delete(
        {**key, "created_at": created}, cutoff=now - timedelta(hours=1)
    )


def test_maintenance_bounded_persisted_pages_and_safe_checkpoints(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(9):
        media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    query = Mock(wraps=media_service.table.query)
    monkeypatch.setattr(media_service.table, "query", query)
    first = media_service.maintain("alice", 4)
    assert first == {"phase": "media", "deleted": 0, "next_phase": "media"}
    assert query.call_count == 1
    assert query.call_args.kwargs["Limit"] == 4
    assert "FilterExpression" not in query.call_args.kwargs
    state_key = {"pk": "USER#alice", "sk": "MAINTENANCE#media"}
    first_state = media_service.table.get_item(Key=state_key)["Item"]
    restored = MediaService(table=media_service.table, s3=media_service.s3)
    assert restored.maintain("alice", 4)["next_phase"] == "media"
    assert query.call_args.kwargs["ExclusiveStartKey"] == first_state["cursor"]
    assert restored.maintain("alice", 4)["next_phase"] == "orphans"
    checkpoint = media_service.table.get_item(Key=state_key)["Item"]
    with monkeypatch.context() as patch:
        patch.setattr(
            media_service.s3,
            "list_objects_v2",
            Mock(side_effect=EndpointConnectionError(endpoint_url="secret")),
        )
        with pytest.raises(MediaStorageError):
            restored.maintain("alice", 4)
    assert media_service.table.get_item(Key=state_key)["Item"] == checkpoint
    assert restored.maintain("alice", 4)["next_phase"] == "media"
    checkpoint["phase"] = "media"
    checkpoint["cursor"] = {"pk": "USER#bob", "sk": "MEDIA#private"}
    media_service.table.put_item(Item=checkpoint)
    query.reset_mock()
    with pytest.raises(ValueError, match="checkpoint"):
        restored.maintain("alice", 4)
    query.assert_not_called()


def test_maintenance_failed_delete_retries_without_advancing_checkpoint(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_id = str(result.media.id)
    upload(media_service, media_id)
    media_service.table.update_item(
        Key={"pk": "USER#alice", "sk": f"MEDIA#{media_id}"},
        UpdateExpression="SET created_at = :old",
        ExpressionAttributeValues={":old": "2000-01-01T00:00:00Z"},
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            media_service.s3,
            "delete_object",
            Mock(side_effect=EndpointConnectionError(endpoint_url="secret")),
        )
        with pytest.raises(MediaStorageError):
            media_service.maintain("alice", 1)
    with pytest.raises(MediaNotFoundError):
        media_service.delete_media("alice", media_id)
    assert "Item" not in media_service.table.get_item(
        Key={"pk": "USER#alice", "sk": "MAINTENANCE#media"}
    )
    assert media_service.maintain("alice", 1)["deleted"] == 1


def test_maintenance_cannot_overwrite_a_concurrent_checkpoint(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):
        media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4))
    media_service.maintain("alice", 1)
    key = {"pk": "USER#alice", "sk": "MAINTENANCE#media"}
    checkpoint = media_service.table.get_item(Key=key)["Item"]
    query = media_service.table.query

    def advance_before_saving(**kwargs: Any) -> Any:
        page = query(**kwargs)
        media_service.table.put_item(
            Item={**checkpoint, "cursor": page["LastEvaluatedKey"], "version": "other-worker"}
        )
        return page

    monkeypatch.setattr(media_service.table, "query", advance_before_saving)
    with pytest.raises(MediaStorageError):
        media_service.maintain("alice", 1)
    assert media_service.table.get_item(Key=key)["Item"]["version"] == "other-worker"


def test_maintenance_checkpoints_empty_nonterminal_pages(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = {"pk": "USER#alice", "sk": f"MEDIA#{uuid4()}"}
    query = Mock(
        side_effect=[
            {"Items": [], "ScannedCount": 1, "LastEvaluatedKey": cursor},
            {"Items": [], "ScannedCount": 1},
        ]
    )
    monkeypatch.setattr(media_service.table, "query", query)
    assert media_service.maintain("alice", 1) == {
        "phase": "media",
        "deleted": 0,
        "next_phase": "media",
    }
    assert query.call_count == 1
    key = {"pk": "USER#alice", "sk": "MAINTENANCE#media"}
    assert media_service.table.get_item(Key=key)["Item"]["cursor"] == cursor
    restored = MediaService(table=media_service.table, s3=media_service.s3)
    assert restored.maintain("alice", 1)["next_phase"] == "orphans"
    assert query.call_count == 2
    assert query.call_args.kwargs["ExclusiveStartKey"] == cursor
    assert query.call_args.kwargs["Limit"] == 1
    assert "cursor" not in media_service.table.get_item(Key=key)["Item"]


@pytest.mark.parametrize("transport", [False, True])
@pytest.mark.parametrize(
    ("action", "operation"),
    [
        ("create", "put_item"),
        ("list", "query"),
        ("profile", "get_item"),
        ("complete_read", "get_item"),
        ("complete_write", "update_item"),
        ("update", "update_item"),
        ("link", "transact_write_items"),
        ("delete_read", "get_item"),
        ("delete_write", "update_item"),
        ("maintenance_read", "get_item"),
        ("maintenance_query", "query"),
        ("maintenance_checkpoint", "put_item"),
        ("linked_batch", "batch_get_item"),
    ],
)
def test_dynamodb_failures_are_sanitized_and_retryable(
    media_client: TestClient,
    media_service: MediaService,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    operation: str,
    transport: bool,
) -> None:
    media_id = create(media_client)["media"]["id"]
    upload(media_service, media_id)
    path = f"/api/media/{media_id}"
    memory = MemoryService(table=media_service.table).create_memory("alice", "Memory", "fact", [])
    if action in {"list", "profile", "linked_batch"}:
        media_service.complete_media("alice", media_id)
    if action == "linked_batch":
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=memory.id))
    error = (
        EndpointConnectionError(endpoint_url="secret-internal-storage")
        if transport
        else ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "secret"}},
            operation,
        )
    )
    target = (
        media_service.table.meta.client
        if operation in {"transact_write_items", "batch_get_item"}
        else media_service.table
    )
    monkeypatch.setattr(target, operation, Mock(side_effect=error))
    if action.startswith("maintenance"):
        with pytest.raises(MediaStorageError):
            media_service.maintain("alice")
        return
    if action == "create":
        response = media_client.post(
            "/api/media", headers=auth(), json={"content_type": "image/jpeg", "size": 4}
        )
    elif action in {"list", "profile", "linked_batch"}:
        response = media_client.get("/api/media", headers=auth())
    elif action.startswith("complete"):
        response = media_client.post(path + "/complete", headers=auth())
    elif action in {"update", "link"}:
        response = media_client.patch(
            path,
            headers=auth(),
            json={"memory_id": str(memory.id)} if action == "link" else {"caption": "Changed"},
        )
    else:
        response = media_client.delete(path, headers=auth())
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Media storage is unavailable. Please try again."}


@pytest.mark.parametrize("new_link", ["relink", "unlink"])
def test_memory_deletion_cannot_clear_a_concurrent_newer_link(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch, new_link: str
) -> None:
    memories = MemoryService(table=media_service.table)
    first = memories.create_memory("alice", "First", "fact", [])
    second = memories.create_memory("alice", "Second", "fact", [])
    media_id = str(
        media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4)).media.id
    )
    media_service.update_media("alice", media_id, MediaUpdate(memory_id=first.id))
    delete = media_service.table.delete_item
    replacement = second.id if new_link == "relink" else None

    def update_link_before_delete(**kwargs: Any) -> Any:
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=replacement))
        return delete(**kwargs)

    monkeypatch.setattr(media_service.table, "delete_item", update_link_before_delete)
    assert memories.delete_memory("alice", str(first.id))
    assert (
        media_service.update_media(
            "alice", media_id, MediaUpdate(caption="Still current")
        ).memory_id
        == replacement
    )
    assert memories.get_memory("alice", str(second.id)).media_ids == (
        [media_id] if replacement else []
    )
    # Deleting linked media also immediately removes the derived reverse link.
    monkeypatch.setattr(media_service.table, "delete_item", delete)
    media_service.delete_media("alice", media_id)
    assert memories.get_memory("alice", str(second.id)).media_ids == []


def test_link_transaction_rejects_concurrently_deleted_media(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    memories = MemoryService(table=media_service.table)
    memory = memories.create_memory("alice", "Memory", "fact", [])
    media_id = str(
        media_service.create_media("alice", MediaCreate(content_type="image/jpeg", size=4)).media.id
    )
    transact = media_service.table.meta.client.transact_write_items

    def delete_before_link(**kwargs: Any) -> Any:
        media_service.delete_media("alice", media_id)
        return transact(**kwargs)

    monkeypatch.setattr(media_service.table.meta.client, "transact_write_items", delete_before_link)
    with pytest.raises(MediaNotFoundError):
        media_service.update_media("alice", media_id, MediaUpdate(memory_id=memory.id))
    assert memories.get_memory("alice", str(memory.id)).media_ids == []


def test_existence_batches_are_consistent_chunked_and_never_drop_unprocessed_keys(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [{"pk": "USER#alice", "sk": f"MEM#{uuid4()}"} for _ in range(101)]
    calls = []

    def batch_get(**kwargs: Any) -> Any:
        request = kwargs["RequestItems"][media_service.table.name]
        assert request["ConsistentRead"] is True
        assert len(request["Keys"]) <= 100
        calls.append(request["Keys"])
        if len(calls) == 1:
            return {"UnprocessedKeys": kwargs["RequestItems"]}
        return {"Responses": {media_service.table.name: request["Keys"]}}

    monkeypatch.setattr(media_service.table.meta.client, "batch_get_item", batch_get)
    assert existing_keys(media_service.table, keys + keys) == {key["sk"] for key in keys}
    assert [len(call) for call in calls] == [100, 100, 1]

    def always_unprocessed(**kwargs: Any) -> Any:
        return {"UnprocessedKeys": kwargs["RequestItems"]}

    batch = Mock(side_effect=always_unprocessed)
    monkeypatch.setattr(media_service.table.meta.client, "batch_get_item", batch)
    with pytest.raises(MediaStorageError):
        existing_keys(media_service.table, keys[:1])
    assert batch.call_count == 3


@pytest.mark.parametrize("code", ["AccessDeniedException", "TransactionCanceledException"])
def test_unexpected_idempotent_transaction_errors_are_not_conflicts(
    media_service: MediaService, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.setattr(
        media_service.table.meta.client,
        "transact_write_items",
        Mock(side_effect=ClientError({"Error": {"Code": code}}, "TransactWriteItems")),
    )
    with pytest.raises(MediaStorageError):
        MemoryService(table=media_service.table).create_memory(
            "alice", "Memory", "fact", [], request_id=uuid4()
        )


def test_maintenance_cli_is_bounded_and_sanitizes_storage_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = Mock()
    service.maintain.return_value = {"phase": "media", "deleted": 2, "next_phase": "orphans"}
    monkeypatch.setattr(maintain_media, "MediaService", Mock(return_value=service))
    monkeypatch.setattr("sys.argv", ["maintain_media", "--user-sub", "alice", "--limit", "7"])
    maintain_media.main()
    service.maintain.assert_called_once_with("alice", 7)
    assert '"deleted": 2' in capsys.readouterr().out
    service.maintain.side_effect = MediaStorageError("secret")
    with pytest.raises(SystemExit) as exc:
        maintain_media.main()
    assert exc.value.code == 1
    assert capsys.readouterr().err == "Media storage is unavailable. Retry this command.\n"
    monkeypatch.setattr("sys.argv", ["maintain_media", "--user-sub", "alice", "--limit", "201"])
    with pytest.raises(SystemExit) as exc:
        maintain_media.main()
    assert exc.value.code == 2
