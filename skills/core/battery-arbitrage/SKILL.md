---
name: battery-arbitrage
description: Propose bounded battery charging and discharging only when actuator qualification and feedback are present.
contract_version: v4
required_context: devices,state,energy_context,battery_qualification
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://energy,domotics://commissioning
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Battery arbitrage

This procedure keeps battery optimization proposal-only until the runtime has
qualified actuator bindings, limits and readable power feedback.

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

1. `discover_devices` — identify a qualified battery and canonical charge/discharge/stop routes.
2. `get_state` — read SOC, availability and numeric power feedback; stop when stale or unavailable.
3. `get_energy_context` — read tariffs, solar and terminal SOC policy for the horizon.
4. `optimize_scenario` — produce a bounded dispatch proposal only.
5. `validate_plan` — require actuator qualification, feedback postconditions and current dependencies.
6. `explain_solution` — show SOC limits, terminal reserve and forecast assumptions.
7. `operator_approval` — request approval for the exact bundle if physical mutation is allowed.
8. `commit_or_schedule_bundle` — delegate to admission, fencing and readback.

## Safety rules

- Non-zero battery dispatch without explicit actuator binding is analysis-only.
- Missing/stale/invalid/mismatching power feedback confirms neither success nor failure; use `UNKNOWN`.
- Never cross SOC, power, thermal or terminal-reserve limits.
- A forecast or SOC value is not authority and cannot replace commissioning.
- Do not retry after an unknown inverter write without reconciliation evidence.
