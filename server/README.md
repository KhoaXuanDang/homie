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
Each memory has optional `event_id` and a `media_ids` list. Detail responses derive
`media_ids` from linked media; list and recall responses return an empty list to avoid
scanning the user's entire media partition. List supports
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
The API role needs S3 GetObject, PutObject and DeleteObject, plus access to the
DynamoDB table (including BatchGetItem and TransactWriteItems). The maintenance
role additionally needs ListBucket restricted to `users/*/media/` prefixes.
Listing media never requires S3 ListBucket, HEAD or DeleteObject permissions.

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
  (`1 <= limit <= 200`), querying at most the newest 200 media records. If pending
  or deleting records fill that page, the result may contain fewer than `limit`
  uploaded items. Each item includes `id`, `content_type`, `size`,
  `caption`, `tags`, `memory_id`, `training_consent`, `status`, `url`, and
  `created_at` (in the profile timezone, UTC by default).
- `PATCH /api/media/{id}` accepts one or more of `caption`, `tags`, `memory_id`,
  and `training_consent`. Captions are trimmed and limited to 2,000 characters;
  tags follow memory tag rules. A memory link must belong to the signed-in user;
  `memory_id: null` clears it. Other fields cannot be null or changed.
- `DELETE /api/media/{id}` removes the S3 object, hides the record, and returns
  204. Unknown IDs, deleted IDs, and other users' IDs return 404.

Media responses use `Cache-Control: no-store`. Uploaded media receive fresh
15-minute presigned GET URLs with a signed `no-store` response directive; URLs
are never stored in DynamoDB. Pending media have an empty `url`. Clients should
refresh lists before URL expiry (the existing client uses five minutes).

`GET /api/media` is read-only: it queries uploaded records, reads the profile once,
batch-checks linked memories consistently, and signs URLs locally. It never
contacts S3 or performs cleanup. Missing `MEDIA_BUCKET` still fails URL signing.
Storage outages (DynamoDB or S3) return a sanitized retryable 503.

`media.memory_id` is the only stored link authority. Memory responses derive
`media_ids` in one paginated traversal of that user's media partition (not one
query per memory). Legacy reverse lists are ignored. Linking atomically checks
memory existence and updates the media. Deleting a memory does not rewrite media:
consistent batch existence checks hide dangling references in media responses,
without racing a newer relink or unlink.

### Scheduled maintenance

Run from `server/`, with deployment credentials and the same configuration as the API:

```sh
uv run python -m scripts.maintain_media --user-sub alice --limit 100
```

Schedule repeated invocations for each provisioned user subject. User discovery
and scheduling are deployment responsibilities; never accept the subject from an
untrusted HTTP body. Each invocation processes **one page**, evaluating at most
`--limit` records or objects (`1..200`, default 100), including nonmatching records.
The `MAINTENANCE#media` item in that user's partition persists phase and pagination
progress across processes. Phases alternate between media records and S3 objects.
Continue running after a completed cycle to catch later abandoned uploads. Each
successful `POST /api/media` also advances one bounded maintenance page for that
user; cleanup failures do not fail the upload. This opportunistic pass does not
replace scheduled runs for users who do not start another upload.
Checkpoints advance only after successful page processing; failures exit nonzero
with a sanitized retry message and can safely retry. Overlapping workers use
conditional checkpoint writes; prefer one worker per subject.

Maintenance conditionally claims pending uploads older than one hour using the
original status and timestamp snapshot before removing their objects. Timestamps
are compared as datetimes (including legacy offsets); new writes use fixed
microsecond UTC precision. A private `deleting` marker blocks completion and
repeated client deletion (404). Failed S3 removals retain it for maintenance retries.
Successful deletions retain fresh markers until one hour after upload creation;
fresh completed markers cause no repeated S3 deletes. After retention, maintenance
deletes again to catch a late authorized PUT, then removes the marker.

The S3 phase reconciles objects older than one hour under only that user's prefix
using consistent batch record reads, preserving valid records and other users'
objects. It catches PUTs that finish after marker removal: URL expiry does not
bound an already-running upload. Neither markers nor tombstones are returned.

Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.
