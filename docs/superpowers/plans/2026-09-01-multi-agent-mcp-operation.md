# Multi-Agent MCP Interoperability and Operations

**Goal:** Prove that Codex, Claude, OpenCode, Gemini, and any MCP-compatible
host can share one configured DomoAI gateway with distinct identity/scopes,
canonical state, idempotent mutation boundaries, and clean transport lifecycle.

**Architecture:** Reuse the existing unified Streamable HTTP gateway and
runtime. Add a vendor-neutral read-only probe in `domoai.mcp.probe`, an
entrypoint, authenticated official-MCP-client composition tests, and a
project-owned bounded shutdown drain. Do not add vendor SDKs, a second runtime,
or lab changes.

## Implementation sequence

1. Write regression tests for probe input/output/error redaction and lifecycle
   leak before implementation; run them to establish RED.
2. Implement canonical JSON/digest and secret-free probe result/error types.
3. Implement the async official MCP session probe and CLI environment/stdin
   token handling; add the script entrypoint.
4. Add four-token authenticated network composition proof covering catalog,
   `domotics://runtime`, discovery, identity/scopes, cancellation, and
   reconnect against one gateway/SQLite runtime.
5. Fix the project-owned server test lifecycle so sse-starlette shutdown
   watchers are observed/drained before global state resets; keep the drain
   bounded and test second-start behavior.
6. Add/update deployment documentation for all named hosts using only the
   common MCP endpoint/token contract.
7. Run focused and full applicable gates, inspect the diff and lab isolation,
   perform system composition review, refresh Graphify if possible, and persist
   durable findings in Obsidian.

## Verification commands

```bash
uv run pytest -q tests/unit/mcp tests/integration/test_mcp_gateway_http.py \
  tests/integration/test_mcp_transport_parity.py \
  tests/contract/test_mcp_gateway_contract.py \
  tests/composition/test_multi_agent_mcp_operation.py
PYTHONASYNCIODEBUG=1 uv run pytest -q tests/integration/test_mcp_gateway_http.py -W error
uv run ruff check src tests
uv run mypy src
bash scripts/composition_check.sh
git diff --check
```

No commit or push is part of this plan.
