# P0 Plan 1 — Foundation + Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first end-to-end slice of homie: Cognito-authenticated Alexa+ MCP server with `remember` / `recall` / `forget`, a memory REST API, and a dashboard where a signed-in user views and edits their memories.

**Architecture:** One FastAPI app serves the MCP endpoint (official `mcp` v2 SDK, stateless Streamable HTTP, mounted at `/mcp`) and the `/api/*` REST routes; both call `app/services/memory.py`, which stores items in a single DynamoDB table keyed by the Cognito `sub`. Every request carries a Cognito access token verified against the pool's JWKS. The Next.js 16 dashboard signs in with Cognito Hosted UI + PKCE (`react-oidc-context`) and calls the REST API with typed `openapi-fetch` + TanStack Query.

**Tech Stack:** Python 3.14, uv, FastAPI, `mcp` 2.x, PyJWT, boto3, moto, pytest · Next.js 16, React 19, bun, shadcn/ui (base-nova), react-oidc-context, TanStack Query, openapi-typescript, openapi-fetch · Terraform (AWS provider 6.x) · GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-03-p0-design.md` (sections 2, 3, 4, 5, 6, 7, 8, 9, 11, 12 milestones 0–2).

**Later plans (not this one):** Plan 2 Google (Calendar + Maps + settings), Plan 3 Media (S3) + Simulator (Bedrock), Plan 4 Deploy (EC2 + Caddy + Alexa registration + latency script).

## Global Constraints

- MCP spec **2025-11-25**, **Streamable HTTP**, stateless; unauthenticated MCP requests → `401` **without** `WWW-Authenticate`.
- MCP tool round-trip **< 500 ms**; **no LLM calls inside tools**.
- Every read/write is scoped to the verified token's `sub`; never trust a user id from the request body/args.
- Tokens: RS256, `iss` = Cognito pool, `token_use == "access"`, `client_id` ∈ configured clients, required scope present.
- DynamoDB table `homie-{env}`, keys `pk`/`sk` (strings); memories at `pk=USER#{sub}`, `sk=MEM#{uuid7}`.
- Memory `kind` ∈ `item_location | fact | preference | todo`; content 1–2000 chars after trim; ≤ 10 tags, each 1–40 chars, lowercased.
- Rate limits per `sub`: MCP tools 60/min, other `/api/*` 120/min; in-memory sliding window.
- List endpoints: `limit` default 50, max 200. `recall` returns ≤ 10.
- Python **3.14** (stdlib `uuid.uuid7`). Server config only via `app/config.py` `settings`; new env vars also go in `server/.env.example`.
- Client: responsive from 375 px, tap targets ≥ 44 px, loading/empty/error state on every data view, confirm destructive actions (AGENTS.md checklist). Prettier style: no semicolons.
- Never commit `.env` files; branch + PR per milestone; stage explicit paths.

## Review Focus

1. **Wrong kind of token** — an ID token, a token from another pool, or one signed by a different key must be rejected with 401 (tests in Task 3).
2. **Junk memory input** — whitespace-only content, 2001+ chars, 11 tags → REST 422 / MCP speakable error, never a 500 or a blank memory (tests in Tasks 5, 6, 7).
3. **Vague recall** — "where is it?" (all stopwords/punctuation) must fall back to recent memories, not return nothing (test in Task 5).
4. **Stale IDs** — PATCH/DELETE of an already-deleted memory or another user's memory returns 404 and never re-creates it (tests in Task 6); `forget` with a non-UUID id gives a speakable error (Task 7).
5. **Expired dashboard session** — a 401 from the API sends the user back to sign-in instead of showing a dead error page (manual check in Task 11, code in Task 9).

---

## File structure

```
infra/terraform/
  main.tf            (modify) provider + variables + locals
  data.tf            (create) DynamoDB table, S3 media bucket (+ public access block, SSE, CORS)
  auth.tf            (create) Cognito pool, domain, resource server, dashboard/alexa clients
  outputs.tf         (create) values for server/.env and client/.env.local
docs/superpowers/spikes/
  2026-10-04-alexa-cognito-auth.md   (create) spike findings + decisions
server/
  pyproject.toml     (modify) python 3.14, deps, pytest config
  .python-version    (modify) 3.14
  .env.example       (modify) new settings
  .gitignore         (modify) openapi.json
  app/config.py      (modify) Cognito/DynamoDB/scope settings
  app/auth.py        (create) token verification, FastAPI dependency, MCP ASGI gate
  app/ratelimit.py   (create) sliding-window limiter + shared instances
  app/db.py          (create) DynamoDB table accessor + key helpers
  app/models/memory.py      (create) Memory schemas
  app/services/memory.py    (create) CRUD + list + recall ranking
  app/routes/wellknown.py   (create) OAuth AS metadata
  app/routes/memories.py    (create) REST CRUD
  app/mcp_server.py  (create) MCPServer + remember/recall/forget
  app/main.py        (modify) lifespan, routers, middleware, MCP mount
  scripts/export_openapi.py (create) dump OpenAPI for client typegen
  tests/conftest.py  (create) env isolation, token factory, moto table, TestClient
  tests/test_auth.py, test_ratelimit.py, test_wellknown.py, test_memory_service.py,
  tests/test_memories_api.py, test_mcp.py   (create)
infra/docker/server.Dockerfile (modify) python 3.14 base image
.github/workflows/ci.yml       (create)
client/
  .gitignore         (modify) allow .env.example
  .env.example       (create)
  package.json       (modify) deps + gen:api + typecheck scripts
  src/lib/env.ts     (create)
  src/lib/api/schema.d.ts   (generated) OpenAPI types
  src/lib/api/client.ts     (create) useApi() — typed fetch with bearer + 401 → sign-in
  src/lib/api/memories.ts   (create) TanStack Query hooks
  src/components/providers.tsx   (create) AuthProvider + QueryClientProvider + Toaster
  src/components/require-auth.tsx (create)
  src/components/app-shell.tsx   (create) sidebar (md+) / bottom tabs (mobile)
  src/components/memories/kinds.ts, memory-card.tsx, memory-dialog.tsx (create)
  src/app/layout.tsx (modify) Providers, metadata
  src/app/page.tsx   (replace) sign-in landing
  src/app/auth/callback/page.tsx (create)
  src/app/(app)/layout.tsx       (create) RequireAuth + AppShell
  src/app/(app)/memories/page.tsx (create)
AGENTS.md            (modify) python 3.14, env setup, gen:api
```

---

### Task 0: Prerequisites

- [ ] **Step 1: Merge scaffold PRs and branch**

PRs #1 (server), #2 (client), #3 (infra), #4 (docs) must be merged to `main` first.

```bash
git checkout main && git pull
git checkout -b feat/p0-foundation-memory
```

- [ ] **Step 2: Confirm tools**

Run: `uv --version && bun --version && terraform version && aws sts get-caller-identity`
Expected: versions print and `aws sts` shows the team AWS account (configure `AWS_PROFILE` if not). `uv` may live at `~/.local/bin/uv`.

---

### Task 1: Terraform dev stack (Cognito, DynamoDB, S3)

**Files:**
- Modify: `infra/terraform/main.tf`
- Create: `infra/terraform/data.tf`, `infra/terraform/auth.tf`, `infra/terraform/outputs.tf`

**Interfaces:**
- Produces (Terraform outputs consumed by Tasks 2, 3, 9, 11): `aws_region`, `dynamodb_table`, `media_bucket`, `cognito_user_pool_id`, `cognito_issuer`, `cognito_domain_url`, `dashboard_client_id`, `alexa_service_client_id`, `alexa_service_client_secret` (sensitive), `alexa_link_client_id`, `alexa_link_client_secret` (sensitive), `mcp_tools_scope`, `mcp_service_scope`.

- [ ] **Step 1: Replace `infra/terraform/main.tf`**

```hcl
# dev-note: local state, one owner runs apply; move to an S3 backend before a second person applies
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "dashboard_urls" {
  description = "Origins allowed to sign in and upload (no trailing slash)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "alexa_redirect_uris" {
  description = "Account-linking redirect URIs shown by the alexa-ai tooling. The alexa-link client is created once this is set."
  type        = list(string)
  default     = []
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "homie", env = var.env }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = "homie-${var.env}"
}
```

- [ ] **Step 2: Create `infra/terraform/data.tf`**

```hcl
resource "aws_dynamodb_table" "main" {
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_s3_bucket" "media" {
  bucket = "${local.name}-media-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  cors_rule {
    allowed_methods = ["GET", "PUT"]
    allowed_origins = var.dashboard_urls
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}
```

- [ ] **Step 3: Create `infra/terraform/auth.tf`**

```hcl
resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_resource_server" "mcp" {
  identifier   = "homie"
  name         = "homie-mcp"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "mcp:tools"
    scope_description = "Use homie tools on behalf of the user"
  }
  scope {
    scope_name        = "mcp:service"
    scope_description = "Service-level access for Alexa"
  }
}

locals {
  tools_scope   = "${aws_cognito_resource_server.mcp.identifier}/mcp:tools"
  service_scope = "${aws_cognito_resource_server.mcp.identifier}/mcp:service"
}

resource "aws_cognito_user_pool_client" "dashboard" {
  name                                 = "dashboard"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile", local.tools_scope]
  callback_urls                        = [for u in var.dashboard_urls : "${u}/auth/callback"]
  logout_urls                          = var.dashboard_urls
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user_pool_client" "alexa_service" {
  name                                 = "alexa-service"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = [local.service_scope]
  supported_identity_providers         = ["COGNITO"]
}

resource "aws_cognito_user_pool_client" "alexa_link" {
  count                                = length(var.alexa_redirect_uris) > 0 ? 1 : 0
  name                                 = "alexa-link"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = [local.tools_scope]
  callback_urls                        = var.alexa_redirect_uris
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
}
```

- [ ] **Step 4: Create `infra/terraform/outputs.tf`**

```hcl
output "aws_region" { value = var.region }
output "dynamodb_table" { value = aws_dynamodb_table.main.name }
output "media_bucket" { value = aws_s3_bucket.media.bucket }
output "cognito_user_pool_id" { value = aws_cognito_user_pool.main.id }
output "cognito_issuer" { value = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}" }
output "cognito_domain_url" { value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com" }
output "dashboard_client_id" { value = aws_cognito_user_pool_client.dashboard.id }
output "alexa_service_client_id" { value = aws_cognito_user_pool_client.alexa_service.id }
output "alexa_service_client_secret" {
  value     = aws_cognito_user_pool_client.alexa_service.client_secret
  sensitive = true
}
output "alexa_link_client_id" { value = try(aws_cognito_user_pool_client.alexa_link[0].id, "") }
output "alexa_link_client_secret" {
  value     = try(aws_cognito_user_pool_client.alexa_link[0].client_secret, "")
  sensitive = true
}
output "mcp_tools_scope" { value = local.tools_scope }
output "mcp_service_scope" { value = local.service_scope }
```

- [ ] **Step 5: Validate**

Run: `cd infra/terraform && terraform fmt && terraform init && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Apply the dev stack (one team member only)**

Run: `terraform apply`
Expected: plan creates ~11 resources; after `yes`, outputs print. Save `terraform output -json` values into the team password manager (secrets via `terraform output -raw alexa_service_client_secret`).

- [ ] **Step 7: Commit**

```bash
git add infra/terraform/main.tf infra/terraform/data.tf infra/terraform/auth.tf infra/terraform/outputs.tf infra/terraform/.terraform.lock.hcl
git commit -m "feat(infra): add Cognito, DynamoDB and S3 dev stack"
```

---

### Task 2: Spike — Alexa ↔ Cognito auth (throwaway code, committed findings)

**Purpose:** answer spec §4 risk before building on it. Code written here is **not committed**; only the findings doc is.

**Files:**
- Create: `docs/superpowers/spikes/2026-10-04-alexa-cognito-auth.md`

**Interfaces:**
- Produces: final values for `MCP_TOOLS_SCOPE`, `MCP_SERVICE_SCOPE`, `alexa_redirect_uris`, and a go/no-go on Cognito for `client_credentials` (used by Task 3 settings and the Plan 4 deploy).

- [ ] **Step 1: Does Cognito accept the `resource` parameter on `client_credentials`?**

```bash
DOMAIN=$(terraform -chdir=infra/terraform output -raw cognito_domain_url)
ID=$(terraform -chdir=infra/terraform output -raw alexa_service_client_id)
SECRET=$(terraform -chdir=infra/terraform output -raw alexa_service_client_secret)
curl -s -u "$ID:$SECRET" "$DOMAIN/oauth2/token" \
  -d grant_type=client_credentials \
  -d scope=homie/mcp:service \
  -d resource=https://example.trycloudflare.com/mcp
```

Expected: JSON with `access_token`. Record: success or the exact error. Decode the token at a JWT debugger (or `cut -d. -f2 | base64 -d`) and record the `scope` claim string exactly (expected `homie/mcp:service`) and that `client_id` and `token_use` are present (`verify_token` requires both).

- [ ] **Step 2: Does Alexa accept the prefixed scope names?**

Install the `alexa-ai` CLI per the [MCP Toolkit Quickstart prerequisites](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-quickstart.html), run `alexa-ai configure`, then `alexa-ai new mcp` in a scratch directory (outside the repo). In the generated `addon.json`, set the auth section to the Cognito values (authorization URL `$DOMAIN/oauth2/authorize`, token URL `$DOMAIN/oauth2/token`, client id/secret, scopes `homie/mcp:tools` and `homie/mcp:service`). Record: whether the CLI/console accepts `homie/mcp:tools`, and the **account-linking redirect URIs** it lists.

- [ ] **Step 3: Throwaway MCP endpoint behind a tunnel**

In the scratch directory create `spike.py`:

```python
import json
import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO)
DOMAIN = "https://REPLACE-with-cognito_domain_url"
mcp = MCPServer("homie-spike")


@mcp.tool()
def ping_tool() -> str:
    """Return pong."""
    return "pong"


async def metadata(_: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": DOMAIN,
        "authorization_endpoint": f"{DOMAIN}/oauth2/authorize",
        "token_endpoint": f"{DOMAIN}/oauth2/token",
        "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
        "scopes_supported": ["homie/mcp:tools", "homie/mcp:service"],
        "code_challenge_methods_supported": ["S256"],
    })


mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def log_everything(scope, receive, send):
    if scope["type"] == "http":
        logging.info("%s %s headers=%s", scope["method"], scope["path"],
                     json.dumps({k.decode(): v.decode()[:40] for k, v in scope["headers"]}))
        auth = dict(scope["headers"]).get(b"authorization")
        if scope["path"].startswith("/mcp") and not auth:
            await Response(status_code=401)(scope, receive, send)
            return
    await app(scope, receive, send)


async def lifespan(_):
    async with mcp.session_manager.run():
        yield

app = Starlette(
    routes=[Route("/.well-known/oauth-authorization-server", metadata), Mount("/", mcp_app)],
    lifespan=lifespan,
)
```

Run (two terminals):

```bash
uv run --with "mcp>=2.3" --with uvicorn uvicorn spike:log_everything --port 8000
cloudflared tunnel --url http://localhost:8000
```

Point `addon.json`'s MCP endpoint at `https://<tunnel>/mcp`, `alexa-ai deploy`, link your account in the Alexa app, and ask Alexa to "use homie spike ping tool". Record from the log: which paths Alexa calls, the `mcp-protocol-version` header value, whether the service-token handshake hits `/mcp` and with which methods, and the round-trip latency Alexa reports.

- [ ] **Step 4: Write findings and decide**

Create `docs/superpowers/spikes/2026-10-04-alexa-cognito-auth.md` with sections: *Questions*, *Observations* (paste the recorded values from Steps 1–3), *Decision*. Decision is one of:
- **Cognito-only (expected):** keep scopes `homie/mcp:tools` / `homie/mcp:service`; set `alexa_redirect_uris` in `infra/terraform/terraform.tfvars` (git-ignored) and re-run `terraform apply` to create the `alexa-link` client.
- **Fallback:** Cognito rejects `resource` or Alexa rejects prefixed scopes → add a Plan-1 follow-up task "FastAPI client_credentials token endpoint" (spec §4 fallback) before Plan 4.

- [ ] **Step 5: Commit findings only**

```bash
git add docs/superpowers/spikes/2026-10-04-alexa-cognito-auth.md
git commit -m "docs: record Alexa/Cognito auth spike findings"
```

---

### Task 3: Server foundation — Python 3.14, settings, auth, rate limiting

**Files:**
- Modify: `server/.python-version`, `server/pyproject.toml`, `server/app/config.py`, `server/.env.example`, `infra/docker/server.Dockerfile`
- Create: `server/app/auth.py`, `server/app/ratelimit.py`, `server/tests/conftest.py`, `server/tests/test_auth.py`, `server/tests/test_ratelimit.py`

**Interfaces:**
- Produces:
  - `app.config.settings` with fields `env, aws_region, cors_origins, public_base_url, cognito_user_pool_id, cognito_domain, cognito_client_ids: list[str], mcp_tools_scope, mcp_service_scope, dynamodb_table, media_bucket` and properties `cognito_issuer`, `cognito_jwks_url`.
  - `app.auth.User(sub: str, client_id: str, scopes: frozenset[str])` (frozen dataclass)
  - `app.auth.AuthError(Exception)`
  - `app.auth.bearer_token(headers: Mapping[str, str] | None) -> str` (raises `AuthError`)
  - `app.auth.verify_token(token: str, required_scope: str) -> User` (raises `AuthError`)
  - `app.auth.current_user` — FastAPI dependency → `User`; 401 (no `WWW-Authenticate`) / 429
  - `app.auth.CurrentUser = Annotated[User, Depends(current_user)]`
  - `app.auth.McpAuthGate` — pure ASGI middleware class
  - `app.ratelimit.RateLimiter(limit: int, window_s: float, now=time.monotonic)` with `.hit(key: str) -> bool`; instances `api_limiter` (120/60 s) and `mcp_limiter` (60/60 s)
  - Test fixtures (`tests/conftest.py`): `make_token(**overrides) -> str`, `client` (TestClient), `ddb` (moto table)

- [ ] **Step 1: Move to Python 3.14 and add deps**

```bash
cd server
uv python pin 3.14
sed -i '' 's/requires-python = ">=3.13"/requires-python = ">=3.14"/' pyproject.toml
uv add "pyjwt[crypto]" boto3 "mcp>=2.3"
uv add --dev "moto[dynamodb,s3]"
```

In `infra/docker/server.Dockerfile` change the first line to `FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim`.

Append to `server/pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
filterwarnings = ["ignore::DeprecationWarning:starlette.*"]
```

Run: `uv run python -c "import uuid, sys; print(sys.version_info[:2], uuid.uuid7())"`
Expected: `(3, 14) <a uuid>`

- [ ] **Step 2: Replace `server/app/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    aws_region: str = "us-east-1"
    cors_origins: list[str] = ["http://localhost:3000"]
    public_base_url: str = "http://localhost:8000"

    cognito_user_pool_id: str = ""
    cognito_domain: str = ""  # e.g. https://homie-dev-123.auth.us-east-1.amazoncognito.com
    cognito_client_ids: list[str] = []
    mcp_tools_scope: str = "homie/mcp:tools"
    mcp_service_scope: str = "homie/mcp:service"

    dynamodb_table: str = "homie-dev"
    media_bucket: str = ""

    google_maps_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    @property
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @property
    def cognito_jwks_url(self) -> str:
        return f"{self.cognito_issuer}/.well-known/jwks.json"


settings = Settings()
```

Replace `server/.env.example`:

```dotenv
ENV=dev
AWS_REGION=us-east-1
# AWS credentials come from the default chain, e.g. AWS_PROFILE=homie
CORS_ORIGINS=["http://localhost:3000"]
PUBLIC_BASE_URL=http://localhost:8000

# from `terraform output`
COGNITO_USER_POOL_ID=
COGNITO_DOMAIN=
# JSON list: dashboard_client_id, alexa_service_client_id, alexa_link_client_id
COGNITO_CLIENT_IDS=[]
MCP_TOOLS_SCOPE=homie/mcp:tools
MCP_SERVICE_SCOPE=homie/mcp:service
DYNAMODB_TABLE=homie-dev
MEDIA_BUCKET=

GOOGLE_MAPS_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

- [ ] **Step 3: Write the rate limiter test** — `server/tests/test_ratelimit.py`

```python
from app.ratelimit import RateLimiter


def test_allows_up_to_limit_then_blocks():
    clock = [0.0]
    limiter = RateLimiter(limit=2, window_s=60, now=lambda: clock[0])
    assert limiter.hit("a")
    assert limiter.hit("a")
    assert not limiter.hit("a")


def test_window_slides():
    clock = [0.0]
    limiter = RateLimiter(limit=1, window_s=60, now=lambda: clock[0])
    assert limiter.hit("a")
    clock[0] = 59.9
    assert not limiter.hit("a")
    clock[0] = 60.1
    assert limiter.hit("a")


def test_keys_are_independent():
    limiter = RateLimiter(limit=1, window_s=60, now=lambda: 0.0)
    assert limiter.hit("a")
    assert limiter.hit("b")
```

- [ ] **Step 4: Run it to see it fail**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ratelimit'`

- [ ] **Step 5: Implement `server/app/ratelimit.py`**

```python
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter keyed by user sub."""

    # dev-note: in-memory, correct only for a single server instance;
    # move to DynamoDB counters (or Redis) if we ever run more than one.

    def __init__(self, limit: int, window_s: float, now: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.now = now
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        now = self.now()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


api_limiter = RateLimiter(limit=120, window_s=60)
mcp_limiter = RateLimiter(limit=60, window_s=60)
```

Run: `uv run pytest tests/test_ratelimit.py -v` → Expected: 3 passed.

- [ ] **Step 6: Create shared fixtures** — `server/tests/conftest.py`

```python
import time
import uuid

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from moto import mock_aws

from app import auth, db
from app.config import settings
from app.ratelimit import api_limiter, mcp_limiter

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_ID = "test-dashboard"
TOOLS = "homie/mcp:tools"
SERVICE = "homie/mcp:service"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    # Never touch real AWS from tests.
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "cognito_user_pool_id", "us-east-1_test")
    monkeypatch.setattr(settings, "cognito_client_ids", [CLIENT_ID])
    monkeypatch.setattr(settings, "mcp_tools_scope", TOOLS)
    monkeypatch.setattr(settings, "mcp_service_scope", SERVICE)
    monkeypatch.setattr(settings, "dynamodb_table", "homie-test")
    monkeypatch.setattr(auth, "_signing_key", lambda token: KEY.public_key())
    api_limiter.reset()
    mcp_limiter.reset()


def _make_token(
    sub: str = "user-a",
    scope: str = TOOLS,
    client_id: str = CLIENT_ID,
    token_use: str = "access",
    exp_in: int = 3600,
    issuer: str | None = None,
    key=KEY,
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "client_id": client_id,
        "token_use": token_use,
        "scope": scope,
        "iss": issuer or settings.cognito_issuer,
        "iat": now,
        "exp": now + exp_in,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def make_token():
    return _make_token


@pytest.fixture
def ddb(isolated_env):
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="homie-test",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        db.table.cache_clear()
        yield
        db.table.cache_clear()


@pytest.fixture(scope="session")
def _session_client():
    # One TestClient for the whole run: the MCP session manager's run() may only start once.
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(_session_client, ddb):
    return _session_client
```

Also create the minimal `server/app/db.py` now (Task 5 uses it; the fixture imports it):

```python
from functools import cache

import boto3

from app.config import settings


@cache
def table():
    return boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.dynamodb_table)


def user_pk(sub: str) -> str:
    return f"USER#{sub}"
```

- [ ] **Step 7: Write auth tests** — `server/tests/test_auth.py`

```python
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import AuthError, bearer_token, verify_token
from tests.conftest import SERVICE, TOOLS


def test_valid_token_returns_user(make_token):
    user = verify_token(make_token(sub="abc"), TOOLS)
    assert user.sub == "abc"
    assert TOOLS in user.scopes


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": SERVICE},  # wrong scope
        {"exp_in": -10},  # expired
        {"client_id": "someone-else"},  # unknown app client
        {"token_use": "id"},  # ID token instead of access token
        {"issuer": "https://cognito-idp.us-east-1.amazonaws.com/other-pool"},  # other pool
        {"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)},  # wrong key
    ],
)
def test_rejects_bad_tokens(make_token, overrides):
    with pytest.raises(AuthError):
        verify_token(make_token(**overrides), TOOLS)


def test_rejects_garbage():
    with pytest.raises(AuthError):
        verify_token("not-a-jwt", TOOLS)


@pytest.mark.parametrize("headers", [None, {}, {"authorization": "Basic abc"}, {"Authorization": "Bearer "}])
def test_bearer_token_missing(headers):
    with pytest.raises(AuthError):
        bearer_token(headers)


def test_bearer_token_any_case():
    assert bearer_token({"Authorization": "bearer xyz"}) == "xyz"


def test_api_requires_token(client):
    res = client.get("/api/memories")
    assert res.status_code == 401
    assert "www-authenticate" not in res.headers


def test_mcp_requires_token_without_www_authenticate(client):
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert res.status_code == 401
    assert "www-authenticate" not in res.headers


def test_api_rejects_service_token(client, make_token):
    res = client.get("/api/memories", headers={"Authorization": f"Bearer {make_token(scope=SERVICE)}"})
    assert res.status_code == 401


def test_api_rate_limited(client, make_token, monkeypatch):
    from app.ratelimit import api_limiter

    monkeypatch.setattr(api_limiter, "limit", 1)
    headers = {"Authorization": f"Bearer {make_token()}"}
    assert client.get("/api/memories", headers=headers).status_code == 200
    assert client.get("/api/memories", headers=headers).status_code == 429
```

(The last four tests pass only after Tasks 6–7 add the routes; they are written here so the auth contract lives in one file. Run them again at the end of Task 7.)

- [ ] **Step 8: Run unit tests to see them fail**

Run: `uv run pytest tests/test_auth.py -v -k "not api_ and not mcp_"`
Expected: FAIL — `ImportError: cannot import name 'AuthError' from 'app.auth'` (module missing).

- [ ] **Step 9: Implement `server/app/auth.py`**

```python
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.ratelimit import api_limiter


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class User:
    sub: str
    client_id: str
    scopes: frozenset[str]


@cache
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(settings.cognito_jwks_url, cache_keys=True, lifespan=3600)


def _signing_key(token: str):
    # dev-note: first call per hour fetches JWKS synchronously (~100 ms); fine at our scale.
    return _jwks().get_signing_key_from_jwt(token).key


def bearer_token(headers: Mapping[str, str] | None) -> str:
    value = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("missing bearer token")
    return token.strip()


def verify_token(token: str, required_scope: str) -> User:
    try:
        claims = jwt.decode(
            token,
            _signing_key(token),
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={"require": ["exp", "iss", "sub", "client_id", "token_use"], "verify_aud": False},
        )
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    if claims["token_use"] != "access":
        raise AuthError("not an access token")
    if claims["client_id"] not in settings.cognito_client_ids:
        raise AuthError("unknown client")
    scopes = frozenset(str(claims.get("scope", "")).split())
    if required_scope not in scopes:
        raise AuthError("missing scope")
    return User(sub=claims["sub"], client_id=claims["client_id"], scopes=scopes)


def current_user(authorization: Annotated[str, Header()] = "") -> User:
    try:
        user = verify_token(bearer_token({"authorization": authorization}), settings.mcp_tools_scope)
    except AuthError:
        # No WWW-Authenticate header on purpose (Alexa+ requirement, kept consistent for REST).
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if not api_limiter.hit(user.sub):
        raise HTTPException(status_code=429, detail="Too many requests, try again in a minute.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


class McpAuthGate:
    """Rejects /mcp requests lacking a valid user or service token with a bare 401.

    Tools re-verify the token themselves and require the user scope, so a service
    token can only reach initialize / tools/list.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            try:
                token = bearer_token(headers)
                try:
                    verify_token(token, settings.mcp_tools_scope)
                except AuthError:
                    verify_token(token, settings.mcp_service_scope)
            except AuthError:
                await Response(status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)
```

- [ ] **Step 10: Run unit tests**

Run: `uv run pytest tests/test_auth.py tests/test_ratelimit.py -v -k "not api_ and not mcp_"`
Expected: all selected pass (11 auth cases + 3 limiter).

- [ ] **Step 11: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add .python-version pyproject.toml uv.lock .env.example app/config.py app/auth.py app/ratelimit.py app/db.py tests/conftest.py tests/test_auth.py tests/test_ratelimit.py ../infra/docker/server.Dockerfile
git commit -m "feat(server): add Cognito token verification and per-user rate limiting"
```

---

### Task 4: OAuth authorization-server metadata endpoint

**Files:**
- Create: `server/app/routes/wellknown.py`, `server/tests/test_wellknown.py`
- Modify: `server/app/main.py`

**Interfaces:**
- Consumes: `settings.cognito_domain`, `settings.cognito_issuer`, scope settings.
- Produces: `GET /.well-known/oauth-authorization-server` (no auth).

- [ ] **Step 1: Write the test** — `server/tests/test_wellknown.py`

```python
from app.config import settings


def test_metadata_points_at_cognito(client, monkeypatch):
    monkeypatch.setattr(settings, "cognito_domain", "https://homie.auth.example.com")
    res = client.get("/.well-known/oauth-authorization-server")
    assert res.status_code == 200
    body = res.json()
    assert body["token_endpoint"] == "https://homie.auth.example.com/oauth2/token"
    assert body["authorization_endpoint"] == "https://homie.auth.example.com/oauth2/authorize"
    assert "client_credentials" in body["grant_types_supported"]
    assert set(body["scopes_supported"]) == {settings.mcp_tools_scope, settings.mcp_service_scope}
    assert body["code_challenge_methods_supported"] == ["S256"]
```

- [ ] **Step 2: Run to see it fail**

Run: `uv run pytest tests/test_wellknown.py -v`
Expected: FAIL — 404 (route not registered; note `client` fixture needs `app.main` to import cleanly, which it does from the scaffold).

- [ ] **Step 3: Implement `server/app/routes/wellknown.py`**

```python
from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["oauth"])


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata() -> dict:
    return {
        "issuer": settings.cognito_issuer,
        "authorization_endpoint": f"{settings.cognito_domain}/oauth2/authorize",
        "token_endpoint": f"{settings.cognito_domain}/oauth2/token",
        "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        "scopes_supported": [settings.mcp_tools_scope, settings.mcp_service_scope],
    }
```

In `server/app/main.py` change the import and registration:

```python
from app.routes import health, wellknown
...
app.include_router(health.router)
app.include_router(wellknown.router)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_wellknown.py -v` → Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routes/wellknown.py app/main.py tests/test_wellknown.py
git commit -m "feat(server): serve OAuth authorization-server metadata for Alexa"
```

---

### Task 5: Memory model + service (DynamoDB, recall ranking)

**Files:**
- Create: `server/app/models/memory.py`, `server/app/services/memory.py`, `server/tests/test_memory_service.py`

**Interfaces:**
- Consumes: `app.db.table()`, `app.db.user_pk(sub)`.
- Produces:
  - `app.models.memory.Kind = Literal["item_location", "fact", "preference", "todo"]`
  - `MemoryCreate(kind, content, tags=[], event_id=None)`, `MemoryUpdate(kind?, content?, tags?, event_id?)`, `Memory(MemoryCreate + id: UUID, media_ids: list[str], created_at: datetime, updated_at: datetime)`
  - `app.services.memory.create_memory(sub, data: MemoryCreate) -> Memory`
  - `list_memories(sub, kind: Kind | None = None, query: str | None = None, limit: int = 50) -> list[Memory]` (newest first; with `query`, only matches, best first)
  - `recall(sub, query: str, kind: Kind | None = None, limit: int = 10) -> list[Memory]` (falls back to newest when nothing matches)
  - `get_memory(sub, memory_id: UUID) -> Memory | None`
  - `update_memory(sub, memory_id: UUID, data: MemoryUpdate) -> Memory | None` (None if missing; never creates)
  - `delete_memory(sub, memory_id: UUID) -> bool`

- [ ] **Step 1: Write the tests** — `server/tests/test_memory_service.py`

```python
import uuid

import pytest
from pydantic import ValidationError

from app.models.memory import MemoryCreate, MemoryUpdate
from app.services import memory


def add(sub, content, kind="fact", tags=()):
    return memory.create_memory(sub, MemoryCreate(kind=kind, content=content, tags=list(tags)))


def test_create_and_get(ddb):
    m = add("a", "  passport is in the top desk drawer  ", "item_location", ["Travel", "travel"])
    assert m.content == "passport is in the top desk drawer"
    assert m.tags == ["travel"]
    assert memory.get_memory("a", m.id) == m


def test_list_newest_first_and_kind_filter(ddb):
    first = add("a", "likes fried chicken", "preference")
    second = add("a", "keys on the hook", "item_location")
    assert [m.id for m in memory.list_memories("a")] == [second.id, first.id]
    assert [m.id for m in memory.list_memories("a", kind="preference")] == [first.id]


def test_users_are_isolated(ddb):
    m = add("a", "secret diary under the bed")
    assert memory.list_memories("b") == []
    assert memory.get_memory("b", m.id) is None
    assert memory.update_memory("b", m.id, MemoryUpdate(content="hacked")) is None
    assert memory.delete_memory("b", m.id) is False
    assert memory.get_memory("a", m.id).content == "secret diary under the bed"


def test_recall_ranks_by_keyword_overlap(ddb):
    add("a", "car keys are on the hook by the door", "item_location")
    target = add("a", "passport is in the top desk drawer", "item_location")
    assert memory.recall("a", "Where is my passport?")[0].id == target.id


def test_recall_matches_plurals(ddb):
    target = add("a", "spare key is under the mat", "item_location")
    add("a", "likes ramen", "preference")
    assert memory.recall("a", "where are my keys")[0].id == target.id


def test_recall_vague_query_falls_back_to_recent(ddb):
    add("a", "milk expires friday", "fact")
    newest = add("a", "umbrella in the car trunk", "item_location")
    results = memory.recall("a", "where is it?")
    assert results and results[0].id == newest.id


def test_list_with_query_only_returns_matches(ddb):
    add("a", "passport in drawer")
    assert memory.list_memories("a", query="bicycle") == []


def test_update_partial_and_clear_event(ddb):
    m = memory.create_memory("a", MemoryCreate(kind="todo", content="bring charger", event_id="evt1"))
    updated = memory.update_memory("a", m.id, MemoryUpdate(event_id=None))
    assert updated.event_id is None
    assert updated.content == "bring charger"
    assert updated.updated_at >= m.updated_at


def test_update_or_delete_missing_does_not_create(ddb):
    ghost = uuid.uuid7()
    assert memory.update_memory("a", ghost, MemoryUpdate(content="x")) is None
    assert memory.delete_memory("a", ghost) is False
    assert memory.list_memories("a") == []


def test_delete(ddb):
    m = add("a", "temp")
    assert memory.delete_memory("a", m.id) is True
    assert memory.get_memory("a", m.id) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": "   "},
        {"content": "x" * 2001},
        {"content": "ok", "tags": [f"t{i}" for i in range(11)]},
        {"content": "ok", "tags": ["x" * 41]},
        {"content": "ok", "kind": "secret"},
    ],
)
def test_invalid_input_rejected(kwargs):
    with pytest.raises(ValidationError):
        MemoryCreate(**{"kind": "fact", **kwargs})
```

- [ ] **Step 2: Run to see failures**

Run: `uv run pytest tests/test_memory_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.memory'`

- [ ] **Step 3: Implement `server/app/models/memory.py`**

```python
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

Kind = Literal["item_location", "fact", "preference", "todo"]
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Tag = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=40)
]
Tags = Annotated[list[Tag], Field(max_length=10), AfterValidator(lambda t: list(dict.fromkeys(t)))]
EventId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class MemoryCreate(BaseModel):
    kind: Kind
    content: Content
    tags: Tags = []
    event_id: EventId | None = None


class MemoryUpdate(BaseModel):
    kind: Kind | None = None
    content: Content | None = None
    tags: Tags | None = None
    event_id: EventId | None = None  # explicit null clears the event link


class Memory(MemoryCreate):
    id: UUID
    media_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Implement `server/app/services/memory.py`**

```python
import re
from datetime import UTC, datetime
from uuid import UUID, uuid7

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.db import table, user_pk
from app.models.memory import Kind, Memory, MemoryCreate, MemoryUpdate

_PREFIX = "MEM#"
# dev-note: list/recall load all of a user's memories (capped) and filter in Python.
# Fine for a personal memory store; add a search index past a few thousand items per user.
_MAX_ITEMS = 2000
_STOPWORDS = frozenset(
    "a an and are at did do does for i in is it its me my of on or put the this to was"
    " were what when where which who why you your left keep kept".split()
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sk(memory_id: UUID) -> str:
    return f"{_PREFIX}{memory_id}"


def _key(sub: str, memory_id: UUID) -> dict:
    return {"pk": user_pk(sub), "sk": _sk(memory_id)}


def _to_memory(item: dict) -> Memory:
    return Memory(
        id=item["sk"].removeprefix(_PREFIX),
        kind=item["kind"],
        content=item["content"],
        tags=item.get("tags", []),
        event_id=item.get("event_id"),
        media_ids=item.get("media_ids", []),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w.removesuffix("s") if len(w) > 3 else w for w in words if w not in _STOPWORDS}


def _matches(memories: list[Memory], query: str) -> list[Memory]:
    # dev-note: keyword-overlap count; swap for BM25 or embeddings when recall gets noisy.
    wanted = _tokens(query)
    scored = []
    for m in memories:
        score = len(wanted & _tokens(f"{m.content} {' '.join(m.tags)}"))
        if score:
            scored.append((score, m))
    scored.sort(key=lambda pair: pair[0], reverse=True)  # stable: newest first among ties
    return [m for _, m in scored]


def _all(sub: str) -> list[Memory]:
    kwargs = {
        "KeyConditionExpression": Key("pk").eq(user_pk(sub)) & Key("sk").begins_with(_PREFIX),
        "ScanIndexForward": False,  # uuid7 ids → newest first
    }
    items: list[dict] = []
    while True:
        page = table().query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page or len(items) >= _MAX_ITEMS:
            return [_to_memory(i) for i in items[:_MAX_ITEMS]]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def create_memory(sub: str, data: MemoryCreate) -> Memory:
    now = _now()
    item = {
        **_key(sub, uuid7()),
        **data.model_dump(exclude_none=True),
        "media_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    table().put_item(Item=item)
    return _to_memory(item)


def list_memories(
    sub: str, kind: Kind | None = None, query: str | None = None, limit: int = 50
) -> list[Memory]:
    memories = _all(sub)
    if kind:
        memories = [m for m in memories if m.kind == kind]
    if query:
        memories = _matches(memories, query)
    return memories[:limit]


def recall(sub: str, query: str, kind: Kind | None = None, limit: int = 10) -> list[Memory]:
    memories = list_memories(sub, kind=kind, limit=_MAX_ITEMS)
    return (_matches(memories, query) or memories)[:limit]


def get_memory(sub: str, memory_id: UUID) -> Memory | None:
    item = table().get_item(Key=_key(sub, memory_id)).get("Item")
    return _to_memory(item) if item else None


def update_memory(sub: str, memory_id: UUID, data: MemoryUpdate) -> Memory | None:
    changes = data.model_dump(exclude_unset=True)
    sets = {k: v for k, v in changes.items() if v is not None}
    removes = ["event_id"] if "event_id" in changes and changes["event_id"] is None else []
    sets["updated_at"] = _now()
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in sets)
    if removes:
        expression += " REMOVE " + ", ".join(f"#{k}" for k in removes)
    try:
        res = table().update_item(
            Key=_key(sub, memory_id),
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames={f"#{k}": k for k in [*sets, *removes]},
            ExpressionAttributeValues={f":{k}": v for k, v in sets.items()},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return _to_memory(res["Attributes"])


def delete_memory(sub: str, memory_id: UUID) -> bool:
    res = table().delete_item(Key=_key(sub, memory_id), ReturnValues="ALL_OLD")
    return "Attributes" in res
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_memory_service.py -v`
Expected: all pass (11 tests incl. 5 parametrized invalid inputs).

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run ruff format .
git add app/models/memory.py app/services/memory.py tests/test_memory_service.py
git commit -m "feat(server): add DynamoDB memory service with keyword recall"
```

---

### Task 6: Memory REST API

**Files:**
- Create: `server/app/routes/memories.py`, `server/tests/test_memories_api.py`
- Modify: `server/app/main.py`

**Interfaces:**
- Consumes: `CurrentUser`, memory service functions, memory models.
- Produces: `GET /api/memories?kind=&q=&limit=` → `list[Memory]`; `POST /api/memories` (201) → `Memory`; `PATCH /api/memories/{memory_id}` → `Memory` | 404; `DELETE /api/memories/{memory_id}` → 204 | 404. OpenAPI schema names `Memory`, `MemoryCreate`, `MemoryUpdate` (consumed by client typegen in Task 9).

- [ ] **Step 1: Write the tests** — `server/tests/test_memories_api.py`

```python
def auth(make_token, sub="user-a"):
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


def test_crud_flow(client, make_token):
    h = auth(make_token)
    created = client.post("/api/memories", headers=h, json={"kind": "item_location", "content": "passport in drawer", "tags": ["Travel"]})
    assert created.status_code == 201
    mid = created.json()["id"]
    assert created.json()["tags"] == ["travel"]

    listed = client.get("/api/memories", headers=h).json()
    assert [m["id"] for m in listed] == [mid]

    patched = client.patch(f"/api/memories/{mid}", headers=h, json={"content": "passport in safe"})
    assert patched.status_code == 200 and patched.json()["content"] == "passport in safe"

    assert client.delete(f"/api/memories/{mid}", headers=h).status_code == 204
    assert client.get("/api/memories", headers=h).json() == []


def test_search_and_kind_filter(client, make_token):
    h = auth(make_token)
    client.post("/api/memories", headers=h, json={"kind": "preference", "content": "loves fried chicken"})
    client.post("/api/memories", headers=h, json={"kind": "item_location", "content": "keys on hook"})
    assert len(client.get("/api/memories?kind=preference", headers=h).json()) == 1
    assert client.get("/api/memories?q=chicken", headers=h).json()[0]["content"] == "loves fried chicken"


def test_other_user_cannot_touch(client, make_token):
    mid = client.post("/api/memories", headers=auth(make_token), json={"kind": "fact", "content": "mine"}).json()["id"]
    other = auth(make_token, sub="user-b")
    assert client.get("/api/memories", headers=other).json() == []
    assert client.patch(f"/api/memories/{mid}", headers=other, json={"content": "x"}).status_code == 404
    assert client.delete(f"/api/memories/{mid}", headers=other).status_code == 404
    assert client.get("/api/memories", headers=auth(make_token)).json()[0]["content"] == "mine"


def test_patch_deleted_memory_is_404_and_not_recreated(client, make_token):
    h = auth(make_token)
    mid = client.post("/api/memories", headers=h, json={"kind": "fact", "content": "temp"}).json()["id"]
    client.delete(f"/api/memories/{mid}", headers=h)
    assert client.patch(f"/api/memories/{mid}", headers=h, json={"content": "back?"}).status_code == 404
    assert client.get("/api/memories", headers=h).json() == []


def test_validation(client, make_token):
    h = auth(make_token)
    assert client.post("/api/memories", headers=h, json={"kind": "fact", "content": "   "}).status_code == 422
    assert client.post("/api/memories", headers=h, json={"kind": "fact", "content": "x" * 2001}).status_code == 422
    assert client.get("/api/memories?limit=500", headers=h).status_code == 422
    assert client.patch("/api/memories/not-a-uuid", headers=h, json={}).status_code == 422


def test_user_id_in_body_is_ignored(client, make_token):
    h = auth(make_token, sub="user-a")
    client.post("/api/memories", headers=h, json={"kind": "fact", "content": "x", "sub": "user-b", "pk": "USER#user-b"})
    assert client.get("/api/memories", headers=auth(make_token, sub="user-b")).json() == []
```

- [ ] **Step 2: Run to see failures**

Run: `uv run pytest tests/test_memories_api.py -v`
Expected: FAIL — 404s (routes missing).

- [ ] **Step 3: Implement `server/app/routes/memories.py`**

```python
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response

from app.auth import CurrentUser
from app.models.memory import Kind, Memory, MemoryCreate, MemoryUpdate
from app.services import memory

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("")
def list_memories(
    user: CurrentUser,
    kind: Kind | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Memory]:
    return memory.list_memories(user.sub, kind=kind, query=q, limit=limit)


@router.post("", status_code=201)
def create_memory(user: CurrentUser, body: MemoryCreate) -> Memory:
    return memory.create_memory(user.sub, body)


@router.patch("/{memory_id}")
def update_memory(user: CurrentUser, memory_id: UUID, body: MemoryUpdate) -> Memory:
    updated = memory.update_memory(user.sub, memory_id, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return updated


@router.delete("/{memory_id}", status_code=204)
def delete_memory(user: CurrentUser, memory_id: UUID) -> Response:
    if not memory.delete_memory(user.sub, memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return Response(status_code=204)
```

Register in `server/app/main.py`: `from app.routes import health, memories, wellknown` and `app.include_router(memories.router)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_memories_api.py tests/test_auth.py -v -k "not mcp_"`
Expected: all pass (including `test_api_requires_token`, `test_api_rate_limited`).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format .
git add app/routes/memories.py app/main.py tests/test_memories_api.py
git commit -m "feat(server): add memory REST API"
```

---

### Task 7: MCP endpoint with remember / recall / forget

**Files:**
- Create: `server/app/mcp_server.py`, `server/tests/test_mcp.py`
- Modify: `server/app/main.py`

**Interfaces:**
- Consumes: `bearer_token`, `verify_token`, `AuthError`, `McpAuthGate`, `mcp_limiter`, memory service + models.
- Produces: `app.mcp_server.mcp` (`MCPServer`), tools `remember(content, kind="fact", tags=None, event_id=None) -> Memory`, `recall(query, kind=None) -> list[Memory]`, `forget(memory_id: str) -> str`; HTTP endpoint `POST /mcp` (stateless, JSON responses). Plan 2 adds Google tools to the same `mcp` object.

- [ ] **Step 1: Write the tests** — `server/tests/test_mcp.py`

```python
from tests.conftest import SERVICE

MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-11-25",
}


def rpc(client, token, method, params=None):
    return client.post(
        "/mcp",
        headers={**MCP_HEADERS, "authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def call(client, token, name, arguments):
    res = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert res.status_code == 200, res.text
    return res.json()["result"]


def error_text(result):
    assert result.get("isError") is True
    return result["content"][0]["text"]


def test_initialize_negotiates_2025_11_25(client, make_token):
    res = rpc(client, make_token(), "initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert res.status_code == 200, res.text
    assert res.json()["result"]["protocolVersion"] == "2025-11-25"


def test_tools_listed(client, make_token):
    tools = {t["name"] for t in rpc(client, make_token(), "tools/list").json()["result"]["tools"]}
    assert {"remember", "recall", "forget"} <= tools


def test_remember_then_recall(client, make_token):
    token = make_token()
    saved = call(client, token, "remember", {"content": "passport is in the top desk drawer", "kind": "item_location"})
    assert saved["structuredContent"]["content"] == "passport is in the top desk drawer"
    call(client, token, "remember", {"content": "keys on the hook", "kind": "item_location"})
    found = call(client, token, "recall", {"query": "where's my passport"})
    assert found["structuredContent"]["result"][0]["content"] == "passport is in the top desk drawer"


def test_forget(client, make_token):
    token = make_token()
    mid = call(client, token, "remember", {"content": "temp"})["structuredContent"]["id"]
    call(client, token, "forget", {"memory_id": mid})
    assert call(client, token, "recall", {"query": "temp"})["structuredContent"]["result"] == []


def test_forget_bad_id_is_speakable(client, make_token):
    assert "couldn't find" in error_text(call(client, make_token(), "forget", {"memory_id": "the passport one"}))


def test_remember_blank_is_speakable(client, make_token):
    assert "couldn't save" in error_text(call(client, make_token(), "remember", {"content": "   "}))


def test_users_isolated_over_mcp(client, make_token):
    call(client, make_token(sub="user-a"), "remember", {"content": "a's secret"})
    assert call(client, make_token(sub="user-b"), "recall", {"query": "secret"})["structuredContent"]["result"] == []


def test_service_token_can_list_but_not_call(client, make_token):
    service = make_token(sub="svc", scope=SERVICE)
    assert rpc(client, service, "tools/list").status_code == 200
    assert "link your homie account" in error_text(call(client, service, "recall", {"query": "x"}))


def test_mcp_rate_limit_is_speakable(client, make_token, monkeypatch):
    from app.ratelimit import mcp_limiter

    monkeypatch.setattr(mcp_limiter, "limit", 1)
    token = make_token()
    call(client, token, "recall", {"query": "x"})
    assert "a bit fast" in error_text(call(client, token, "recall", {"query": "x"}))
```

- [ ] **Step 2: Run to see failures**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: FAIL — `/mcp` returns 404.

- [ ] **Step 3: Implement `server/app/mcp_server.py`**

```python
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from app.auth import AuthError, User, bearer_token, verify_token
from app.config import settings
from app.models.memory import Kind, Memory, MemoryCreate
from app.ratelimit import mcp_limiter
from app.services import memory

mcp = MCPServer(
    "homie",
    instructions=(
        "homie is the user's personal memory. Save things they ask you to remember and look them"
        " up when they ask where something is or what they told you before."
    ),
)


def _user(ctx: Context) -> User:
    try:
        user = verify_token(bearer_token(ctx.headers), settings.mcp_tools_scope)
    except AuthError:
        raise ToolError("Please link your homie account in the Alexa app first.") from None
    if not mcp_limiter.hit(user.sub):
        raise ToolError("You're going a bit fast. Try again in a minute.")
    return user


# dev-note: tools are sync and call boto3 (~10 ms) on the event loop; switch to
# anyio.to_thread if concurrent load ever makes that visible in latency.


@mcp.tool()
def remember(
    ctx: Context,
    content: str,
    kind: Kind = "fact",
    tags: list[str] | None = None,
    event_id: str | None = None,
) -> Memory:
    """Save something the user wants you to remember.

    Use kind="item_location" when they say where they put something ("my passport is in the top
    desk drawer"), "preference" for likes and dislikes ("I love fried chicken", "I don't eat
    pork"), "todo" for tasks, and "fact" for anything else. Store it in the user's own words.
    Pass event_id only to attach a to-do to a calendar event.
    """
    user = _user(ctx)
    try:
        data = MemoryCreate(kind=kind, content=content, tags=tags or [], event_id=event_id)
    except ValidationError:
        raise ToolError(
            "I couldn't save that. Please say it again in a sentence or two, with at most 10 tags."
        ) from None
    return memory.create_memory(user.sub, data)


@mcp.tool()
def recall(ctx: Context, query: str, kind: Kind | None = None) -> list[Memory]:
    """Look up what the user told you before.

    Use when the user asks where something is ("where's my passport?"), what they like, or what
    they need to do. Pass their question as query. Returns up to 10 memories, best match first;
    if nothing matches it returns their most recent memories so you can still answer.
    """
    user = _user(ctx)
    return memory.recall(user.sub, query[:200], kind=kind)


@mcp.tool()
def forget(ctx: Context, memory_id: str) -> str:
    """Delete one memory the user no longer wants kept.

    First call recall to find the memory, then pass its id. Confirm with the user before
    forgetting if more than one memory could match.
    """
    user = _user(ctx)
    try:
        mid = UUID(memory_id)
    except ValueError:
        raise ToolError("I couldn't find that memory. Ask me to recall it first.") from None
    if not memory.delete_memory(user.sub, mid):
        raise ToolError("I couldn't find that memory. It may already be forgotten.")
    return "Done, I've forgotten it."
```

- [ ] **Step 4: Mount it — replace `server/app/main.py`**

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings

from app.auth import McpAuthGate
from app.config import settings
from app.mcp_server import mcp
from app.routes import health, memories, wellknown

# dev-note: DNS-rebinding protection off — it guards unauthenticated localhost servers;
# every /mcp request here must carry a verified Cognito token (McpAuthGate).
mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Homie API", lifespan=lifespan)

app.add_middleware(McpAuthGate)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(wellknown.router)
app.include_router(memories.router)
# Register every router above this line: the MCP app is mounted at "/" and catches the rest.
# The endpoint is still /mcp: that is the SDK's default streamable_http_path (Mount("/mcp") would give /mcp/mcp).
app.mount("/", mcp_app)
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all pass. If `test_initialize_negotiates_2025_11_25` fails because the installed SDK negotiates an older version, run `uv add "mcp>=<newer>"`; if no release supports 2025-11-25, stop and raise it with the team (it's a hard Alexa requirement).

- [ ] **Step 6: Manual smoke test with MCP Inspector**

```bash
uv run fastapi dev app/main.py   # needs a real server/.env from Task 1 outputs
npx @modelcontextprotocol/inspector
```

In Inspector: transport *Streamable HTTP*, URL `http://localhost:8000/mcp`, header `Authorization: Bearer <dashboard access token>` (get one after Task 9 from the browser, or skip until Task 11). Expected: tools list shows remember/recall/forget.

- [ ] **Step 7: Commit**

```bash
uv run ruff check . && uv run ruff format .
git add app/mcp_server.py app/main.py tests/test_mcp.py
git commit -m "feat(server): add MCP endpoint with remember, recall and forget tools"
```

---

### Task 8: CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `client/package.json` (add `typecheck` script)

- [ ] **Step 1: Add the typecheck script**

Run: `cd client && npm pkg set scripts.typecheck="tsc --noEmit"`

- [ ] **Step 2: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  server:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: server
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest

  client:
    runs-on: ubuntu-latest
    env:
      HUSKY: "0"
    defaults:
      run:
        working-directory: client
    steps:
      - uses: actions/checkout@v7
      - uses: oven-sh/setup-bun@v2
      - run: bun install --frozen-lockfile
      - run: bun run lint
      - run: bun run typecheck
      - run: bun run format:check
      - run: bun run build

  terraform:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: infra/terraform
    steps:
      - uses: actions/checkout@v7
      - uses: hashicorp/setup-terraform@v4
      - run: terraform fmt -check
      - run: terraform init -backend=false
      - run: terraform validate
```

- [ ] **Step 3: Verify locally what CI runs**

Run: `cd server && uv run ruff check . && uv run ruff format --check . && uv run pytest && cd ../client && bun run lint && bun run typecheck && bun run format:check && bun run build`
Expected: all succeed.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml client/package.json
git commit -m "ci: run server, client and terraform checks on PRs"
```

---

### Task 9: Client foundation — auth, providers, typed API, app shell

**Files:**
- Modify: `client/.gitignore`, `client/package.json`, `client/src/app/layout.tsx`, `server/.gitignore`
- Create: `client/.env.example`, `server/scripts/export_openapi.py`, `client/src/lib/env.ts`, `client/src/lib/api/client.ts`, `client/src/lib/api/schema.d.ts` (generated), `client/src/components/providers.tsx`, `client/src/components/require-auth.tsx`, `client/src/components/app-shell.tsx`, `client/src/app/auth/callback/page.tsx`, `client/src/app/(app)/layout.tsx`
- Replace: `client/src/app/page.tsx`

**Interfaces:**
- Consumes: OpenAPI from Task 6; Terraform outputs `cognito_issuer`, `dashboard_client_id`, `cognito_domain_url`, `mcp_tools_scope`.
- Produces: `useApi()` → typed `openapi-fetch` client (`paths` from `schema.d.ts`) that adds the bearer token and redirects to sign-in on 401; `env` object; `<RequireAuth>`, `<AppShell>`; route group `(app)` for signed-in pages.

- [ ] **Step 1: Dependencies and shadcn components**

```bash
cd client
bun add react-oidc-context oidc-client-ts @tanstack/react-query openapi-fetch
bun add -d openapi-typescript
bunx --bun shadcn@latest add input textarea label badge skeleton tabs dialog alert-dialog sonner -y
```

Note: this project's shadcn style is **base-nova** (Base UI). Components compose with a `render` prop (`<DialogTrigger render={<Button />} />`) instead of Radix's `asChild`. Check generated files in `src/components/ui/` for exact prop names before using them.

- [ ] **Step 2: OpenAPI export + type generation**

Create `server/scripts/export_openapi.py`:

```python
import json
from pathlib import Path

from app.main import app

Path("openapi.json").write_text(json.dumps(app.openapi(), indent=2))
```

Append `openapi.json` to `server/.gitignore`. Add the client script:

```bash
npm pkg set scripts.gen:api="(cd ../server && uv run python scripts/export_openapi.py) && openapi-typescript ../server/openapi.json -o src/lib/api/schema.d.ts"
bun run gen:api
```

Expected: `src/lib/api/schema.d.ts` created containing `"/api/memories"` and `Memory`.

- [ ] **Step 3: Env files**

Append to `client/.gitignore` right after the `.env*` line: `!.env.example`

Create `client/.env.example`:

```dotenv
NEXT_PUBLIC_API_URL=http://localhost:8000
# terraform output cognito_issuer
NEXT_PUBLIC_COGNITO_AUTHORITY=
# terraform output dashboard_client_id
NEXT_PUBLIC_COGNITO_CLIENT_ID=
# terraform output cognito_domain_url
NEXT_PUBLIC_COGNITO_DOMAIN=
NEXT_PUBLIC_COGNITO_SCOPE=openid email profile homie/mcp:tools
```

Copy to `client/.env.local` and fill it in (do not commit).

Create `client/src/lib/env.ts`:

```ts
// NEXT_PUBLIC_* must be referenced literally so Next can inline them at build time.
export const env = {
  apiUrl: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  cognitoAuthority: process.env.NEXT_PUBLIC_COGNITO_AUTHORITY ?? "",
  cognitoClientId: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID ?? "",
  cognitoDomain: process.env.NEXT_PUBLIC_COGNITO_DOMAIN ?? "",
  cognitoScope:
    process.env.NEXT_PUBLIC_COGNITO_SCOPE ?? "openid email profile homie/mcp:tools",
}
```

- [ ] **Step 4: Typed API client** — `client/src/lib/api/client.ts`

```ts
"use client"

import createClient from "openapi-fetch"
import { useMemo } from "react"
import { useAuth } from "react-oidc-context"

import { env } from "@/lib/env"

import type { paths } from "./schema"

// dev-note: one redirect per page load; concurrent 401s would otherwise each start one.
let redirecting = false

export function useApi() {
  const auth = useAuth()
  const token = auth.user?.access_token
  const signIn = auth.signinRedirect

  return useMemo(() => {
    const client = createClient<paths>({ baseUrl: env.apiUrl })
    client.use({
      onRequest({ request }) {
        if (token) request.headers.set("Authorization", `Bearer ${token}`)
        return request
      },
      onResponse({ response }) {
        // Expired or revoked session: send the user back through sign-in.
        if (response.status === 401 && !redirecting) {
          redirecting = true
          void signIn()
        }
        return response
      },
    })
    return client
  }, [token, signIn])
}
```

- [ ] **Step 5: Providers** — `client/src/components/providers.tsx`

```tsx
"use client"

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useRouter } from "next/navigation"
import { WebStorageStateStore } from "oidc-client-ts"
import { useState } from "react"
import { AuthProvider } from "react-oidc-context"

import { Toaster } from "@/components/ui/sonner"
import { env } from "@/lib/env"

export function Providers({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
      }),
  )
  const isBrowser = typeof window !== "undefined"

  return (
    <AuthProvider
      authority={env.cognitoAuthority}
      client_id={env.cognitoClientId}
      redirect_uri={isBrowser ? `${window.location.origin}/auth/callback` : ""}
      scope={env.cognitoScope}
      userStore={
        isBrowser
          ? new WebStorageStateStore({ store: window.localStorage })
          : undefined
      }
      onSigninCallback={() => router.replace("/memories")}
    >
      <QueryClientProvider client={queryClient}>
        {children}
        <Toaster richColors position="top-center" />
      </QueryClientProvider>
    </AuthProvider>
  )
}
```

If `bun run build` later fails prerendering with `window is not defined` / `localStorage is not defined`, wrap the export: in `layout.tsx` import `Providers` through a tiny client file using `next/dynamic(() => import("@/components/providers").then(m => m.Providers), { ssr: false })`.

- [ ] **Step 6: Root layout** — modify `client/src/app/layout.tsx`

Change `metadata` to `{ title: "homie", description: "See and control what your Alexa remembers." }`, add `import { Providers } from "@/components/providers"`, and change the body to:

```tsx
<body className="flex min-h-full flex-col">
  <Providers>{children}</Providers>
</body>
```

- [ ] **Step 7: Landing + callback pages**

Replace `client/src/app/page.tsx`:

```tsx
"use client"

import { useRouter } from "next/navigation"
import { useEffect } from "react"
import { useAuth } from "react-oidc-context"

import { Button } from "@/components/ui/button"

export default function Home() {
  const auth = useAuth()
  const router = useRouter()

  useEffect(() => {
    if (auth.isAuthenticated) router.replace("/memories")
  }, [auth.isAuthenticated, router])

  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-6 p-6 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">homie</h1>
      <p className="text-muted-foreground max-w-md text-balance">
        Your Alexa remembers where things are, what you like, and what&apos;s
        coming up. See and control everything it knows, here.
      </p>
      <Button
        size="lg"
        className="min-h-11 px-8"
        disabled={auth.isLoading}
        onClick={() => void auth.signinRedirect()}
      >
        Sign in
      </Button>
    </main>
  )
}
```

Create `client/src/app/auth/callback/page.tsx`:

```tsx
"use client"

import Link from "next/link"
import { useAuth } from "react-oidc-context"

export default function AuthCallback() {
  const auth = useAuth()

  if (auth.error) {
    return (
      <main className="p-6" role="alert">
        <p>Sign-in didn&apos;t work: {auth.error.message}</p>
        <Link href="/" className="underline">
          Try again
        </Link>
      </main>
    )
  }
  return (
    <main className="text-muted-foreground p-6" aria-live="polite">
      Signing you in…
    </main>
  )
}
```

- [ ] **Step 8: Auth guard** — `client/src/components/require-auth.tsx`

```tsx
"use client"

import { useAuth } from "react-oidc-context"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const auth = useAuth()

  if (auth.isLoading) {
    return (
      <div className="space-y-3 p-6" aria-busy="true">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  if (!auth.isAuthenticated) {
    return (
      <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
        <p>
          {auth.error
            ? "Your session ended. Please sign in again."
            : "Please sign in to see your homie data."}
        </p>
        <Button className="min-h-11" onClick={() => void auth.signinRedirect()}>
          Sign in
        </Button>
      </main>
    )
  }
  return children
}
```

- [ ] **Step 9: App shell** — `client/src/components/app-shell.tsx`

```tsx
"use client"

import { Brain, LogOut } from "lucide-react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { useAuth } from "react-oidc-context"

import { env } from "@/lib/env"
import { cn } from "@/lib/utils"

// Plans 2–3 add Calendar, Media, Simulator and Settings here.
const NAV = [{ href: "/memories", label: "Memories", icon: Brain }]

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const auth = useAuth()

  async function signOut() {
    await auth.removeUser()
    const params = new URLSearchParams({
      client_id: env.cognitoClientId,
      logout_uri: window.location.origin,
    })
    window.location.href = `${env.cognitoDomain}/logout?${params}`
  }

  const links = NAV.map(({ href, label, icon: Icon }) => {
    const active = pathname.startsWith(href)
    return { href, label, Icon, active }
  })

  return (
    <div className="flex min-h-dvh">
      <aside className="hidden w-56 shrink-0 flex-col border-r md:flex">
        <div className="px-5 py-5 text-lg font-semibold">homie</div>
        <nav aria-label="Main" className="flex flex-col gap-1 px-2">
          {links.map(({ href, label, Icon, active }) => (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex min-h-11 items-center gap-3 rounded-md px-3 text-sm",
                active
                  ? "bg-muted font-medium"
                  : "text-muted-foreground hover:bg-muted",
              )}
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </Link>
          ))}
        </nav>
        <button
          onClick={signOut}
          className="text-muted-foreground hover:bg-muted mx-2 mt-auto mb-4 flex min-h-11 items-center gap-3 rounded-md px-3 text-sm"
        >
          <LogOut className="size-4" aria-hidden />
          Sign out
        </button>
      </aside>

      <main className="min-w-0 flex-1 pb-20 md:pb-0">{children}</main>

      <nav
        aria-label="Main"
        className="bg-background fixed inset-x-0 bottom-0 z-10 flex border-t md:hidden"
      >
        {links.map(({ href, label, Icon, active }) => (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-14 flex-1 flex-col items-center justify-center gap-1 text-xs",
              active ? "text-foreground font-medium" : "text-muted-foreground",
            )}
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </Link>
        ))}
        <button
          onClick={signOut}
          className="text-muted-foreground flex min-h-14 flex-1 flex-col items-center justify-center gap-1 text-xs"
        >
          <LogOut className="size-5" aria-hidden />
          Sign out
        </button>
      </nav>
    </div>
  )
}
```

Create `client/src/app/(app)/layout.tsx`:

```tsx
import { AppShell } from "@/components/app-shell"
import { RequireAuth } from "@/components/require-auth"

export default function AppLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  )
}
```

- [ ] **Step 10: Verify build**

Run: `bun run lint && bun run typecheck && bun run format && bun run build`
Expected: build succeeds (apply the Step 5 `ssr: false` fallback if prerender fails on `window`).

- [ ] **Step 11: Commit**

```bash
git add client/.gitignore client/.env.example client/package.json client/bun.lock client/components.json client/src server/scripts/export_openapi.py server/.gitignore
git commit -m "feat(client): add Cognito sign-in, typed API client and app shell"
```

---

### Task 10: `/memories` page

**Files:**
- Create: `client/src/lib/api/memories.ts`, `client/src/components/memories/kinds.ts`, `client/src/components/memories/memory-card.tsx`, `client/src/components/memories/memory-dialog.tsx`, `client/src/app/(app)/memories/page.tsx`

**Interfaces:**
- Consumes: `useApi()`, generated `components["schemas"]["Memory" | "MemoryCreate" | "MemoryUpdate"]`.
- Produces: hooks `useMemories(kind, q)`, `useCreateMemory()`, `useUpdateMemory()`, `useDeleteMemory()`; types `Memory`, `MemoryKind`.

- [ ] **Step 1: Query hooks** — `client/src/lib/api/memories.ts`

```ts
"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { useApi } from "./client"
import type { components } from "./schema"

export type Memory = components["schemas"]["Memory"]
export type MemoryKind = Memory["kind"]
export type MemoryCreate = components["schemas"]["MemoryCreate"]
export type MemoryUpdate = components["schemas"]["MemoryUpdate"]

const KEY = ["memories"] as const

export function useMemories(kind: MemoryKind | undefined, q: string) {
  const api = useApi()
  return useQuery({
    queryKey: [...KEY, kind ?? "all", q],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/memories", {
        params: { query: { kind, q: q || undefined, limit: 200 } },
      })
      if (error) throw new Error("Couldn't load memories")
      return data
    },
  })
}

export function useCreateMemory() {
  const api = useApi()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: MemoryCreate) => {
      const { data, error } = await api.POST("/api/memories", { body })
      if (error) throw new Error("Couldn't save memory")
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  })
}

export function useUpdateMemory() {
  const api = useApi()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, body }: { id: string; body: MemoryUpdate }) => {
      const { data, error } = await api.PATCH("/api/memories/{memory_id}", {
        params: { path: { memory_id: id } },
        body,
      })
      if (error) throw new Error("Couldn't update memory")
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  })
}

export function useDeleteMemory() {
  const api = useApi()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/api/memories/{memory_id}", {
        params: { path: { memory_id: id } },
      })
      if (error) throw new Error("Couldn't delete memory")
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  })
}
```

- [ ] **Step 2: Kind labels** — `client/src/components/memories/kinds.ts`

```ts
import type { MemoryKind } from "@/lib/api/memories"

export const KIND_LABEL: Record<MemoryKind, string> = {
  item_location: "Item location",
  fact: "Fact",
  preference: "Preference",
  todo: "To-do",
}

export const KIND_TABS = [
  { value: "all", label: "All" },
  { value: "item_location", label: "Item locations" },
  { value: "fact", label: "Facts" },
  { value: "preference", label: "Preferences" },
  { value: "todo", label: "To-dos" },
] as const
```

- [ ] **Step 3: Card with confirm-delete** — `client/src/components/memories/memory-card.tsx`

```tsx
"use client"

import { Pencil, Trash2 } from "lucide-react"
import { toast } from "sonner"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { type Memory, useDeleteMemory } from "@/lib/api/memories"

import { KIND_LABEL } from "./kinds"

export function MemoryCard({
  memory,
  onEdit,
}: {
  memory: Memory
  onEdit: () => void
}) {
  const remove = useDeleteMemory()

  return (
    <li>
      <Card>
        <CardContent className="flex items-start gap-2">
          <div className="min-w-0 flex-1 space-y-2">
            <p className="break-words">{memory.content}</p>
            <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
              <Badge variant="secondary">{KIND_LABEL[memory.kind]}</Badge>
              {memory.tags?.map((tag) => (
                <Badge key={tag} variant="outline">
                  #{tag}
                </Badge>
              ))}
              <time dateTime={memory.created_at}>
                {new Date(memory.created_at).toLocaleDateString()}
              </time>
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="size-11"
            aria-label="Edit memory"
            onClick={onEdit}
          >
            <Pencil />
          </Button>
          <AlertDialog>
            <AlertDialogTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-11"
                  aria-label="Delete memory"
                />
              }
            >
              <Trash2 />
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete this memory?</AlertDialogTitle>
                <AlertDialogDescription>
                  Alexa will no longer remember: “{memory.content}”
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  onClick={() =>
                    remove.mutate(memory.id, {
                      onSuccess: () => toast.success("Memory deleted"),
                      onError: () => toast.error("Couldn't delete. Try again."),
                    })
                  }
                >
                  Delete
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>
    </li>
  )
}
```

- [ ] **Step 4: Create/edit dialog** — `client/src/components/memories/memory-dialog.tsx`

```tsx
"use client"

import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  type Memory,
  type MemoryKind,
  useCreateMemory,
  useUpdateMemory,
} from "@/lib/api/memories"

import { KIND_LABEL } from "./kinds"

export function MemoryDialog({
  open,
  onOpenChange,
  memory,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  memory?: Memory
}) {
  const create = useCreateMemory()
  const update = useUpdateMemory()
  const pending = create.isPending || update.isPending

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    const content = String(form.get("content") ?? "").trim()
    if (!content) {
      toast.error("Write something for Alexa to remember.")
      return
    }
    const body = {
      kind: form.get("kind") as MemoryKind,
      content,
      tags: String(form.get("tags") ?? "")
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean)
        .slice(0, 10),
    }
    const callbacks = {
      onSuccess: () => {
        onOpenChange(false)
        toast.success(memory ? "Memory updated" : "Memory saved")
      },
      onError: () => toast.error("Couldn't save. Check the text and try again."),
    }
    if (memory) update.mutate({ id: memory.id, body }, callbacks)
    else create.mutate(body, callbacks)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{memory ? "Edit memory" : "Add memory"}</DialogTitle>
        </DialogHeader>
        <form
          key={memory?.id ?? "new"}
          onSubmit={onSubmit}
          className="space-y-4"
        >
          <div className="space-y-2">
            <Label htmlFor="memory-content">What should Alexa remember?</Label>
            <Textarea
              id="memory-content"
              name="content"
              required
              maxLength={2000}
              defaultValue={memory?.content}
              placeholder="My passport is in the top desk drawer"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="memory-kind">Type</Label>
            <select
              id="memory-kind"
              name="kind"
              defaultValue={memory?.kind ?? "fact"}
              className="border-input bg-background h-11 w-full rounded-md border px-3 text-sm"
            >
              {Object.entries(KIND_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="memory-tags">Tags (comma separated, optional)</Label>
            <Input
              id="memory-tags"
              name="tags"
              className="h-11"
              defaultValue={memory?.tags?.join(", ")}
              placeholder="travel, documents"
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={pending} className="min-h-11">
              {pending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 5: Page** — `client/src/app/(app)/memories/page.tsx`

```tsx
"use client"

import { Plus, Search } from "lucide-react"
import { useDeferredValue, useState } from "react"

import { KIND_TABS } from "@/components/memories/kinds"
import { MemoryCard } from "@/components/memories/memory-card"
import { MemoryDialog } from "@/components/memories/memory-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { type Memory, type MemoryKind, useMemories } from "@/lib/api/memories"

export default function MemoriesPage() {
  const [tab, setTab] = useState<string>("all")
  const [search, setSearch] = useState("")
  const q = useDeferredValue(search.trim())
  const kind = tab === "all" ? undefined : (tab as MemoryKind)
  const memories = useMemories(kind, q)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Memory | null>(null)

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 p-4 md:p-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Memories</h1>
        <Button className="min-h-11" onClick={() => setCreating(true)}>
          <Plus aria-hidden /> Add memory
        </Button>
      </header>

      <div className="relative">
        <Search
          className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
          aria-hidden
        />
        <Input
          aria-label="Search memories"
          placeholder="Search…"
          className="h-11 pl-9"
          maxLength={200}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
        <TabsList className="w-full justify-start overflow-x-auto">
          {KIND_TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {memories.isPending ? (
        <div className="space-y-3" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : memories.isError ? (
        <div
          role="alert"
          className="flex flex-col items-start gap-3 rounded-md border p-4"
        >
          <p>Couldn&apos;t load your memories.</p>
          <Button variant="outline" onClick={() => memories.refetch()}>
            Try again
          </Button>
        </div>
      ) : memories.data.length === 0 ? (
        <div className="text-muted-foreground rounded-md border border-dashed p-8 text-center">
          {q
            ? `Nothing matches “${q}”.`
            : "Nothing here yet. Tell Alexa “remember my passport is in the desk drawer”, or add one yourself."}
        </div>
      ) : (
        <ul className="space-y-3">
          {memories.data.map((m) => (
            <MemoryCard key={m.id} memory={m} onEdit={() => setEditing(m)} />
          ))}
        </ul>
      )}

      <MemoryDialog open={creating} onOpenChange={setCreating} />
      <MemoryDialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
        memory={editing ?? undefined}
      />
    </div>
  )
}
```

- [ ] **Step 6: Verify build**

Run: `bun run lint && bun run typecheck && bun run format && bun run build`
Expected: success. If a shadcn base-nova prop differs (e.g. `Tabs onValueChange` signature, `AlertDialogTrigger render`), align with the generated file in `src/components/ui/` and rerun.

- [ ] **Step 7: Commit**

```bash
git add client/src/lib/api/memories.ts client/src/components/memories client/src/app/\(app\)/memories
git commit -m "feat(client): add memories page with search, tabs, edit and delete"
```

---

### Task 11: End-to-end verification, docs, PR

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Run the stack against the dev AWS stack**

```bash
# terminal 1
cd server && cp .env.example .env   # fill from terraform outputs; COGNITO_CLIENT_IDS=["<dashboard>","<alexa-service>","<alexa-link>"]
uv run fastapi dev app/main.py
# terminal 2
cd client && bun dev
```

- [ ] **Step 2: Manual checks (record results in the PR description)**

1. Open http://localhost:3000 → Sign in → Cognito Hosted UI → sign up with email → lands on `/memories` with empty state.
2. Add "My passport is in the top desk drawer" (Item location, tag `travel`) → toast, appears in list.
3. Search "passport" → shows it; search "bicycle" → "Nothing matches".
4. Edit it → content updates; Delete → confirm dialog → removed.
5. Browser devtools at 375 px width: no horizontal scroll, bottom tab bar visible, buttons ≥ 44 px, dialogs usable.
6. **Expired session (Review Focus 5):** in devtools → Application → Local Storage, edit the `oidc.user:…` entry's `access_token` to `garbage`, then reload → API 401 → redirected to Cognito sign-in (not a blank error page).
7. Copy a fresh `access_token` from Local Storage → MCP Inspector (Task 7 Step 6) → call `remember` then `recall` → the memory shows up on `/memories` after refresh.
8. With `cloudflared tunnel --url http://localhost:8000` and the spike's add-on config pointed at the tunnel: ask Alexa "remember my keys are on the hook", then "where are my keys?" → correct answer; the memory appears in the dashboard (same Cognito user).

- [ ] **Step 3: Update `AGENTS.md`**

In the layout section change `Python 3.13` → `Python 3.14`. Add under **Commands**:

```markdown
| regenerate API types | — | `bun run gen:api` (after changing server routes/models) |
```

Add a section:

```markdown
## Local setup

1. Get dev values from the team password manager (or `terraform -chdir=infra/terraform output`).
2. `server/.env` from `server/.env.example`; AWS creds via `AWS_PROFILE`.
3. `client/.env.local` from `client/.env.example`.
4. Alexa testing: `cloudflared tunnel --url http://localhost:8000`, point the add-on at `<tunnel>/mcp`.
   See `docs/superpowers/spikes/2026-10-04-alexa-cognito-auth.md`.
```

- [ ] **Step 4: Final checks and PR**

Run: `cd server && uv run ruff check . && uv run pytest && cd ../client && bun run lint && bun run typecheck && bun run build`
Expected: all green.

```bash
git add AGENTS.md
git commit -m "docs: document local setup, Python 3.14 and API typegen"
git push -u origin feat/p0-foundation-memory   # ask before pushing
gh pr create -B main -t "feat: P0 foundation + memory (Cognito auth, MCP memory tools, dashboard)" \
  -b "Implements Plan 1 (docs/superpowers/plans/2026-10-03-p0-plan-1-foundation-memory.md). Manual check results: <paste Step 2 results>"
```
