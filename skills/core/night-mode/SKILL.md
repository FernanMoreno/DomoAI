---
name: night-mode
description: Prepare an explicit, bounded night scene for lights and climate without changing security authority.
contract_version: v4
required_context: devices,state,energy_context,night_policy
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://policies
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Night mode

This procedure proposes an explicit bounded scene for approved lights and
climate devices. Security devices are outside its authority.

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

1. `discover_devices` — select only devices explicitly included by night policy.
2. `get_state` — read state and occupancy evidence; stop if required context is stale.
3. `get_energy_context` — read bounded cost context when climate/load shifting is requested.
4. `optimize_scenario` — produce a proposal with explicit device and time limits.
5. `validate_plan` — re-check policy, authority, route and readback requirements.
6. `explain_solution` — list every affected device and excluded security action.
7. `operator_approval` — obtain approval for the exact scene bundle when required.
8. `commit_or_schedule_bundle` — delegate ordered execution to runtime admission and readback.

## Safety rules

- Never unlock, disarm, disable alarms or alter security authority implicitly.
- A night scene must have a bounded device list and expiry; no open-ended mutation.
- Missing state, policy or readback stops the scene.
- Partial execution is reported by member status; do not pretend rollback occurred.
