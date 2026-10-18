"""Alexa-facing MCP tools backed by the authenticated memory service."""

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, InputRequiredResult
from pydantic import BaseModel, ConfigDict, PlainValidator, ValidationError, WithJsonSchema

from app.config import settings
from app.middleware.auth import verify_token
from app.models.memory import Kind, MemoryCreate
from app.services.memory_service import MemoryService

_KINDS = ("item_location", "fact", "preference", "todo")


class ChangeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    memory_id: str


class RecalledMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content: str
    kind: Kind
    tags: list[str]
    created_at: str


class RecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memories: list[RecalledMemory]


def _user_sub(ctx: Context) -> str:
    authorization = (ctx.headers or {}).get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip() or " " in token.strip():
        raise ToolError("A linked user account is required for memory tools.")
    try:
        user = verify_token(token.strip(), settings.mcp_tools_scope)
    except ValueError as exc:
        raise ToolError("A linked user account is required for memory tools.") from exc
    if user.sub is None:
        raise ToolError("A linked user account is required for memory tools.")
    return user.sub


def _storage_unavailable(exc: BotoCoreError | ClientError) -> ToolError:
    return ToolError("Memory storage is unavailable. Please try again shortly.")


def _preserve_literal(value: Any) -> Any:
    # The SDK JSON-parses string arguments unless their Pydantic field type is str.
    return value


class MemoryMCPServer(MCPServer):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError("Please provide the required tool arguments.") from exc
            raise


mcp = MemoryMCPServer("homie")


@mcp.tool()
def remember(
    ctx: Context,
    content: Annotated[
        str,
        PlainValidator(_preserve_literal),
        WithJsonSchema({"type": "string", "maxLength": 2000}),
    ],
    kind: Annotated[Any, WithJsonSchema({"type": "string", "enum": list(_KINDS)})] = "fact",
    tags: Annotated[
        Any,
        WithJsonSchema(
            {
                "anyOf": [
                    {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 40},
                        "maxItems": 10,
                    },
                    {"type": "null"},
                ]
            }
        ),
    ] = None,
    event_id: Annotated[
        str,
        PlainValidator(_preserve_literal),
        WithJsonSchema({"anyOf": [{"type": "string", "maxLength": 1024}, {"type": "null"}]}),
    ] = None,
) -> ChangeResult:
    """Save something the linked user wants to remember."""
    user_sub = _user_sub(ctx)
    if not isinstance(content, str) or not content.strip():
        raise ToolError("Please tell me what to remember.")
    if len(content.strip()) > 2000:
        raise ToolError("That memory is too long. Please shorten it. Limit is 2000 characters")
    try:
        data = MemoryCreate.model_validate(
            {
                "content": content,
                "kind": kind,
                "tags": [] if tags is None else tags,
                "event_id": event_id,
            }
        )
    except ValidationError as exc:
        raise ToolError("Please check the memory type, tags, or calendar link.") from exc
    try:
        # MCP tools bypass FastAPI Depends; the service binds this worker thread's table.
        memory = MemoryService().create_memory(
            user_sub, data.content, data.kind, data.tags, data.event_id
        )
    except (BotoCoreError, ClientError) as exc:
        raise _storage_unavailable(exc) from exc
    return ChangeResult(status="success", memory_id=str(memory.id))


@mcp.tool()
def recall(
    ctx: Context,
    query: Annotated[
        str, PlainValidator(_preserve_literal), WithJsonSchema({"type": "string", "maxLength": 200})
    ],
    kind: Annotated[
        Any, WithJsonSchema({"anyOf": [{"type": "string", "enum": list(_KINDS)}, {"type": "null"}]})
    ] = None,
) -> RecallResult:
    """Find the linked user's memories, or list recent ones with a blank query."""
    user_sub = _user_sub(ctx)
    if not isinstance(query, str):
        raise ToolError("Please provide a search phrase or a blank query for recent memories.")
    query = query.strip()
    if len(query) > 200:
        raise ToolError("Please use a shorter search phrase.")
    if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
        raise ToolError("Please choose a valid memory type.")
    try:
        memories = MemoryService().recall(user_sub, query, kind=kind, limit=10)
    except (BotoCoreError, ClientError) as exc:
        raise _storage_unavailable(exc) from exc
    return RecallResult(
        memories=[
            RecalledMemory(
                id=str(memory.id),
                content=memory.content,
                kind=memory.kind,
                tags=memory.tags,
                created_at=memory.created_at.isoformat(),
            )
            for memory in memories
        ]
    )


@mcp.tool()
def forget(
    ctx: Context,
    memory_id: Annotated[Any, WithJsonSchema({"type": "string", "format": "uuid"})],
) -> ChangeResult:
    """Delete one of the linked user's memories by ID."""
    user_sub = _user_sub(ctx)
    if not isinstance(memory_id, str):
        raise ToolError("Please provide a valid memory ID.")
    try:
        parsed_id = str(UUID(memory_id))
    except ValueError as exc:
        raise ToolError("Please provide a valid memory ID.") from exc
    try:
        deleted = MemoryService().delete_memory(user_sub, parsed_id)
    except (BotoCoreError, ClientError) as exc:
        raise _storage_unavailable(exc) from exc
    if not deleted:
        raise ToolError("I couldn't find that memory.")
    return ChangeResult(status="success", memory_id=parsed_id)


_base_url = urlsplit(settings.public_base_url)
if not _base_url.scheme or not _base_url.netloc:
    raise ValueError("PUBLIC_BASE_URL must have a scheme and host")
_public_origin = f"{_base_url.scheme}://{_base_url.netloc}"
_transport_security = TransportSecuritySettings(
    allowed_hosts=[_base_url.netloc],
    allowed_origins=list(dict.fromkeys([_public_origin, *settings.cors_origins])),
)
mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security,
)
