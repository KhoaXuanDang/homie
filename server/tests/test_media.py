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
from app.models.media import MediaCreate
from app.routes.media import get_media_service
from app.services.media_service import MediaNotFoundError, MediaService
from app.services.memory_service import MemoryService
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
    with pytest.raises(ClientError):
        media_service.s3.head_object(
            Bucket=settings.media_bucket, Key=f"users/alice/media/{media_id}.jpg"
        )
    assert "Item" in media_service.table.get_item(Key=key)
    monkeypatch.setattr(
        "app.services.media_service.utc_now", lambda: now + timedelta(hours=1, seconds=1)
    )
    assert media_service.cleanup_pending("alice") == 1
    assert "Item" not in media_service.table.get_item(Key=key)
