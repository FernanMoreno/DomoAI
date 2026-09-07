---
name: optimize-ev-charging
description: Propose and safely schedule EV charging against deadline, tariff and bounded site power.
contract_version: v4
required_context: devices,state,energy_context,ev_capability
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.get_energy_context,mcp.optimize_scenario,mcp.validate_plan,mcp.explain_solution,operator.request_approval,mcp.commit_or_schedule_bundle
allowed_resources: domotics://devices,domotics://runtime,domotics://energy,domotics://commissioning
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: physical_mutation
failure_mode: stop_and_report
---

# Optimize EV charging

This portable procedure creates a bounded charging proposal. It does not infer
charger authority from a vehicle label and never calls a vendor route.

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

1. `discover_devices` — identify a qualified EV charger and its canonical power capability.
2. `get_state` — read connector, charging and availability state; stop when stale or unavailable.
3. `get_energy_context` — read tariff and site-power context for the complete deadline horizon.
4. `optimize_scenario` — propose charging slots with explicit capacity, deadline and import limits.
5. `validate_plan` — bind the proposal to current policy, capabilities and state dependencies.
6. `explain_solution` — show deadline margin, power assumptions and any unserved energy.
7. `operator_approval` — request approval for the exact ordered bundle when mutation is required.
8. `commit_or_schedule_bundle` — submit only the approved, digest-bound bundle for runtime admission and readback.

## Safety rules

- Never exceed charger, household or contract-power limits; a missing limit is a stop condition.
- Do not treat vehicle presence as permission to charge or as proof of connector safety.
- A stale connector, meter or power-feedback state stops the procedure.
- Approval covers the exact bundle digest, not a future replacement proposal.
- Timeout, rejected admission or missing readback is `UNKNOWN`; do not retry blindly.
- No direct adapter, vendor, solver or alarm/security operation is allowed.
