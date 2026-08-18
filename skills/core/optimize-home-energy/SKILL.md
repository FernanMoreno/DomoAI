---
name: optimize-home-energy
description: Coordinate semantic home reads and solver proposals while preserving runtime approval and execution safety.
contract_version: v2
---

# Optimize home energy

This portable procedure coordinates context gathering, optimization and the
existing plan boundary. It is usable by compatible Agent Skills hosts, but it
does not grant authorization and it never calls a physical adapter directly.

## Contract metadata

- Contract version: `v2`
- Fixture reference: `tests/integration/test_core_skill.py`
- Safety owner: DomoAI runtime policy and plan executor
- Host mapping: every DomoAI operation uses one general `mcp` connection;
  hosts must not replace it with separate server instances, vendor calls or
  arbitrary solver code.

## Declared operations

- `discover_devices`
- `get_state`
- `get_energy_context`
- `optimize_scenario`
- `validate_plan`
- `explain_solution`
- `operator_approval`
- `execute_plan`

## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `get_energy_context` → `mcp.get_energy_context` (`read`)
- `optimize_scenario` → `mcp.optimize_scenario` (`proposal`)
- `validate_plan` → `mcp.validate_plan` (`validation`)
- `explain_solution` → `mcp.explain_solution` (`read`)
- `operator_approval` → `operator.request_approval` (`approval`)
- `execute_plan` → `mcp.execute_plan` (`mutation`)

## Procedure

1. `discover_devices` — read the canonical inventory and identify controllable
   energy devices, their areas and capabilities.
2. `get_state` — read current state, freshness and availability for the selected
   devices. Stop if required state is stale or unavailable and the operator has
   not accepted it as an assumption.
3. `get_energy_context` — read the complete typed tariff, solar and optional
   battery context for the requested horizon. Stop if the provider cannot
   return a complete context or its runtime revision is stale.
4. `optimize_scenario` — build a solver-neutral scenario with explicit horizon,
   resolution, units, loads, constraints and objectives. Request a proposal only.
5. `validate_plan` — send the returned proposal through the runtime validation
   and policy boundary. Never treat an optimizer result as authorization.
6. `explain_solution` — explain selected slots, hard constraints, diagnostics and
   assumptions in user-facing language.
7. `operator_approval` — if policy or risk requires confirmation, pause and ask
   the operator. The skill cannot approve its own plan.
8. `execute_plan` — execute only the validated plan with the matching digest and
   approval. The runtime performs the final revision, policy and postcondition
   checks.

## Safety rules

- A missing approval is a stop condition, not an invitation to retry execution.
- A changed runtime revision or validation digest requires revalidation.
- The skill may explain or revise a proposal, but may not edit runtime policies.
- Vendor APIs, adapter identifiers, extra MCP servers and arbitrary
  Python/solver code are outside this procedure.
