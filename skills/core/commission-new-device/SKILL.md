---
name: commission-new-device
description: Discover and verify a new device through canonical read-only runtime evidence.
contract_version: v4
required_context: devices,state
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.explain_solution
allowed_resources: domotics://devices,domotics://runtime,domotics://commissioning
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: none
failure_mode: stop_and_report
---

# Commission a new device

This procedure verifies that a newly discovered device has a stable canonical
identity, expected capabilities and current state. Discovery is evidence only;
it never enrolls, configures or actuates a device.

## Declared operations

- `discover_devices`
- `get_state`
- `explain_solution`

## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `explain_solution` → `mcp.explain_solution` (`read`)

## Procedure

1. `discover_devices` — obtain the canonical inventory and identity evidence.
2. `get_state` — confirm availability and current state for the candidate.
3. `explain_solution` — report missing capabilities, stale evidence or conflicts.

## Safety rules

- Commissioning remains read-only until a separate operator-approved workflow.
- It cannot infer a vendor route, write configuration or acquire authority.
- Missing, stale or contradictory evidence stops commissioning and is audited.
