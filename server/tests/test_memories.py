from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import db, main
from app.config import settings
from app.middleware.auth import RateLimiter, get_user_sub
from app.models.db import MemoryRecord
from app.services.memory_service import MemoryService


def auth(user_sub: str = "alice") -> dict[str, str]:
    return {"Authorization": " ".join(("Bearer", user_sub))}


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=session.get_bind()))
    monkeypatch.setattr(main, "engine", session.get_bind())
    monkeypatch.setattr(main, "init_db", lambda: db.Base.metadata.create_all(session.get_bind()))
    monkeypatch.setattr(main.app.state, "rate_limiter", RateLimiter())
    monkeypatch.setattr(settings, "jwt_secret", "")
    with TestClient(main.app) as client:
        yield client


def test_api_crud(client: TestClient) -> None:
    response = client.post(
        "/memories",
        headers=auth(),
        json={
            "content": "Keys in drawer",
            "memory_type": "item_location",
            "tags": ["home", "home"],
        },
    )
    assert response.status_code == 201
    memory = response.json()
    assert memory["user_sub"] == "alice"
    assert memory["tags"] == ["home"]
    assert memory["timezone"] == "UTC"
    url = f"/memories/{memory['id']}"
    assert client.get(url, headers=auth()).json() == memory
    assert client.get("/memories", headers=auth(), params={"tags": "home"}).json() == [memory]
    updated = client.put(url, headers=auth(), json={"content": "Keys in bag"})
    assert updated.status_code == 200
    assert updated.json()["content"] == "Keys in bag"
    assert updated.json()["tags"] == ["home"]
    deleted = client.delete(url, headers=auth())
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(url, headers=auth()).status_code == 404
    assert client.delete(url, headers=auth()).status_code == 404
    assert client.get("/memories", headers=auth()).json() == []


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_api_user_isolation(client: TestClient, method: str) -> None:
    memory = client.post(
        "/memories", headers=auth(), json={"content": "Private", "memory_type": "fact"}
    ).json()
    response = client.request(
        method,
        f"/memories/{memory['id']}",
        headers=auth("bob"),
        **({"json": {"content": "Overwrite"}} if method == "put" else {}),
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert client.get("/memories", headers=auth("bob")).json() == []
    assert client.get(f"/memories/{memory['id']}", headers=auth()).json() == memory


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/memories", None),
        ("post", "/memories", {"content": "x", "memory_type": "fact"}),
        ("get", "/memories/missing", None),
        ("put", "/memories/missing", {"content": "x"}),
        ("delete", "/memories/missing", None),
    ],
)
def test_auth_required(client: TestClient, method: str, path: str, data: dict | None) -> None:
    assert client.request(method, path, json=data).status_code == 401


@pytest.mark.parametrize("token", ["", "a b", "x" * 256])
def test_invalid_tokens(client: TestClient, token: str) -> None:
    assert client.get("/memories", headers=auth(token)).status_code == 401


def test_mock_auth_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "nonempty-test-value")
    assert client.get("/memories", headers=auth()).status_code == 503
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("field", ["user_sub", "id", "timezone", "created_at", "updated_at"])
def test_protected_fields(client: TestClient, field: str) -> None:
    response = client.post(
        "/memories",
        headers=auth(),
        json={"content": "Private", "memory_type": "fact", field: "forged"},
    )
    assert response.status_code == 422
    response = client.put("/memories/missing", headers=auth(), json={field: "forged"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"content": " "},
        {"content": None},
        {"tags": None},
        {"tags": [""]},
        {"tags": ["x"] * 21},
        {"content": "x" * 10001},
    ],
)
def test_invalid_update(client: TestClient, data: dict[str, object]) -> None:
    assert client.put("/memories/missing", headers=auth(), json=data).status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        "skip=-1",
        f"skip={2**63}",
        "limit=0",
        "limit=101",
        "limit=no",
        "tags=",
        "tags=" + "x" * 51,
        "&".join(["tags=x"] * 21),
    ],
)
def test_invalid_list_parameters(client: TestClient, query: str) -> None:
    assert client.get(f"/memories?{query}", headers=auth()).status_code == 422


def test_api_tag_filter_pagination(client: TestClient) -> None:
    for tags in [["home"], ["home", "keys"], ["homework"]]:
        response = client.post(
            "/memories",
            headers=auth(),
            json={"content": "Memory", "memory_type": "fact", "tags": tags},
        )
        assert response.status_code == 201
    response = client.get("/memories?tags=home&tags=keys&limit=1", headers=auth())
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["tags"] == ["home", "keys"]
    assert client.get("/memories?tags=home&skip=1&limit=1", headers=auth()).json()[0]["tags"] == [
        "home"
    ]


def test_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.middleware.auth.monotonic", lambda: now[0])
    for _ in range(120):
        assert client.get("/memories", headers=auth()).status_code == 200
    response = client.get("/memories", headers=auth())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert client.get("/memories", headers=auth("bob")).status_code == 200
    assert client.get("/health").status_code == 200
    now[0] += 60
    assert client.get("/memories", headers=auth()).status_code == 200


def test_overlapping_read_does_not_rollback_create(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_pending, read_started, release_insert = Event(), Event(), Event()
    original_commit = Session.commit
    original_check = main.app.state.rate_limiter.check

    def pause_commit(session: Session) -> None:
        if any(isinstance(record, MemoryRecord) for record in session.new):
            session.flush()
            insert_pending.set()
            assert release_insert.wait(5)
        original_commit(session)

    def check_rate(user_sub: str) -> None:
        original_check(user_sub)
        if user_sub == "bob":
            read_started.set()

    monkeypatch.setattr(Session, "commit", pause_commit)
    monkeypatch.setattr(main.app.state.rate_limiter, "check", check_rate)
    with ThreadPoolExecutor(max_workers=2) as workers:
        create = workers.submit(
            client.post,
            "/memories",
            headers=auth(),
            json={"content": "Keep this memory", "memory_type": "fact"},
        )
        try:
            assert insert_pending.wait(5)
            read = workers.submit(client.get, "/memories", headers=auth("bob"))
            assert read_started.wait(5)
            with pytest.raises(TimeoutError):
                read.result(timeout=0.1)
        finally:
            release_insert.set()
        assert create.result(timeout=5).status_code == 201
        assert read.result(timeout=5).status_code == 200
    assert len(client.get("/memories", headers=auth()).json()) == 1


def test_unexpected_errors_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sensitive memory content and database credentials")

    monkeypatch.setattr(MemoryService, "list_memories", fail)
    response = client.get("/memories", headers={**auth(), "Origin": "http://localhost:3000"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong. Please try again."}
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "sensitive" not in caplog.text


def test_auth_attaches_request_state(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.middleware import auth as auth_module

    def check_state(request: Request) -> str:
        credentials = auth_module.HTTPAuthorizationCredentials(scheme="Bearer", credentials="alice")
        sub = get_user_sub(request, credentials)
        assert request.state.user_sub == sub
        return sub

    main.app.dependency_overrides[get_user_sub] = check_state
    try:
        assert client.get("/memories", headers=auth()).status_code == 200
    finally:
        main.app.dependency_overrides.clear()
