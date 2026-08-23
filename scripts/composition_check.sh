#!/usr/bin/env bash
# Non-interactive architecture + composition gate for CI/local use.
#
#   scripts/composition_check.sh          # architecture + full test suite
#   scripts/composition_check.sh --fast   # architecture + unit/contract only,
#                                          # skips tests/composition (Docker)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Source-derived architecture gate =="
uv run python scripts/check_architecture_contracts.py

echo "== Import Linter (diagnostic migration baseline) =="
if ! uv run lint-imports; then
    cat <<'EOF'
WARNING: Import Linter still reports the documented historical runtime
orchestration edges. The source-derived gate above remains blocking; see
docs/architecture-contracts.md for the migration boundary.
EOF
fi

if [[ "${1:-}" == "--fast" ]]; then
    echo "== pytest (unit + contract, no Docker-backed composition tests) =="
    uv run pytest tests/unit tests/contract
else
    echo "== pytest (full suite, including Docker-backed composition tests) =="
    uv run pytest
fi
