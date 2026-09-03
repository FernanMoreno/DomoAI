#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

# The lab .env is ignored by Git and contains the operator-provided HA token.
# Load only KEY=VALUE assignments; never source executable shell content and
# never print the values. Explicit process environment remains authoritative.
LAB_ENV_FILE="$PROJECT_ROOT/dev/lab/.env"
if [[ -f "$LAB_ENV_FILE" ]]; then
    while IFS='=' read -r key value || [[ -n "$key" ]]; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        [[ -v "$key" ]] && continue
        export "$key=$value"
    done < "$LAB_ENV_FILE"
fi

# Run the installed entrypoint directly when the project environment exists.
# Keeping ``uv`` out of the long-lived process tree ensures SIGTERM reaches
# Python/Uvicorn and the gateway's async finally block can release runtime
# ownership and close adapters.  The fallback keeps the launcher usable before
# the virtual environment has been created.
GATEWAY_BIN="$PROJECT_ROOT/.venv/bin/domoai-mcp-gateway"
if [[ -x "$GATEWAY_BIN" ]]; then
    exec "$GATEWAY_BIN"
fi

exec uv run --project "$PROJECT_ROOT" domoai-mcp-gateway
