# Phase 4 qualification preflight

- Status: `blocked_external_dependency`
- Environment: `local_preflight`
- Run: `phase4-preflight-187-20260906T011853Z`
- Seed: `187`
- Started: `2026-09-06T01:18:53.548600+00:00`
- Completed: `2026-09-06T01:19:15.214381+00:00`

## Software gates

| Gate | Status | Exit code | Details |
|---|---|---:|---|
| `digital_twin` | `passed` | `0` | `{"adapters":["fixture","home_assistant","knx","matter","modbus","zigbee2mqtt"],"checks":["audit","automation","discovery","faults","idempotency","identity","normalization","optimization","privacy","product","readback","recovery","routing","scheduler"],"domains":["battery","climate","cover","environment","ev","light","power","solar","switch","water"],"failed_check_codes":[],"invariant_violations":[],"plant_digest":"837c82b7d9071ba4782d2201918a07b1c60dd6a956a3b6ebb8e87fbab02ca98c","run_id":"twin-187","seed":187,"trace_digest":"be7598b1360c40ae2f4e39d0c2720af1ea41b11b9b1cecaa60a4e5972428d7ff"}` |
| `process_lab` | `passed` | `0` | `{"tests":["tests/integration/test_digital_twin_matrix.py","tests/integration/test_virtual_lab_assets.py","tests/integration/test_virtual_lab_smoke_configuration.py","tests/integration/test_zigbee2mqtt_fixture.py","tests/integration/test_modbus_fixture.py","tests/unit/lab/test_battery_simulator.py","tests/unit/lab/test_ev_charging_simulator.py","tests/unit/lab/test_water_consumption_simulator.py","tests/unit/lab/test_thermal_simulator.py","tests/integration/test_matter_server_fixture.py","tests/integration/test_knx_fixture.py","tests/integration/test_home_assistant_provider_runtime.py","tests/integration/test_multi_adapter_runtime.py","tests/integration/test_solar_self_consumption_mcp.py"]}` |

## Physical gates

| Gate | Status | Evidence scope | Reason |
|---|---|---|---|
| `battery_hil` | `blocked_external_dependency` | `external_blocked` | `hardware_not_available` |
| `external_provider` | `blocked_external_dependency` | `external_blocked` | `independent_provider_not_deployed` |
| `live_protocol_commissioning` | `blocked_external_dependency` | `external_blocked` | `physical_bus_not_available` |

## Residuals

- `battery_hil`
- `live_protocol_commissioning`
- `external_provider`

This report proves local software/process behavior only. It is not physical commissioning or HIL evidence and cannot enable production readiness.
