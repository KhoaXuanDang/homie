# AGENTS.md

Guide for AI agents (and humans) working in this repo.

## What this is

**homie** — an Alexa+ memory assistant for the [Amazon Developer Hackathon 2026](https://amazonappdev2026.devpost.com/) (Alexa+ track).
Users tell Alexa things to remember (where items are, preferences, to-dos); Alexa uses that memory with Google Maps and
Google Calendar to suggest places/routes and create reminders. A web dashboard lets users see and control what is stored.

Alexa+ integrations go through **MCP** (spec 2025-11-25 or later, **Streamable HTTP** transport). The backend is that MCP server.

## Layout

```
server/   Python 3.13, FastAPI, uv
  app/main.py      FastAPI app, middleware, router registration
  app/config.py    Settings (pydantic-settings, reads server/.env)
  app/routes/      HTTP routers — thin: parse request, call a service, return
  app/services/    business logic + external API clients (Google, storage)
  app/models/      pydantic schemas / DB models
  tests/           pytest
client/   Next.js 16 (App Router, src/), React 19, Tailwind 4, shadcn/ui, bun
  src/app/         routes/pages
  src/components/ui/  shadcn components (generated — add via CLI, don't hand-write)
  src/lib/         shared helpers
infra/    docker/*.Dockerfile, docker-compose.yml, terraform/ (AWS)
docs/     specs and plans
```

## Commands

Run from the package directory. `uv` lives at `~/.local/bin/uv` if it's not on PATH.

| Task | server/ | client/ |
|---|---|---|
| install | `uv sync` | `bun install` (also installs the git hook) |
| dev | `uv run fastapi dev app/main.py` (:8000, docs at /docs) | `bun dev` (:3000) |
| test | `uv run pytest` | — |
| lint | `uv run ruff check .` | `bun run lint` |
| format | `uv run ruff format .` | `bun run format` |
| add dep | `uv add <pkg>` / `uv add --dev <pkg>` | `bun add <pkg>` / `bun add -d <pkg>` |
| add UI component | — | `bunx --bun shadcn@latest add <name>` |

Full stack: `docker compose -f infra/docker-compose.yml up --build`.

Before saying a change is done: run the test + lint commands for every package you touched.

## Conventions

- **Server:** routes stay thin; logic lives in `services/`. Config comes only from `app/config.py` `settings`; never read
  `os.environ` directly. New env vars go in both `config.py` and `.env.example`. Use type hints everywhere.
- **Client:** server components by default; add `"use client"` only when needed. Use shadcn components before writing
  custom UI. Prettier style: no semicolons, double quotes; Tailwind classes are auto-sorted.
- **Next.js 16 is newer than most training data.** Before using an unfamiliar Next API, check the bundled docs at
  `client/node_modules/next/dist/docs/` (or Context7) instead of guessing.
- `cn` is imported from the `cn` package (shadcn's built-in clsx + tailwind-merge replacement), re-exported from
  `@/lib/utils`.
- Use package managers to add deps (`uv add`, `bun add`) — don't hand-edit versions in lock files.
- Keep it simple: no abstractions, config knobs or scaffolding until a second real use exists.

### Checklist for every server service / endpoint / MCP tool

- **Edge cases:** empty/missing input, unknown or already-deleted IDs, duplicates, timezones (always store UTC,
  convert with the user's `PROFILE.timezone`), oversized payloads, external API down/slow/returning nothing.
- **Security:** every read/write is scoped to the authenticated user's `sub` — never trust a user ID from the request
  body. Validate input with pydantic at the boundary. Never log tokens, secrets or memory content. Presigned S3 URLs
  are short-lived and scoped to the user's prefix.
- **Rate limits:** cap per-user request rates on our endpoints (especially the Bedrock simulator), respect Google API
  quotas (timeouts + handle 429 with a friendly message, no hot retry loops), and bound list sizes (`limit` params).

### Checklist for every client UI change

- **Responsive:** works from phone width (~375px) to desktop; no horizontal scroll; tap targets ≥ 44px.
- **User-friendly:** loading, empty and error states for every data view; confirm destructive actions (delete
  memory/media, disconnect Google); plain-language error messages; keyboard accessible with visible focus and labels.

## Git workflow

- Never commit to `main` directly: branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`) and open a PR with `gh pr create`.
- Commit messages: Conventional Commits with scope, e.g. `feat(server): add memory MCP tools`.
- **Never commit `.env` files** (only `.env.example`). Check `git diff --cached --name-only` before committing.
- Stage specific paths, not `git add -A`. If a change touches more than 10 files, split it into focused commits or PRs.
- The pre-commit hook (husky in `client/.husky`, runs lint-staged) runs for every commit in the repo; it only formats
  staged client files. Don't bypass it with `--no-verify`.
- Ask before pushing to a remote.

## Secrets & external services

Google API keys and OAuth client credentials live in `server/.env` (template: `server/.env.example`). The client must
never hold Google secrets — it talks only to the server.
