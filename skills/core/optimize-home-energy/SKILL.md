---
name: optimize-home-energy
description: Coordinate semantic home reads and solver proposals while preserving runtime approval and execution safety.
contract_version: v3
---

# Optimize home energy

This portable procedure coordinates context gathering, optimization and the
existing plan boundary. It is usable by compatible Agent Skills hosts, but it
does not grant authorization and it never calls a physical adapter directly.

## Contract metadata

- Contract version: `v3`
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
- `commit_or_schedule_bundle`

## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `get_energy_context` → `mcp.get_energy_context` (`read`)
- `optimize_scenario` → `mcp.optimize_scenario` (`proposal`)
- `validate_plan` → `mcp.validate_plan` (`validation`)
- `explain_solution` → `mcp.explain_solution` (`read`)
- `operator_approval` → `operator.request_approval` (`approval`)
- `commit_or_schedule_bundle` → `mcp.commit_or_schedule_bundle` (`mutation`)

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
   the operator. For a bundle, approve the complete ordered `bundle_digest`
   shown in the explanation; the skill cannot approve its own plan.
8. `commit_or_schedule_bundle` — hand the complete ordered bundle to the
   runtime-owned commit boundary with the bundle digest and per-member
   approval ids. The runtime performs preflight, durable scheduling or
   sequential physical execution, final revision/policy checks and recovery
   bookkeeping. It never infers rollback for an already written member.

## Safety rules

- A missing approval is a stop condition, not an invitation to retry execution.
- An authenticated operator principal is not consent by itself. The trusted
  host must provide a one-time human `ApprovalAssertion` bound to the exact
  plan or `bundle_digest`; missing, mismatching, replayed or expired assertions
  stop before a grant or physical mutation is created.
- A changed runtime revision or validation digest requires revalidation.
- Battery telemetry or mathematical limits without an explicit actuator
  binding are analysis-only. Non-zero battery dispatch must stop with
  `battery_actuation_unbound` before approval, scheduling, or execution.
- A bound battery must identify a canonical device/capability and distinct
  charge, discharge, and stop commands. The runtime must revalidate those
  commands against the live inventory and safety boundary; the skill never
  infers a physical route from battery telemetry.
- A dispatchable battery must also identify one readable numeric `kW`
  `power_feedback_capability`, its `charge_positive`/`discharge_positive`
  convention, and a positive tolerance. Compiled battery commands carry this
  measured-power postcondition; missing, stale, unavailable, invalid, or
  mismatching feedback is `UNKNOWN`, never confirmed success.
- `BatteryState` is read-only planning evidence for initial SOC, not an
  authorization or closed-loop post-execution guarantee. Inverter settling,
  SOC reconciliation, and crash recovery remain separate contracts.
- Plan execution performs a plan-wide safety preflight before the first
  physical write. Known precondition or configured SafetyKernel failures
  reject the attempt with no adapter call; sequential effects are projected
  only for canonical deterministic commands, and just-in-time checks still
  run before every write.
- `bundle_digest` identifies the human-reviewed ordered bundle; each member's
  `validation_digest` remains the runtime authority for MCP approval and
  each bundle-bound approval grant is also checked against the same
  `bundle_digest`. A successful workflow may therefore be `scheduled` without
  meaning that physical execution has already happened.
- The skill may explain or revise a proposal, but may not edit runtime policies.
- Vendor APIs, adapter identifiers, extra MCP servers and arbitrary
  Python/solver code are outside this procedure.
