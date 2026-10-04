FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project
COPY app ./app
EXPOSE 8000
CMD ["uv", "run", "fastapi", "run", "app/main.py", "--port", "8000"]
