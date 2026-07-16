FROM node:24-bookworm-slim AS node_runtime

FROM python:3.12-slim-bookworm

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl gh git libatomic1 ripgrep \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.8.14

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown -R agent:agent /workspace /app

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/agent \
    WORKSPACE_DIR=/workspace

USER agent
EXPOSE 8080

CMD ["python", "-m", "slack_codex.app"]
