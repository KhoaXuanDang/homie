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

## Media API

Set `MEDIA_BUCKET` to the private S3 bucket provisioned by Terraform. Media routes
use the same Cognito authentication and per-user isolation as memories.

- `POST /api/media` accepts `{content_type, size, caption?}` and returns 201
  `{media, upload_url, upload_headers, expires_in}`. The media starts `pending`;
  `training_consent` defaults to false. The PUT URL expires after 300 seconds and
  signs both `Content-Type` and `Content-Length`. Browser XHR sets Content-Type
  and supplies Content-Length automatically from the file body; non-browser
  clients should send both returned headers. Uploads are limited to 30/minute
  per user, in addition to the general API limit.
- PUT the file directly to `upload_url`, then `POST /api/media/{id}/complete`.
  Completion returns the uploaded media (200), including on repeated calls.
  Missing objects or mismatched size/type return 409: restart with a new pending
  record and URL. S3 failures return a retryable 503, not a missing-object error.
- `GET /api/media?limit=50` returns a newest-first array of uploaded media only
  (`1 <= limit <= 200`). Each item includes `id`, `content_type`, `size`,
  `caption`, `tags`, `memory_id`, `training_consent`, `status`, `url`, and
  `created_at` (in the profile timezone, UTC by default).
- `PATCH /api/media/{id}` accepts one or more of `caption`, `tags`, `memory_id`,
  and `training_consent`. Captions are trimmed and limited to 2,000 characters;
  tags follow memory tag rules. A memory link must belong to the signed-in user;
  `memory_id: null` clears it. Other fields cannot be null or changed.
- `DELETE /api/media/{id}` removes the S3 object and record and returns 204.
  Unknown IDs and other users' IDs return 404.

Media responses use `Cache-Control: no-store`. Uploaded media receive fresh
15-minute presigned GET URLs with a signed `no-store` response directive; URLs
are never stored in DynamoDB. Pending media have an empty `url`. Clients should
refresh lists before URL expiry (the existing client uses five minutes).

On each list request, pending uploads older than one hour are removed for that
user, including any abandoned object. Cleanup conditionally claims the pending
record before touching S3, so concurrent completion cannot lose uploaded media.
A private `deleting` marker hides in-progress deletions and blocks completion;
it is retained on storage failures so the next list or explicit delete retries.
It is never returned as a public media status.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.