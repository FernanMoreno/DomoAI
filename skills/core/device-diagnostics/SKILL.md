---
name: device-diagnostics
description: Inspect canonical inventory and state to produce bounded diagnostics without mutation.
contract_version: v4
required_context: devices,state
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.explain_solution
allowed_resources: domotics://devices,domotics://runtime
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: none
failure_mode: stop_and_report
---

# Device diagnostics

This portable procedure reads only the canonical runtime inventory and state.
It reports availability, freshness and bounded diagnostics. It never calls an
adapter, vendor API or solver directly and it never changes a device.

## Declared operations

- `discover_devices`
- `get_state`
- `explain_solution`

## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `explain_solution` → `mcp.explain_solution` (`read`)

## Procedure

1. `discover_devices` — read canonical devices and capabilities.
2. `get_state` — read current state, availability and freshness.
3. `explain_solution` — report evidence and stop on stale or unavailable context.

## Safety rules

- Diagnostics are read-only and do not grant execution authority.
- Raw vendor payloads, credentials and direct routes are not part of the result.
- A repair or mutation request must start a separate approved plan workflow.
