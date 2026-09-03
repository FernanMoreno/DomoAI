# Real Lab Composition Implementation Plan

> Execute with the lab fixtures unchanged; persistent Home Assistant battery
> bindings use stable registry identity claims instead of generated device IDs.

**Goal:** Verify the complete configured runtime against Docker Home Assistant,
KNX Virtual/ETS on Windows through the WSL `knxd` bridge, and the shared virtual
battery over HTTP, Modbus and Home Assistant.

**Spec:** `specs/158-physical-authority-program/blocks/10-real-lab-composition.md`

## Tasks

- [x] Inspect Docker service health, published ports, KNX process ownership and
  bridge readiness without changing the lab configuration.
- [x] Verify the live multi-adapter command/readback path through
  `PlanService`/`PlanExecutor` for Home Assistant, Zigbee2MQTT, Matter, KNX and
  Modbus.
- [x] Verify the Home Assistant provider command path with an independent
  readback and unconditional cleanup.
- [x] Verify shared virtual battery state across HTTP, Modbus (`1503`), HA and
  KNX Virtual/ETS, including authorized battery routing and STOP cleanup.
- [x] Correct the integration test boundaries so provider validation receives a
  provider snapshot and actuator routes are exercised only with an explicit
  `DispatchableBatteryBinding`.
- [x] Migrate the persistent Home Assistant battery mapping from the recreated
  device UUID to its stable MQTT registry identity claim and verify the real
  mapping live.
- [x] Record evidence scope and residual limitations; do not claim physical
  hardware qualification.

## Verification record

- Live multi-adapter + HA plan tests: **2 passed in 15.35s**.
- Live shared battery tests: **2 passed in 12.17s**.
- KNX gateway/bridge live smoke previously verified: **1 passed**.
- Docker health, HA state discovery, battery health/readback and bridge
  readiness were independently inspected.
- The persistent lab mapping now uses `identity_claims.identity_keys` with
  `mqtt:lab-battery-1` for both capacity and dispatch. The live provider
  resolved the current HA registry device and the shared battery test passed
  with the real mapping; no ephemeral mapping was used.

## Evidence boundary

The result proves software composition against virtual/containerized systems and
KNX Virtual/ETS. It does not prove an inverter, battery firmware, physical
electrical envelope, hardware identity or production HIL qualification.
