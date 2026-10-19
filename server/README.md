# Homie server

From `server/`, run `uv sync`, then `uv run fastapi dev app/main.py`.
`GET /health` remains unauthenticated and returns `{"status": "ok"}`.

Configuration is loaded from `server/.env` (see `.env.example`). Memories live in
the DynamoDB table `DYNAMODB_TABLE` (keys `pk`/`sk`, see the P0 spec). When
`DYNAMODB_ENDPOINT_URL` is set (DynamoDB Local), the app creates the table at
startup, waiting for the endpoint to come up; against AWS the table comes from
Terraform. Authentication uses Cognito access tokens signed by the configured
user pool.
Before starting Docker Compose from the repository root, create its required env
file with `cp server/.env.example server/.env`.
Docker Compose runs DynamoDB Local with its data in the `dynamodb-data` named
volume, so rebuilding or recreating containers keeps memories;
`docker compose down -v` deletes them.

## Cognito authentication

Set `COGNITO_USER_POOL_ID` and the allowed `COGNITO_CLIENT_IDS` (a comma-separated
list or JSON array) for the deployment. The API verifies RS256 signatures against
the pool's cached JWKS, checks the issuer, access-token use, client ID, expiration,
and required scope. Memory API requests require `MCP_TOOLS_SCOPE`. The `/mcp`
HTTP endpoint accepts either `MCP_TOOLS_SCOPE` or `MCP_SERVICE_SCOPE`, so a
service token can initialize and list tools. Calling a personal-memory tool
requires `MCP_TOOLS_SCOPE` and a linked user subject; a service-only token gets
an MCP tool error. Invalid or missing credentials return 401 without a
`WWW-Authenticate` header.
There is no mock-auth bypass. The sample pool and client IDs are development
defaults only; configure your own values for deployed environments.

Authenticated memory API requests are limited to 120 requests per user per minute
and MCP requests to 60, per process.

## MCP endpoint

`POST /mcp` serves MCP Streamable HTTP and negotiates protocol version
`2025-11-25`. The server uses stateless JSON responses, so clients do not need
to retain a session ID. Send a Cognito bearer access token and
`Accept: application/json, text/event-stream`; after `initialize`, send
`MCP-Protocol-Version: 2025-11-25` on subsequent requests. `PUBLIC_BASE_URL`
and `CORS_ORIGINS` supply the allowed server host and browser origins. When
`PUBLIC_BASE_URL` is unset, local MCP requests may use either
`localhost:8000` or `127.0.0.1:8000` as the Host. An explicitly configured
`PUBLIC_BASE_URL` allows only its host.

The endpoint exposes three tools:

| Tool | Arguments | Successful `structuredContent` |
|---|---|---|
| `remember` | `content`, optional `kind` (default `fact`), `tags`, `event_id` | `{"status":"success","memory_id":"<uuid>"}` |
| `recall` | `query`, optional `kind` | `{"memories":[{"id":"<uuid>","content":"...","kind":"fact","tags":[],"created_at":"<ISO 8601>"}]}` |
| `forget` | `memory_id` | `{"status":"success","memory_id":"<uuid>"}` |

`recall` returns at most ten memories. A blank query lists the newest memories;
a nonblank query searches the user's newest 2,000 memories, ranks matches, and
falls back to recent memories when nothing matches. Older memories are outside
the P0 search window. Results are scoped to the authenticated user, omit `event_id`, and
express `created_at` in the user's profile timezone (UTC by default). Missing
or invalid inputs and unavailable storage produce a plain-language MCP tool
error. This P0 endpoint uses preconfigured Cognito bearer tokens; MCP OAuth
discovery and audience-bound tokens are separate integration work.

## Memory API

```sh
AUTH_HEADER="$(printf '%s %s' 'Bearer' "$COGNITO_ACCESS_TOKEN")"
curl -X POST http://127.0.0.1:8000/api/memories \
  -H "Authorization: $AUTH_HEADER" \
  -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Keys are in the drawer","kind":"item_location","tags":["home"]}'
curl http://127.0.0.1:8000/api/memories?limit=20 \
  -H "Authorization: $AUTH_HEADER"
```

Memory routes live under `/api/memories`. Use `GET /api/memories/{id}`,
`PATCH /api/memories/{id}` with one or more of `content`, `kind`, `tags`, and
`event_id`, or `DELETE /api/memories/{id}` (204, no body). The create request
requires an `Idempotency-Key` UUID. Replaying the same key and normalized body
returns the original memory with 201; reusing it for a different body returns
409, and retrying after deleting the created memory returns 410.
Unknown IDs and other users' IDs both return 404. Ownership, IDs, and timestamps
cannot be supplied in create/update bodies. Empty/null updates are rejected.

Content is trimmed and bounded to 1–2,000 characters; `kind` is one of
`item_location`, `fact`, `preference`, or `todo`. Tags are at most 10 nonempty
strings of up to 40 characters each, trimmed, lowercased, and deduplicated.
Each memory has optional `event_id` and a `media_ids` list. List supports
`kind`, best-first `q` keyword search, and `1 <= limit <= 200` (default 50),
with newest memories as search tie-breakers. Recall falls back to newest memories
when the query has no matches.

A user without a stored profile gets the default timezone `UTC`. All database
timestamps are UTC; responses convert them using the stored profile's IANA
timezone, including daylight-saving offsets. Profile editing is not exposed
in this phase. Memory bodies cannot override the profile timezone.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.
