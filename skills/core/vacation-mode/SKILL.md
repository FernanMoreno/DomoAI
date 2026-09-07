---
name: vacation-mode
description: Prepare an expiring vacation proposal for protection and bounded savings with explicit scope.
contract_version: v4
required_context: devices,state,energy_context,vacation_policy
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://policies
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Vacation mode

This portable procedure creates a bounded, expiring proposal. It cannot create
standing authority or turn simulation into a security guarantee.

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

1. `discover_devices` — enumerate the explicit vacation policy scope.
2. `get_state` — read availability and safety state; stop on stale context.
3. `get_energy_context` — read bounded tariff and forecast assumptions.
4. `optimize_scenario` — propose savings and presence actions with an expiry.
5. `validate_plan` — bind every plan to current policy, authority and dependencies.
6. `explain_solution` — distinguish simulation from real protection and list expiry.
7. `operator_approval` — request approval for the exact finite bundle.
8. `commit_or_schedule_bundle` — submit only the approved bundle for runtime admission and readback.

## Safety rules

- Every vacation action must have an explicit expiry and household/area scope.
- Presence simulation is not a security guarantee; never disarm or unlock devices.
- Do not create indefinite standing consent from a one-time vacation request.
- Stale, unavailable, timed-out or unconfirmed actions stop with `UNKNOWN`.
