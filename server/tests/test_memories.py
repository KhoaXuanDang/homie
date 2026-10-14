from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from time import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.middleware.auth import RateLimiter, current_user
from app.services.memory_service import MemoryService

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
    assert _test_signing_key is not None
    return jwt.encode(claims, _test_signing_key, algorithm="RS256", headers={"kid": "test-key"})


def auth(user_sub: str = "alice", **overrides: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_sub, **overrides)}"}


@pytest.fixture
def client(
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
    response = client.get("/memories", headers={"Authorization": f"Bearer {token}"})
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
    assert client.get("/memories", headers={"Authorization": f"Bearer {valid}"}).status_code == 200
    response = client.get("/memories", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_missing_credentials_return_clean_401(client: TestClient) -> None:
    for path in ("/memories", "/mcp"):
        response = client.get(path)
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers


def test_legacy_mock_auth_environment_variable_is_inactive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    response = client.get("/memories", headers={"Authorization": "Bearer alice"})
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
    response = client.get("/memories", headers=auth("alice", **overrides))
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication token."}
    assert "www-authenticate" not in response.headers


def test_cognito_settings_defaults_and_client_id_list_parsing() -> None:
    default_settings = Settings(_env_file=None)
    assert default_settings.cognito_issuer == (
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_QiV9mNe4W"
    )
    assert default_settings.cognito_jwks_url == (
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_QiV9mNe4W/.well-known/jwks.json"
    )
    assert default_settings.cognito_client_ids == ["5nc9ipnu9f7s2de5tto3daag4b"]
    parsed = Settings(_env_file=None, cognito_client_ids="client-a, client-b")
    assert parsed.cognito_client_ids == ["client-a", "client-b"]


def test_mcp_auth_gate_requires_service_scope_and_has_its_own_limiter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.app.state, "mcp_rate_limiter", RateLimiter(max_requests=1))
    service_token = auth(scope="homie/mcp:service")
    response = client.get("/mcp", headers=service_token)
    # Valid credentials pass the gate; the MCP route is not registered yet.
    assert response.status_code == 404

    limited = client.get("/mcp", headers=service_token)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"

    tools_only = client.get("/mcp", headers=auth(scope="homie/mcp:tools"))
    assert tools_only.status_code == 401
    assert "www-authenticate" not in tools_only.headers

    # The separate API limiter remains unaffected by MCP requests.
    assert client.get("/memories", headers=auth()).status_code == 200


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
        response = client.get("/memories", headers=auth(f"user_{i}"))
        assert response.status_code == 200


def test_concurrent_create_and_read(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=2) as workers:
        create = workers.submit(
            client.post,
            "/memories",
            headers=auth(),
            json={"content": "Keep this memory", "memory_type": "fact"},
        )
        read = workers.submit(client.get, "/memories", headers=auth("bob"))
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
            "/memories",
            {"content": "private" * 1500, "memory_type": "fact"},
            ["body", "content"],
            "String should have at most 10000 characters",
        ),
        (
            "post",
            "/memories",
            {"content": "private", "memory_type": "fact", "tags": [""]},
            ["body", "tags", 0],
            "String should have at least 1 character",
        ),
        (
            "put",
            "/memories/missing",
            {},
            ["body"],
            "Value error, Provide at least one field to update",
        ),
        (
            "get",
            "/memories?limit=private",
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
