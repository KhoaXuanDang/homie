from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from time import time
from typing import Any
from uuid import uuid4, uuid7

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.middleware import auth as auth_middleware
from app.middleware.auth import RateLimiter, current_user
from app.routes.memories import get_memory_service
from app.services.memory_service import IdempotencyInProgressError, MemoryService

TEST_CLIENT_ID = "test-client"
_test_signing_key: rsa.RSAPrivateKey | None = None
_test_issuer = ""


def token(user_sub: str = "alice", **overrides: object) -> str:
    now = int(time())
    claims: dict[str, object] = {
        "iss": _test_issuer,
        "sub": user_sub,
        "token_use": "access",
        "client_id": TEST_CLIENT_ID,
        "scope": "homie/mcp:tools homie/mcp:service",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    if claims.get("sub") is None:
        claims.pop("sub", None)
    assert _test_signing_key is not None
    return jwt.encode(claims, _test_signing_key, algorithm="RS256", headers={"kid": "test-key"})


def auth(user_sub: str = "alice", **overrides: object) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token(user_sub, **overrides)}",
        "Idempotency-Key": str(uuid4()),
    }


def mcp_request(
    client: TestClient,
    headers: dict[str, str],
    method: str,
    params: dict[str, object],
) -> httpx.Response:
    return client.post(
        "/mcp",
        headers={
            **headers,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def mcp_initialize(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    return mcp_request(
        client,
        headers,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "memory-api-tests", "version": "1.0"},
        },
    )


@pytest.fixture
def client(
    app_client: TestClient,
    ddb_table: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_jwks_endpoint: str,
    test_signing_key: rsa.RSAPrivateKey,
) -> Iterator[TestClient]:
    global _test_issuer, _test_signing_key
    _test_issuer = fake_jwks_endpoint
    _test_signing_key = test_signing_key
    monkeypatch.setattr(main.app.state, "api_rate_limiter", RateLimiter(max_requests=120))
    monkeypatch.setattr(main.app.state, "mcp_rate_limiter", RateLimiter(max_requests=60))
    main.app.dependency_overrides[get_memory_service] = lambda: MemoryService(table=ddb_table)
    try:
        yield app_client
    finally:
        main.app.dependency_overrides.pop(get_memory_service, None)


def test_api_crud(client: TestClient) -> None:
    response = client.post(
        "/api/memories",
        headers=auth(),
        json={
            "content": "Keys in drawer",
            "kind": "item_location",
            "tags": ["home", "home"],
        },
    )
    assert response.status_code == 201
    memory = response.json()
    assert memory["user_sub"] == "alice"
    assert memory["tags"] == ["home"]
    assert memory["timezone"] == "UTC"
    url = f"/api/memories/{memory['id']}"
    assert client.get(url, headers=auth()).json() == memory
    assert client.get("/api/memories", headers=auth(), params={"q": "drawer"}).json() == [memory]
    updated = client.patch(url, headers=auth(), json={"content": "Keys in bag"})
    assert updated.status_code == 200
    assert updated.json()["content"] == "Keys in bag"
    assert updated.json()["tags"] == ["home"]
    assert client.put(url, headers=auth(), json={"content": "Legacy update"}).status_code == 405
    deleted = client.delete(url, headers=auth())
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(url, headers=auth()).status_code == 404
    assert client.delete(url, headers=auth()).status_code == 404
    assert client.get("/api/memories", headers=auth()).json() == []


def test_idempotent_create_replays_after_lost_response(client: TestClient) -> None:
    headers = auth()
    body = {"content": "Spare key in the cabinet", "kind": "item_location"}
    first = client.post("/api/memories", headers=headers, json=body)
    retry = client.post("/api/memories", headers=headers, json=body)
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert len(client.get("/api/memories", headers=auth()).json()) == 1


def test_idempotency_conflict_and_deleted_memory_return_409_and_410(client: TestClient) -> None:
    headers = auth()
    first = client.post(
        "/api/memories", headers=headers, json={"content": "Original", "kind": "fact"}
    )
    conflict = client.post(
        "/api/memories", headers=headers, json={"content": "Changed", "kind": "fact"}
    )
    assert first.status_code == 201
    assert conflict.status_code == 409

    assert client.delete(f"/api/memories/{first.json()['id']}", headers=auth()).status_code == 204
    gone = client.post(
        "/api/memories", headers=headers, json={"content": "Original", "kind": "fact"}
    )
    assert gone.status_code == 410
    assert client.get("/api/memories", headers=auth()).json() == []


def test_idempotency_transaction_in_progress_returns_retryable_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def in_progress(*args: Any, **kwargs: Any) -> None:
        raise IdempotencyInProgressError("Request in progress, try again")

    monkeypatch.setattr(MemoryService, "create_memory", in_progress)
    response = client.post(
        "/api/memories",
        headers=auth(),
        json={"content": "A fact", "kind": "fact"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Request in progress, try again"}


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
def test_idempotency_key_required_and_validated(client: TestClient, key: str | None) -> None:
    headers = auth()
    headers.pop("Idempotency-Key")
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post(
        "/api/memories", headers=headers, json={"content": "A fact", "kind": "fact"}
    )
    assert response.status_code == 422


def test_validation_error_openapi_matches_response(client: TestClient) -> None:
    response = client.post("/api/memories", headers=auth(), json={})
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid request fields."
    assert response.json()["errors"]

    schema = client.get("/openapi.json").json()["components"]["schemas"]
    validation = schema["HTTPValidationError"]
    assert validation["required"] == ["detail", "errors"]
    assert validation["properties"]["detail"]["type"] == "string"
    assert validation["properties"]["errors"]["type"] == "array"
    error_schema = schema["ValidationError"]
    assert error_schema["required"] == ["loc", "msg", "type"]
    assert set(error_schema["properties"]) == {"loc", "msg", "type"}


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_api_user_isolation(client: TestClient, method: str) -> None:
    memory = client.post(
        "/api/memories", headers=auth(), json={"content": "Private", "kind": "fact"}
    ).json()
    response = client.request(
        method,
        f"/api/memories/{memory['id']}",
        headers=auth("bob"),
        **({"json": {"content": "Overwrite"}} if method == "patch" else {}),
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert client.get("/api/memories", headers=auth("bob")).json() == []
    assert client.get(f"/api/memories/{memory['id']}", headers=auth()).json() == memory


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/api/memories", None),
        ("post", "/api/memories", {"content": "x", "kind": "fact"}),
        ("get", f"/api/memories/{uuid7()}", None),
        ("patch", f"/api/memories/{uuid7()}", {"content": "x"}),
        ("delete", f"/api/memories/{uuid7()}", None),
    ],
)
def test_auth_required(client: TestClient, method: str, path: str, data: dict | None) -> None:
    assert (
        client.request(
            method, path, json=data, headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("token", ["", "a b", "x" * 256])
def test_invalid_tokens(client: TestClient, token: str) -> None:
    response = client.get("/api/memories", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_signature_must_match_the_cached_jwks(
    client: TestClient, test_signing_key: rsa.RSAPrivateKey
) -> None:
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time())
    forged = jwt.encode(
        {
            "iss": _test_issuer,
            "sub": "alice",
            "token_use": "access",
            "client_id": TEST_CLIENT_ID,
            "scope": "homie/mcp:tools",
            "iat": now,
            "exp": now + 300,
        },
        forged_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    valid = jwt.encode(
        {
            "iss": _test_issuer,
            "sub": "alice",
            "token_use": "access",
            "client_id": TEST_CLIENT_ID,
            "scope": "homie/mcp:tools",
            "iat": now,
            "exp": now + 300,
        },
        test_signing_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    assert (
        client.get("/api/memories", headers={"Authorization": f"Bearer {valid}"}).status_code == 200
    )
    response = client.get("/api/memories", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_missing_credentials_return_clean_401(client: TestClient) -> None:
    for path in ("/api/memories", "/mcp"):
        response = client.get(path)
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers


def test_legacy_mock_auth_environment_variable_is_inactive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    response = client.get("/api/memories", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://wrong.example/pool"},
        {"token_use": "id"},
        {"client_id": "not-allowed"},
        {"scope": "homie/mcp:service"},
        {"exp": int(time()) - 10},
    ],
)
def test_invalid_cognito_claims_return_clean_401(
    client: TestClient, overrides: dict[str, object]
) -> None:
    response = client.get("/api/memories", headers=auth("alice", **overrides))
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication token."}
    assert "www-authenticate" not in response.headers


def test_cognito_settings_defaults_and_client_id_list_parsing() -> None:
    default_settings = Settings(_env_file=None)
    assert default_settings.cognito_issuer == ""
    assert default_settings.cognito_jwks_url == ""
    assert default_settings.cognito_client_ids == []
    parsed = Settings(_env_file=None, cognito_client_ids="client-a, client-b")
    assert parsed.cognito_client_ids == ["client-a", "client-b"]


def test_mcp_auth_gate_requires_service_scope_and_has_its_own_limiter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.app.state, "mcp_rate_limiter", RateLimiter(max_requests=1))
    service_token = auth(scope="homie/mcp:service", sub=None)
    response = mcp_initialize(client, service_token)
    assert response.status_code == 200

    limited = mcp_initialize(client, service_token)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"

    # Linked-user tokens are also accepted and use their own user rate-limit partition.
    tools_only = mcp_initialize(client, auth(scope="homie/mcp:tools"))
    assert tools_only.status_code == 200
    tools_limited = mcp_initialize(client, auth(scope="homie/mcp:tools"))
    assert tools_limited.status_code == 429

    # The separate API limiter remains unaffected by MCP requests.
    assert client.get("/api/memories", headers=auth()).status_code == 200


def test_mcp_gate_accepts_service_and_linked_user_tokens(client: TestClient) -> None:
    for headers in (auth(scope="homie/mcp:service", sub=None), auth(scope="homie/mcp:tools")):
        initialized = mcp_initialize(client, headers)
        assert initialized.status_code == 200
        assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"

        listed = mcp_request(client, headers, "tools/list", {})
        assert listed.status_code == 200
        assert {tool["name"] for tool in listed.json()["result"]["tools"]} == {
            "remember",
            "recall",
            "forget",
        }


def test_jwks_refresh_http_error_uses_stale_cached_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_middleware, "_jwks_cache", {})
    monkeypatch.setattr(auth_middleware, "_jwks_last_refresh_attempt", {})
    now = [1000.0]
    monkeypatch.setattr(auth_middleware, "monotonic", lambda: now[0])
    jwks_url = auth_middleware.settings.cognito_jwks_url
    cached_jwks = auth_middleware._load_jwks(jwks_url)

    def fail_refresh(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("JWKS endpoint is unavailable")

    monkeypatch.setattr(auth_middleware.httpx, "get", fail_refresh)
    now[0] += auth_middleware.JWKS_CACHE_TTL_SECONDS + auth_middleware.JWKS_REFRESH_THROTTLE_SECONDS

    assert auth_middleware._load_jwks(jwks_url) == cached_jwks
    assert auth_middleware.verify_token(token(), "homie/mcp:tools").sub == "alice"


@pytest.mark.parametrize("field", ["user_sub", "id", "timezone", "created_at", "updated_at"])
def test_protected_fields(client: TestClient, field: str) -> None:
    response = client.post(
        "/api/memories",
        headers=auth(),
        json={"content": "Private", "kind": "fact", field: "forged"},
    )
    assert response.status_code == 422
    response = client.patch(f"/api/memories/{uuid7()}", headers=auth(), json={field: "forged"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"content": " "},
        {"content": None},
        {"tags": None},
        {"tags": [""]},
        {"tags": ["x"] * 11},
        {"content": "x" * 2001},
    ],
)
def test_invalid_update(client: TestClient, data: dict[str, object]) -> None:
    assert client.patch(f"/api/memories/{uuid7()}", headers=auth(), json=data).status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "limit=no",
        "kind=secret",
        "q=" + "x" * 201,
    ],
)
def test_invalid_list_parameters(client: TestClient, query: str) -> None:
    assert client.get(f"/api/memories?{query}", headers=auth()).status_code == 422


def test_api_kind_and_best_first_search(client: TestClient) -> None:
    for kind, content in [
        ("fact", "keys are near the door"),
        ("fact", "keys and passport are in the spare drawer"),
        ("todo", "call the doctor"),
    ]:
        response = client.post(
            "/api/memories",
            headers=auth(),
            json={"content": content, "kind": kind},
        )
        assert response.status_code == 201
    response = client.get("/api/memories?kind=fact&q=keys+passport", headers=auth())
    assert response.status_code == 200
    assert [item["content"] for item in response.json()] == [
        "keys and passport are in the spare drawer",
        "keys are near the door",
    ]


@pytest.mark.parametrize("query", ["q=", "q=%20"])
def test_empty_query_parameter_returns_unfiltered_memories(client: TestClient, query: str) -> None:
    created = client.post(
        "/api/memories",
        headers=auth(),
        json={"content": "Keep this memory", "kind": "fact"},
    )
    assert created.status_code == 201

    response = client.get(f"/api/memories?{query}", headers=auth())

    assert response.status_code == 200
    assert response.json() == [created.json()]


def test_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.middleware.auth.monotonic", lambda: now[0])
    for _ in range(120):
        assert client.get("/api/memories", headers=auth()).status_code == 200
    response = client.get("/api/memories", headers=auth())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert client.get("/api/memories", headers=auth("bob")).status_code == 200
    assert client.get("/health").status_code == 200
    now[0] += 60
    assert client.get("/api/memories", headers=auth()).status_code == 200


def test_rate_limiter_eviction_and_no_lockout() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=60.0, max_entries=3, cleanup_interval=10.0)
    limiter.check("user1")
    limiter.check("user2")
    limiter.check("user3")
    assert len(limiter.requests) == 3
    assert list(limiter.requests.keys()) == ["user1", "user2", "user3"]

    # 4th user arrives: user1 evicted (LRU), user4 admitted without lockout
    limiter.check("user4")
    assert len(limiter.requests) == 3
    assert "user1" not in limiter.requests
    assert list(limiter.requests.keys()) == ["user2", "user3", "user4"]

    # user4 can make another request within its quota
    limiter.check("user4")
    # user4 3rd request hits per-user limit
    with pytest.raises(HTTPException) as exc_info:
        limiter.check("user4")
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "60"}


def test_rate_limiter_cleanup_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.middleware.auth.monotonic", lambda: now[0])
    limiter = RateLimiter(
        max_requests=5, window_seconds=60.0, max_entries=10, cleanup_interval=10.0
    )
    limiter.check("user1")
    now[0] = 105.0
    limiter.check("user2")

    # At now = 161.0, user1 expired (161 - 100 > 60), user2 not expired (161 - 105 <= 60)
    now[0] = 161.0
    limiter.check("user3")
    assert "user1" not in limiter.requests
    assert "user2" in limiter.requests
    assert "user3" in limiter.requests


def test_rate_limiter_concurrent_access() -> None:
    limiter = RateLimiter(max_requests=50, window_seconds=60.0, max_entries=100)

    def worker(user_id: int) -> None:
        for _ in range(10):
            limiter.check(f"user_{user_id}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(20)))
    assert len(limiter.requests) == 20


def test_many_users_do_not_lock_out_new_users(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.app.state, "api_rate_limiter", RateLimiter(max_entries=5))
    for i in range(15):
        response = client.get("/api/memories", headers=auth(f"user_{i}"))
        assert response.status_code == 200


def test_concurrent_create_and_read(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=2) as workers:
        create = workers.submit(
            client.post,
            "/api/memories",
            headers=auth(),
            json={"content": "Keep this memory", "kind": "fact"},
        )
        read = workers.submit(client.get, "/api/memories", headers=auth("bob"))
        assert create.result(timeout=5).status_code == 201
        assert read.result(timeout=5).status_code == 200
    assert len(client.get("/api/memories", headers=auth()).json()) == 1


def test_unexpected_errors_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sensitive memory content and database credentials")

    monkeypatch.setattr(MemoryService, "list_memories", fail)
    response = client.get("/api/memories", headers={**auth(), "Origin": "http://localhost:3000"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong. Please try again."}
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "sensitive" not in caplog.text
    assert "database credentials" not in response.text
    assert "Unhandled RuntimeError" in caplog.text
    assert "test_memories.py" in caplog.text
    assert "in fail" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    ("method", "path", "data", "loc", "message"),
    [
        (
            "post",
            "/api/memories",
            {"content": "private" * 1500, "kind": "fact"},
            ["body", "content"],
            "String should have at most 2000 characters",
        ),
        (
            "post",
            "/api/memories",
            {"content": "private", "kind": "fact", "tags": [""]},
            ["body", "tags", 0],
            "String should have at least 1 character",
        ),
        (
            "patch",
            f"/api/memories/{uuid7()}",
            {},
            ["body"],
            "Value error, Provide at least one field to update",
        ),
        (
            "get",
            "/api/memories?limit=private",
            None,
            ["query", "limit"],
            "Input should be a valid integer, unable to parse string as an integer",
        ),
    ],
)
def test_validation_details_do_not_echo_values(
    client: TestClient, method: str, path: str, data: dict | None, loc: list, message: str
) -> None:
    response = client.request(method, path, headers=auth(), json=data)
    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["loc"] == loc
    assert error["msg"] == message
    assert set(error) == {"loc", "msg", "type"}
    assert "private" not in response.text


def test_current_user_dependency_attaches_authenticated_sub(client: TestClient) -> None:
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", auth("alice")["Authorization"].encode())],
            "app": main.app,
        }
    )
    user = current_user(request)
    assert user.sub == "alice"
    assert request.state.user_sub == "alice"


def test_api_responses_are_never_cached(client: TestClient) -> None:
    ok = client.get("/api/memories", headers=auth())
    unauthorized = client.get("/api/memories")
    invalid = client.post("/api/memories", headers=auth(), json={"content": ""})
    assert [r.status_code for r in (ok, unauthorized, invalid)] == [200, 401, 422]
    assert all(r.headers["cache-control"] == "no-store" for r in (ok, unauthorized, invalid))
    assert "cache-control" not in client.get("/health").headers
