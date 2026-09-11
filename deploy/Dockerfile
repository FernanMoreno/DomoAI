FROM ghcr.io/astral-sh/uv:0.12.4@sha256:d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c AS uv

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY skills/core ./skills/core
COPY config ./config

RUN addgroup --system --gid 10001 domoai \
    && adduser --system --uid 10001 --ingroup domoai --home /nonexistent \
        --no-create-home --shell /usr/sbin/nologin domoai \
    && mkdir -p /app/data \
    && chown -R domoai:domoai /app \
    && uv export --locked --no-dev --format requirements-txt \
        --no-emit-project --output-file /tmp/requirements.txt \
    && uv pip install --system --require-hashes -r /tmp/requirements.txt \
    && uv pip install --system --no-deps . \
    && rm /tmp/requirements.txt

USER domoai:domoai

EXPOSE 8124

CMD ["domoai-mcp-gateway"]
