# Backend foundation

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
and required scope. Memory API requests require `MCP_TOOLS_SCOPE`; requests under
`/mcp` require `MCP_SERVICE_SCOPE`. Invalid or missing credentials return 401.
There is no mock-auth bypass. The sample pool and client IDs are development
defaults only; configure your own values for deployed environments.

Authenticated memory API requests are limited to 120 requests per user per minute
and MCP requests to 60, per process.

## Memory API

```sh
AUTH_HEADER="$(printf '%s %s' 'Bearer' "$COGNITO_ACCESS_TOKEN")"
curl -X POST http://127.0.0.1:8000/memories \
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