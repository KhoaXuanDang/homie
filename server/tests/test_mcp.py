"""MCP acceptance tests through the real Streamable HTTP endpoint."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from time import time
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import jwt
import pytest
from botocore.exceptions import EndpointConnectionError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response

from app import main
from app.middleware import auth as auth_middleware
from app.middleware.auth import RateLimiter
from app.services import memory_service
from app.services.memory_service import MemoryService

PROTOCOL_VERSION = "2025-11-25"
CLIENT_ID = "test-client"


class McpClient:
    def __init__(self, http: TestClient, issuer: str, signing_key: rsa.RSAPrivateKey) -> None:
        self.http = http
        self.issuer = issuer
        self.signing_key = signing_key
        self.next_id = 1

    def token(self, user_sub: str | None = "alice", *, scope: str = "homie/mcp:tools") -> str:
        now = int(time())
        claims: dict[str, object] = {
            "iss": self.issuer,
            "token_use": "access",
            "client_id": CLIENT_ID,
            "scope": scope,
            "iat": now,
            "exp": now + 300,
        }
        if user_sub is not None:
            claims["sub"] = user_sub
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": "test-key"})

    def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        notification: bool = False,
        headers: dict[str, str] | None = None,
        path: str = "/mcp",
    ) -> Response:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.next_id
            self.next_id += 1
        if params is not None:
            payload["params"] = params
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Authorization": f"Bearer {token or self.token()}",
        }
        if headers:
            request_headers.update(headers)
        return self.http.post(path, json=payload, headers=request_headers)

    def initialize(self, *, token: str | None = None) -> Response:
        return self.post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "homie-tests", "version": "1.0"},
            },
            token=token,
        )

    def call(self, name: str, arguments: dict[str, Any], *, token: str | None = None) -> Response:
        return self.post("tools/call", {"name": name, "arguments": arguments}, token=token)


@pytest.fixture
def mcp_client(
    app_client: TestClient,
    ddb_table: Any,
    fake_jwks_endpoint: str,
    test_signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[McpClient, Any]]:
    # MCP handlers run in worker threads and construct their own MemoryService.
    # Pin each construction to this test's Moto table, independent of thread-local caches.
    monkeypatch.setattr(memory_service, "get_table", lambda: ddb_table)
    monkeypatch.setattr(main.app.state, "mcp_rate_limiter", RateLimiter(max_requests=60))
    monkeypatch.setattr(main.app.state, "api_rate_limiter", RateLimiter(max_requests=120))
    yield McpClient(app_client, fake_jwks_endpoint, test_signing_key), ddb_table


def result(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


def successful_content(response: Response) -> dict[str, Any]:
    tool_result = result(response)
    assert tool_result.get("isError") is not True, tool_result
    return tool_result["structuredContent"]


def tool_error(response: Response) -> str:
    tool_result = result(response)
    assert tool_result["isError"] is True, tool_result
    return " ".join(part.get("text", "") for part in tool_result.get("content", []))


def test_initialization_tool_schemas_and_endpoint_shape(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, _ = mcp_client
    initialized = result(mcp.initialize())
    assert initialized["protocolVersion"] == PROTOCOL_VERSION

    notification = mcp.post("notifications/initialized", notification=True)
    assert notification.status_code == 202
    assert "mcp-session-id" not in notification.headers

    listed = result(mcp.post("tools/list"))
    tools = {tool["name"]: tool for tool in listed["tools"]}
    assert set(tools) == {"remember", "recall", "forget"}
    kinds = {"item_location", "fact", "preference", "todo"}

    remember_input = tools["remember"]["inputSchema"]
    assert remember_input["type"] == "object"
    assert set(remember_input["required"]) == {"content"}
    assert set(remember_input["properties"]) == {"content", "kind", "tags", "event_id"}
    assert remember_input["properties"]["content"]["type"] == "string"
    assert remember_input["properties"]["content"]["maxLength"] == 2000
    assert remember_input["properties"]["kind"]["type"] == "string"
    assert set(remember_input["properties"]["kind"]["enum"]) == kinds
    assert remember_input["properties"]["kind"]["default"] == "fact"
    tags_array, tags_null = remember_input["properties"]["tags"]["anyOf"]
    assert tags_array["type"] == "array"
    assert tags_array["items"] == {"type": "string", "maxLength": 40}
    assert tags_array["maxItems"] == 10
    assert tags_null["type"] == "null"
    event_string, event_null = remember_input["properties"]["event_id"]["anyOf"]
    assert event_string["type"] == "string"
    assert event_string["maxLength"] == 1024
    assert event_null["type"] == "null"

    recall_input = tools["recall"]["inputSchema"]
    assert recall_input["type"] == "object"
    assert set(recall_input["required"]) == {"query"}
    assert set(recall_input["properties"]) == {"query", "kind"}
    assert recall_input["properties"]["query"]["type"] == "string"
    assert recall_input["properties"]["query"]["maxLength"] == 200
    assert recall_input["properties"]["kind"]["type"] == "string"
    assert set(recall_input["properties"]["kind"]["enum"]) == kinds

    forget_input = tools["forget"]["inputSchema"]
    assert forget_input["type"] == "object"
    assert set(forget_input["required"]) == {"memory_id"}
    assert set(forget_input["properties"]) == {"memory_id"}
    assert forget_input["properties"]["memory_id"]["type"] == "string"
    assert forget_input["properties"]["memory_id"]["format"] == "uuid"

    for name in ("remember", "forget"):
        output = tools[name]["outputSchema"]
        assert output["type"] == "object"
        assert output["additionalProperties"] is False
        assert set(output["required"]) == {"status", "memory_id"}
        assert set(output["properties"]) == {"status", "memory_id"}
        assert output["properties"]["status"]["type"] == "string"
        assert output["properties"]["status"]["const"] == "success"
        assert output["properties"]["memory_id"]["type"] == "string"

    recall_output = tools["recall"]["outputSchema"]
    assert recall_output["type"] == "object"
    assert recall_output["additionalProperties"] is False
    assert set(recall_output["required"]) == {"memories"}
    assert set(recall_output["properties"]) == {"memories"}
    memories = recall_output["properties"]["memories"]
    assert memories["type"] == "array"
    assert memories["items"] == {"$ref": "#/$defs/RecalledMemory"}
    recalled_memory = recall_output["$defs"]["RecalledMemory"]
    assert recalled_memory["type"] == "object"
    assert recalled_memory["additionalProperties"] is False
    expected_fields = {"id", "content", "kind", "tags", "created_at"}
    assert set(recalled_memory["required"]) == expected_fields
    assert set(recalled_memory["properties"]) == expected_fields
    properties = recalled_memory["properties"]
    assert properties["id"]["type"] == "string"
    assert properties["content"]["type"] == "string"
    assert properties["kind"]["type"] == "string"
    assert set(properties["kind"]["enum"]) == kinds
    assert properties["tags"]["type"] == "array"
    assert properties["tags"]["items"]["type"] == "string"
    assert properties["created_at"]["type"] == "string"

    assert mcp.post("initialize", path="/mcp/mcp").status_code == 404


def test_remember_recall_forget_and_profile_timezone(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, table = mcp_client
    table.put_item(Item={"pk": "USER#alice", "sk": "PROFILE", "timezone": "America/Chicago"})
    result(mcp.initialize())

    saved = successful_content(
        mcp.call(
            "remember",
            {
                "content": "Passport is in the blue drawer",
                "tags": ["travel"],
                "event_id": "cal-123",
            },
        )
    )
    assert set(saved) == {"status", "memory_id"}
    assert saved["status"] == "success"
    memory_id = str(UUID(saved["memory_id"]))
    stored = table.get_item(Key={"pk": "USER#alice", "sk": f"MEM#{memory_id}"})["Item"]
    assert stored["event_id"] == "cal-123"

    recalled = successful_content(mcp.call("recall", {"query": "passport"}))
    assert set(recalled) == {"memories"}
    assert len(recalled["memories"]) == 1
    memory = recalled["memories"][0]
    assert set(memory) == {"id", "content", "kind", "tags", "created_at"}
    assert memory["id"] == memory_id
    assert memory["content"] == "Passport is in the blue drawer"
    assert memory["kind"] == "fact"
    assert memory["tags"] == ["travel"]
    created_at = datetime.fromisoformat(memory["created_at"])
    assert created_at.utcoffset() is not None
    assert created_at.utcoffset() == created_at.astimezone(ZoneInfo("America/Chicago")).utcoffset()

    deleted = successful_content(mcp.call("forget", {"memory_id": memory_id}))
    assert deleted == {"status": "success", "memory_id": memory_id}
    assert successful_content(mcp.call("recall", {"query": "passport"})) == {"memories": []}
    assert tool_error(mcp.call("forget", {"memory_id": memory_id}))


@pytest.mark.parametrize("literal", ["null", "[]"])
def test_json_looking_strings_stay_literal(mcp_client: tuple[McpClient, Any], literal: str) -> None:
    mcp, table = mcp_client
    result(mcp.initialize())
    memory_id = successful_content(mcp.call("remember", {"content": literal, "event_id": literal}))[
        "memory_id"
    ]
    stored = table.get_item(Key={"pk": "USER#alice", "sk": f"MEM#{memory_id}"})["Item"]
    assert stored["content"] == literal
    assert stored["event_id"] == literal

    recalled = successful_content(mcp.call("recall", {"query": literal}))["memories"]
    assert len(recalled) == 1
    assert recalled[0]["id"] == memory_id
    assert recalled[0]["content"] == literal
    assert "event_id" not in recalled[0]


@pytest.mark.parametrize("kind", [None, "null", "[]"])
def test_explicit_null_or_json_looking_kind_is_invalid(
    mcp_client: tuple[McpClient, Any], kind: Any
) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    assert tool_error(mcp.call("remember", {"content": "Do not save", "kind": kind}))
    assert successful_content(mcp.call("recall", {"query": ""})) == {"memories": []}
    assert tool_error(mcp.call("recall", {"query": "", "kind": kind}))


@pytest.mark.parametrize("memory_id", [None, "null", "[]", "123"])
def test_json_looking_memory_ids_are_rejected(
    mcp_client: tuple[McpClient, Any], memory_id: Any
) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    assert tool_error(mcp.call("forget", {"memory_id": memory_id}))


def test_recall_timestamp_defaults_to_utc_without_profile(
    mcp_client: tuple[McpClient, Any],
) -> None:
    mcp, table = mcp_client
    result(mcp.initialize())
    assert "Item" not in table.get_item(Key={"pk": "USER#alice", "sk": "PROFILE"})
    memory_id = successful_content(mcp.call("remember", {"content": "No profile yet"}))["memory_id"]
    memories = successful_content(mcp.call("recall", {"query": "profile"}))["memories"]
    assert len(memories) == 1
    assert memories[0]["id"] == memory_id
    assert datetime.fromisoformat(memories[0]["created_at"]).utcoffset() == timedelta(0)


def test_recall_limits_scoping_ranking_and_fallback(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    saved_ids = [
        successful_content(mcp.call("remember", {"content": f"Fact number {i}"}))["memory_id"]
        for i in range(11)
    ]
    todo_id = successful_content(
        mcp.call("remember", {"content": "Call the dentist", "kind": "todo"})
    )["memory_id"]
    bob = mcp.token("bob")
    successful_content(mcp.call("remember", {"content": "Bob's private fact"}, token=bob))

    recent = successful_content(mcp.call("recall", {"query": "  ", "kind": "fact"}))["memories"]
    assert len(recent) == 10
    assert [memory["id"] for memory in recent] == list(reversed(saved_ids[1:]))
    assert all(memory["kind"] == "fact" for memory in recent)
    assert todo_id not in {memory["id"] for memory in recent}
    assert all(memory["content"] != "Bob's private fact" for memory in recent)

    ranked = successful_content(mcp.call("recall", {"query": " dentist "}))["memories"]
    assert ranked[0]["id"] == todo_id
    fallback = successful_content(mcp.call("recall", {"query": "unmatched-phrase"}))["memories"]
    assert fallback[0]["id"] == todo_id

    boundary = "dentist " + "x" * 192
    assert len(boundary) == 200
    assert (
        successful_content(mcp.call("recall", {"query": boundary}))["memories"][0]["id"] == todo_id
    )
    assert tool_error(mcp.call("recall", {"query": "x" * 201}))


def test_user_isolation_and_empty_recall(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    alice_id = successful_content(
        mcp.call("remember", {"content": "The safe combination is private"})
    )["memory_id"]
    bob = mcp.token("bob")
    assert successful_content(mcp.call("recall", {"query": ""}, token=bob)) == {"memories": []}
    assert tool_error(mcp.call("forget", {"memory_id": alice_id}, token=bob))
    assert (
        successful_content(mcp.call("recall", {"query": "safe"}))["memories"][0]["id"] == alice_id
    )


def test_http_auth_and_service_scope(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, _ = mcp_client
    invalid = mcp.initialize(token="invalid")
    assert invalid.status_code == 401
    assert "www-authenticate" not in invalid.headers
    no_auth = mcp.post("tools/list", headers={"Authorization": ""})
    assert no_auth.status_code == 401
    assert "www-authenticate" not in no_auth.headers

    service_token = mcp.token(None, scope="homie/mcp:service")
    assert result(mcp.initialize(token=service_token))["protocolVersion"] == PROTOCOL_VERSION
    assert {
        tool["name"] for tool in result(mcp.post("tools/list", token=service_token))["tools"]
    } == {
        "remember",
        "recall",
        "forget",
    }
    for name, arguments in [
        ("remember", {"content": "service token should fail"}),
        ("recall", {"query": ""}),
        ("forget", {"memory_id": str(uuid4())}),
    ]:
        assert tool_error(mcp.call(name, arguments, token=service_token))
    service_token_with_sub = mcp.token("alice", scope="homie/mcp:service")
    assert tool_error(mcp.call("recall", {"query": ""}, token=service_token_with_sub)) == (
        "A linked user account is required for memory tools."
    )
    assert successful_content(mcp.call("recall", {"query": ""})) == {"memories": []}


def test_mcp_tool_call_verifies_jwt_once(
    mcp_client: tuple[McpClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = mcp_client
    original_verify = auth_middleware.verify_token
    count = 0

    def counted_verify(token: str, required_scope: str | tuple[str, ...]) -> Any:
        nonlocal count
        count += 1
        return original_verify(token, required_scope)

    monkeypatch.setattr(auth_middleware, "verify_token", counted_verify)
    for name, arguments in [
        ("remember", {"content": "Verify once"}),
        ("recall", {"query": "verify"}),
        ("forget", {"memory_id": str(uuid4())}),
    ]:
        before = count
        response = mcp.call(name, arguments)
        assert response.status_code == 200
        assert count == before + 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "  "},
        {"content": "private" * 400},
    ],
)
def test_remember_content_errors_are_safe(
    mcp_client: tuple[McpClient, Any], arguments: dict[str, Any]
) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    response = mcp.call("remember", arguments)
    assert tool_error(response)
    assert "privateprivate" not in response.text


def test_forget_invalid_ids_and_storage_failure_are_safe(
    mcp_client: tuple[McpClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    assert tool_error(mcp.call("forget", {"memory_id": "not-a-uuid"}))
    assert tool_error(mcp.call("forget", {"memory_id": str(uuid4())}))

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise EndpointConnectionError(endpoint_url="https://private-storage.example")

    monkeypatch.setattr(MemoryService, "recall", unavailable)
    response = mcp.call("recall", {"query": "anything"})
    assert tool_error(response)
    assert "private-storage.example" not in response.text


def test_expected_tool_errors_keep_exact_safe_text(mcp_client: tuple[McpClient, Any]) -> None:
    mcp, _ = mcp_client
    assert tool_error(mcp.call("forget", {"memory_id": "not-a-uuid"})) == (
        "Please provide a valid memory ID."
    )
    assert tool_error(mcp.call("remember", {"content": "  "})) == (
        "Please tell me what to remember."
    )
    assert tool_error(mcp.call("recall", {})) == (
        "Please check the required tool arguments and their format."
    )
    assert tool_error(mcp.call("PRIVATE-UNKNOWN-TOOL", {})) == ("Unable to run that memory tool.")


def test_unexpected_tool_errors_remain_generic(
    mcp_client: tuple[McpClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = mcp_client

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Private internal failure details")

    monkeypatch.setattr(MemoryService, "recall", crash)
    response = mcp.call("recall", {"query": "anything"})
    assert tool_error(response) == "Error executing tool recall"
    assert "Private internal failure details" not in response.text


@pytest.mark.parametrize(
    ("name", "arguments", "secret"),
    [
        ("remember", {}, ""),
        (
            "remember",
            {"kind": "fact", "tags": ["PRIVATE-MEMORY-DATA"]},
            "PRIVATE-MEMORY-DATA",
        ),
        ("remember", {"content": "valid", "kind": "private-invalid-kind"}, "private-invalid-kind"),
        ("remember", {"content": "valid", "tags": ["private" * 20]}, "privateprivate"),
        ("remember", {"content": "valid", "event_id": "private" * 200}, "privateprivate"),
        ("recall", {}, ""),
        ("recall", {"kind": "PRIVATE-MEMORY-DATA"}, "PRIVATE-MEMORY-DATA"),
        ("forget", {}, ""),
        ("forget", {"noise": "PRIVATE-MEMORY-DATA"}, "PRIVATE-MEMORY-DATA"),
    ],
)
def test_invalid_tool_arguments_are_safe(
    mcp_client: tuple[McpClient, Any], name: str, arguments: dict[str, Any], secret: str
) -> None:
    mcp, _ = mcp_client
    result(mcp.initialize())
    response = mcp.call(name, arguments)
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("result", {}).get("isError") is True or "error" in payload
    if secret:
        assert secret not in response.text


def test_origin_host_cors_rate_limit_and_rest_routes(
    mcp_client: tuple[McpClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = mcp_client
    allowed_origin = "http://localhost:3000"
    preflight = mcp.http.options(
        "/mcp",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == allowed_origin
    assert "POST" in preflight.headers["access-control-allow-methods"]
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    browser_request = mcp.post("tools/list", headers={"Origin": allowed_origin})
    assert {tool["name"] for tool in result(browser_request)["tools"]} == {
        "remember",
        "recall",
        "forget",
    }
    assert browser_request.headers["access-control-allow-origin"] == allowed_origin
    assert "origin" in browser_request.headers["vary"].lower()
    tokenless_browser_request = mcp.post(
        "tools/list", headers={"Origin": allowed_origin, "Authorization": ""}
    )
    assert tokenless_browser_request.status_code == 401
    assert tokenless_browser_request.headers["access-control-allow-origin"] == allowed_origin
    assert "www-authenticate" not in tokenless_browser_request.headers
    assert result(mcp.initialize())["protocolVersion"] == PROTOCOL_VERSION
    assert mcp.post("tools/list", headers={"Origin": "http://evil.example"}).status_code in {
        400,
        403,
        421,
    }
    assert mcp.post("tools/list", headers={"Host": "evil.example"}).status_code in {
        400,
        403,
        421,
    }

    monkeypatch.setattr(main.app.state, "mcp_rate_limiter", RateLimiter(max_requests=1))
    assert result(mcp.post("tools/list"))["tools"]
    limited = mcp.post("tools/list")
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert mcp.http.get("/health").json() == {"status": "ok"}
    assert (
        mcp.http.get(
            "/api/memories", headers={"Authorization": f"Bearer {mcp.token()}"}
        ).status_code
        == 200
    )
