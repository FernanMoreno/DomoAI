# Unified general MCP interface

DomoAI exposes one public local MCP process: `uv run domoai-mcp`. Any
MCP-compatible client that supports the documented stdio transport may connect
to it. Claude Code and Codex are interchangeable examples, not required SDKs
or server integrations.

## Architecture boundary

```text
AI / compatible MCP clients
             │
       one MCP + Skills
             │
     DomoAI Unified MCP
             │
  policy/runtime + OR-Tools proposal layer
             │
 Universal Device Model + Adapter Runtime
             │
 HA | Matter | MQTT | KNX | Zigbee | Modbus
```

The server registers the existing semantic tools and resources from one
runtime composition. OR-Tools validates scenarios, computes proposals and
explains results inside that server. It cannot call adapters, approve plans or
execute physical commands. Mutations still use `validate_plan` and
`execute_plan` through the runtime policy, digest, revision and audit boundary.

## Generic configuration

```json
{
  "mcpServers": {
    "domoai": {
      "command": "uv",
      "args": ["run", "domoai-mcp"],
      "cwd": "/absolute/path/to/DomoAI"
    }
  }
}
```

Do not configure a second `domoai-ortools-mcp` server. The public script is
intentionally absent; `src/domoai/mcp/ortools_server.py` is an internal
registration module used by the unified composition and focused tests.

## Portable skill

`skills/core/optimize-home-energy/SKILL.md` routes all DomoAI operations through
the single `mcp` role. `operator` is a separate consent boundary supplied by
the host. A host may change configuration syntax or presentation, but it may
not introduce vendor tools, direct adapters, arbitrary solver code or a second
server role.

The `operator` boundary is enforced by the server, not merely documented.
Production hosts must inject an authenticated `OperatorPrincipal` and a
one-time `ApprovalAssertion` from the trusted UI/host boundary. The principal
records who is authenticated; the assertion proves that this human explicitly
approved the exact plan or bundle digest, with a nonce and expiry. A principal
without an assertion is rejected and cannot issue a grant. No reusable
credential is exposed to the MCP tool schema. The local/dev compatibility path accepts an
`operator_token` only when `DOMOAI_ALLOW_LEGACY_OPERATOR_TOKEN=1` is enabled
and the value matches `DOMOAI_OPERATOR_APPROVAL_TOKEN` (compared in constant
time). The default is disabled. If the token is unset, blank, disabled or
incorrect, the server refuses to issue a grant and reports
`operator_authentication_failed`.

## Validation

```bash
uv run pytest -q \
  tests/contract/test_unified_mcp_contract.py \
  tests/integration/test_unified_mcp_compatibility.py \
  tests/contract/test_skill_contract.py \
  tests/integration/test_energy_skill_workflow.py
```

The deterministic suite proves two independent MCP sessions plus a
protocol-level flow receive equivalent catalogs/results and that optimization
does not call the fixture adapter.

Validation evidence on 2026-08-23: full pytest `972 passed, 10 skipped`, Ruff
clean, `uv lock --check` clean and mypy clean for `src`. No credentials or live
hardware are required for this deterministic contract; the separate Home
Assistant inverter HIL smoke was not executed.

The current energy skill contract is v3 and uses
`commit_or_schedule_bundle`. A valid optimizer result may also be
`no_action_required`; that terminal result has no plan, approval or adapter
write. Battery dispatch remains software-qualified until matching HIL evidence
is installed.
