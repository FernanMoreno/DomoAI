---
name: solar-self-consumption
description: Shift flexible loads toward measured and forecast solar while keeping export and import limits explicit.
contract_version: v4
required_context: devices,state,energy_context,solar_forecast
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://energy
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Solar self-consumption

This portable procedure uses solar forecast as bounded planning evidence and
requires live validation before any physical mutation.

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

1. `discover_devices` — find canonical flexible loads, meter and solar telemetry.
2. `get_state` — read current production/load evidence and stop if stale.
3. `get_energy_context` — read forecast, tariff and export assumptions for the horizon.
4. `optimize_scenario` — propose load shifting with explicit import/export constraints.
5. `validate_plan` — bind the proposal to current routes, policy and live dependencies.
6. `explain_solution` — show which slots depend on forecast assumptions.
7. `operator_approval` — obtain approval for the exact physical bundle when required.
8. `commit_or_schedule_bundle` — submit the digest-bound bundle for admission and readback.

## Safety rules

- A forecast is not measured production and never confirms that a load may start.
- Missing, stale or low-confidence production evidence stops the physical path.
- Never exceed import/export, device or contract limits.
- If readback disagrees with the proposal, report `UNKNOWN` and stop retries.
- No battery dispatch is allowed without its separate qualification and feedback contract.
