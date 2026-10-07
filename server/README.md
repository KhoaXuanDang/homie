# Backend foundation

From `server/`, run `uv sync`, then `uv run fastapi dev app/main.py`.
The app creates missing tables at startup and closes database connections at shutdown.
`GET /health` remains unauthenticated and returns `{"status": "ok"}`.

Configuration is loaded from `server/.env` (see `.env.example`). `DATABASE_URL`
defaults to `sqlite:///./homie.db`. Other synchronous SQLAlchemy database URLs
require their corresponding driver; tag filtering currently requires SQLite.
Schema changes to an existing database require migrations; automatic table creation
does not alter existing tables. Alembic and real Alexa authentication are deferred.

## Development authentication

With `JWT_SECRET` empty, send an `Authorization` header with scheme `Bearer`
and the desired user sub as the token. The token itself
is treated as a mock Alexa `sub`; it is **not verified**. Use this only on a trusted
local machine, never on a public deployment. Setting `JWT_SECRET` to a nonempty
value disables mock authentication and returns 503 until real OAuth/JWT
verification is implemented. It does not yet enable signed JWT authentication.
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
List supports `skip >= 0`, `1 <= limit <= 100` (default 50), and repeated `tags`
query parameters. All specified tags must match exactly, before pagination.
Results are ordered newest first with an ID tie-breaker.

Profiles are created on first memory creation with timezone `UTC`. All database
timestamps are UTC; responses convert them using the stored profile's IANA
timezone, including daylight-saving offsets. Profile editing is not exposed
in this phase. Memory bodies cannot override the profile timezone.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.