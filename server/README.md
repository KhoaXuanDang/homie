# Backend foundation

From `server/`, run `uv sync`, then `uv run fastapi dev app/main.py`.
`GET /health` remains unauthenticated and returns `{"status": "ok"}`.

Configuration is loaded from `server/.env` (see `.env.example`). Memories live in
the DynamoDB table `DYNAMODB_TABLE` (keys `pk`/`sk`, see the P0 spec). When
`DYNAMODB_ENDPOINT_URL` is set (DynamoDB Local), the app creates the table at
startup, waiting for the endpoint to come up; against AWS the table comes from
Terraform. Real Alexa authentication is deferred.
Before starting Docker Compose from the repository root, create its required env
file with `cp server/.env.example server/.env`.
Docker Compose runs DynamoDB Local with its data in the `dynamodb-data` named
volume, so rebuilding or recreating containers keeps memories;
`docker compose down -v` deletes them.

## Development authentication

Authentication fails closed by default. For local development, explicitly set
`ALLOW_MOCK_AUTH=true` with `JWT_SECRET` empty, then send an `Authorization` header with scheme `Bearer`
and the desired user sub as the token. The token itself
is treated as a mock Alexa `sub`; it is **not verified**. Use this only on a trusted
local machine, never on a public deployment. Setting `JWT_SECRET` to a nonempty
value disables mock authentication and returns 503 until real OAuth/JWT
verification is implemented. It does not yet enable signed JWT authentication.
Without the explicit mock opt-in, authenticated memory routes return 503.
Memory routes are limited to 120 requests per user per minute in each process.

## Memory API

```sh
AUTH_HEADER="$(printf '%s %s' 'Bearer' 'alice')"
curl -X POST http://127.0.0.1:8000/memories \
  -H "Authorization: $AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Keys are in the drawer","memory_type":"item_location","tags":["home"]}'
curl http://127.0.0.1:8000/memories?limit=20 \
  -H "Authorization: $AUTH_HEADER"
```

Use `GET /memories/{id}`, `PUT /memories/{id}` with one or more of `content`,
`memory_type`, and `tags`, or `DELETE /memories/{id}` (204, no body).
Unknown IDs and other users' IDs both return 404. Ownership, IDs, and timestamps
cannot be supplied in create/update bodies. Empty/null updates are rejected.

Content is trimmed and bounded to 1–10,000 characters; `memory_type` is a
nonempty string of up to 50 characters. Tags are at most 20 nonempty strings of
up to 50 characters each, trimmed and deduplicated, with case preserved.
Repeated content creates separate memories with different IDs.
List supports `0 <= skip <= 2**63 - 1`, `1 <= limit <= 100` (default 50), and repeated `tags`
query parameters. All specified tags must match exactly, before pagination.
Results are ordered newest first (IDs are UUIDv7, so key order is creation order).

A user without a stored profile gets the default timezone `UTC`. All database
timestamps are UTC; responses convert them using the stored profile's IANA
timezone, including daylight-saving offsets. Profile editing is not exposed
in this phase. Memory bodies cannot override the profile timezone.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.