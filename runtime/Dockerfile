FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl gh git ripgrep \
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
