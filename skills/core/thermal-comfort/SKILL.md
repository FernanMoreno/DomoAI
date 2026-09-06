---
name: thermal-comfort
description: Propose bounded HVAC changes that balance comfort, occupancy evidence and energy cost.
contract_version: v4
required_context: devices,state,energy_context,comfort_bounds
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://energy
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Thermal comfort

This procedure proposes HVAC intent from semantic temperature, occupancy and
comfort bounds. It never bypasses policy or writes an adapter directly.

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

1. `discover_devices` — identify canonical HVAC and readable temperature capabilities.
2. `get_state` — read temperature, mode, occupancy and freshness; stop on stale safety context.
3. `get_energy_context` — read the requested tariff and forecast horizon.
4. `optimize_scenario` — propose bounded setpoints with hard comfort limits and anti-cycle windows.
5. `validate_plan` — re-check current policy, route, state dependencies and safety limits.
6. `explain_solution` — explain comfort trade-offs, assumptions and cycle spacing.
7. `operator_approval` — request approval for a physical change when policy requires it.
8. `commit_or_schedule_bundle` — delegate the exact approved bundle to runtime admission and readback.

## Safety rules

- Hard temperature and anti-cycle limits outrank cost optimization.
- Missing occupancy or temperature evidence means stop, not an aggressive default.
- Do not infer heating/cooling capability from a vendor model name.
- A failed postcondition, timeout or stale feedback is `UNKNOWN`; no blind retry.
- Never alter security, locks, alarms or emergency modes as a comfort side effect.
