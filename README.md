# homie

Alexa+ memory assistant with Google Maps / Calendar integrations — [Amazon Developer Hackathon 2026](https://amazonappdev2026.devpost.com/).

```
server/   FastAPI backend (uv) — app/{routes,models,services}
client/   Next.js 16 dashboard (bun, shadcn/ui, husky + prettier)
infra/    Docker + Terraform
```

## Dev

```sh
# backend → http://localhost:8000/docs
cd server && cp .env.example .env && uv sync && uv run fastapi dev app/main.py

# frontend → http://localhost:3000
cd client && bun install && bun dev        # `bun install` also installs the git pre-commit hook
```

Checks: `uv run pytest`, `uv run ruff check .` (server) · `bun run lint`, `bun run format` (client).

## Docker

```sh
docker compose -f infra/docker-compose.yml up --build
```
