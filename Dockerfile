FROM python:3.11.15-slim AS builder

# Build dependencies for packages that compile native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv from its official image at a pinned version (no remote install script)
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /uvx /bin/

WORKDIR /app

# Copy project files
COPY . .

# Install exactly the versions in uv.lock into a local venv
RUN uv sync --locked --no-dev

FROM python:3.11.15-slim
LABEL maintainer="codescent" \
      project="mariadb-mcp"

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}"

# Copy venv and app from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Fix venv python symlink (builder's uv python path doesn't exist in final stage)
RUN ln -sf /usr/local/bin/python3 /app/.venv/bin/python

EXPOSE 9001

CMD ["python", "src/server.py", "--host", "0.0.0.0", "--transport", "sse"]
