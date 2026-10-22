from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.models.user import ProfileUpdate
from app.routes.me import get_profile_service
from app.services.memory_service import DELETING_SK
from app.services.profile_service import ProfileService
from tests.test_memories import auth, client  # noqa: F401

DEFAULT = {"timezone": "UTC", "home": None, "travel_mode": "DRIVE"}


@pytest.fixture
def api(client: TestClient, ddb_table: Any) -> Iterator[TestClient]:  # noqa: F811
    main.app.dependency_overrides[get_profile_service] = lambda: ProfileService(table=ddb_table)
    try:
        yield client
    finally:
        main.app.dependency_overrides.pop(get_profile_service, None)


def test_missing_profile_returns_defaults(api: TestClient) -> None:
    response = api.get("/api/me/profile", headers=auth())
    assert response.status_code == 200
    assert response.json() == DEFAULT


def test_put_merges_and_round_trips(api: TestClient) -> None:
    first = api.put(
        "/api/me/profile",
        headers=auth(),
        json={"timezone": "America/New_York", "home_address": " 1 Main St "},
    )
    assert first.status_code == 200
    expected = {
        "timezone": "America/New_York",
        "home": {"address": "1 Main St", "lat": None, "lng": None},
        "travel_mode": "DRIVE",
    }
    assert first.json() == expected
    # Omitted fields keep their stored value.
    second = api.put("/api/me/profile", headers=auth(), json={"travel_mode": "WALK"})
    assert second.json() == {**expected, "travel_mode": "WALK"}
    assert api.get("/api/me/profile", headers=auth()).json() == second.json()
    # An empty home_address clears home and keeps the other fields.
    cleared = api.put("/api/me/profile", headers=auth(), json={"home_address": " "})
    assert cleared.json() == {**expected, "travel_mode": "WALK", "home": None}


def test_put_accepts_full_settings_payload(api: TestClient) -> None:
    # The Settings page sends every field on each save.
    body = {"timezone": "Asia/Tokyo", "travel_mode": "TRANSIT", "home_address": ""}
    response = api.put("/api/me/profile", headers=auth(), json=body)
    assert response.json() == {"timezone": "Asia/Tokyo", "home": None, "travel_mode": "TRANSIT"}


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Not/AZone"},
        {"timezone": None},
        {"travel_mode": "FLY"},
        {"home_address": None},
        {"home_address": "x" * 301},
        {"home": {"address": "1 Main St"}},
        {"user_sub": "bob"},
        {},
    ],
)
def test_put_rejects_invalid_fields(api: TestClient, body: dict) -> None:
    response = api.put("/api/me/profile", headers=auth(), json=body)
    assert response.status_code == 422
    assert api.get("/api/me/profile", headers=auth()).json() == DEFAULT


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/me/profile"), ("put", "/api/me/profile"), ("delete", "/api/me")],
)
def test_auth_required(api: TestClient, method: str, path: str) -> None:
    response = api.request(method, path, json={"travel_mode": "WALK"})
    assert response.status_code == 401


def test_profile_is_scoped_to_user(api: TestClient) -> None:
    api.put("/api/me/profile", headers=auth(), json={"timezone": "Asia/Tokyo"})
    assert api.get("/api/me/profile", headers=auth("bob")).json() == DEFAULT


def test_delete_me_removes_only_that_users_data(
    api: TestClient, ddb_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "media_bucket", "homie-test-media")
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.create_bucket(Bucket="homie-test-media")
    for key in ["users/alice/media/1.jpg", "users/alice/media/2.mp4", "users/bob/media/3.jpg"]:
        s3.put_object(Bucket="homie-test-media", Key=key, Body=b"x")
    for user in ["alice", "bob"]:
        api.put("/api/me/profile", headers=auth(user), json={"timezone": "Asia/Tokyo"})
        api.post("/api/memories", headers=auth(user), json={"content": "Keys", "kind": "fact"})
        for sk in ["MEDIA#pending", "MEDIA#uploaded", "MEDIA#deleting", "MAINTENANCE#media"]:
            ddb_table.put_item(Item={"pk": f"USER#{user}", "sk": sk})

    assert api.delete("/api/me", headers=auth()).status_code == 204

    assert api.get("/api/me/profile", headers=auth()).json() == DEFAULT
    assert api.get("/api/memories", headers=auth()).json() == []
    assert (
        ddb_table.query(
            KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "USER#alice"}
        )["Items"]
        == []
    )
    remaining = [o["Key"] for o in s3.list_objects_v2(Bucket="homie-test-media")["Contents"]]
    assert remaining == ["users/bob/media/3.jpg"]
    assert api.get("/api/me/profile", headers=auth("bob")).json()["timezone"] == "Asia/Tokyo"
    assert len(api.get("/api/memories", headers=auth("bob")).json()) == 1
    for sk in ["MEDIA#pending", "MEDIA#uploaded", "MEDIA#deleting", "MAINTENANCE#media"]:
        assert "Item" in ddb_table.get_item(Key={"pk": "USER#bob", "sk": sk})
    # Repeating the delete is a no-op.
    assert api.delete("/api/me", headers=auth()).status_code == 204


def test_delete_me_paginates_past_one_query_page(ddb_table: Any) -> None:
    with ddb_table.batch_writer() as batch:
        for i in range(1200):
            batch.put_item(Item={"pk": "USER#alice", "sk": f"MEM#{i:05}", "pad": "x" * 1000})
    ProfileService(table=ddb_table).delete_all_user_data("alice")
    assert (
        ddb_table.query(
            KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "USER#alice"}
        )["Items"]
        == []
    )


def test_delete_me_keeps_records_when_s3_reports_errors(
    ddb_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "media_bucket", "homie-test-media")
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.create_bucket(Bucket="homie-test-media")
    s3.put_object(Bucket="homie-test-media", Key="users/alice/media/1.jpg", Body=b"x")
    failing = {"Errors": [{"Key": "users/alice/media/1.jpg", "Code": "AccessDenied"}]}
    monkeypatch.setattr(s3, "delete_objects", lambda **_: failing)
    service = ProfileService(table=ddb_table, s3=s3)
    service.put_profile("alice", ProfileUpdate(timezone="Asia/Tokyo"))

    with pytest.raises(RuntimeError):
        service.delete_all_user_data("alice")
    assert service.get_profile("alice").timezone == "Asia/Tokyo"


def test_writes_are_blocked_while_account_is_deleting(api: TestClient, ddb_table: Any) -> None:
    ddb_table.put_item(Item={"pk": "USER#alice", "sk": DELETING_SK})
    memory = {"content": "Keys", "kind": "fact"}
    assert api.post("/api/memories", headers=auth(), json=memory).status_code == 409
    assert (
        api.put("/api/me/profile", headers=auth(), json={"travel_mode": "WALK"}).status_code == 409
    )
    assert api.post("/api/memories", headers=auth("bob"), json=memory).status_code == 201
    # A finished delete removes the marker, so the account can be used again.
    assert api.delete("/api/me", headers=auth()).status_code == 204
    assert api.post("/api/memories", headers=auth(), json=memory).status_code == 201
