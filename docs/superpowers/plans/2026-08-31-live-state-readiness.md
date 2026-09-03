# Live State and Readiness Repair Plan

> **Execution:** execute this plan inline in the current session, one task at a time, with RED tests before production edits.

## Context

The live gateway currently has all configured transports connected, but `/readyz` can report `ready` while canonical state is stale or unavailable. A manual refresh temporarily reduces the age, then the age grows again because no periodic state refresh exists. Home Assistant is configured only with URL/token in the running process, so its mapping document is not loaded; the real virtual battery and EV entity IDs are therefore projected as conflicting generic `value` capabilities. Matter Server currently reports node 5 unavailable and node 7 available; persistent volumes must be preserved, so the unavailable node must be diagnosed and treated explicitly rather than hidden by deleting data.

## Scope and invariants

- Preserve source `observed_at` as device/provider provenance and `received_at` as the runtime receipt time.
- Make freshness classification and readiness use one server-owned JIT policy; never rely only on a prior sweeper mutation.
- Refresh known state periodically through the adapter/discovery boundary; cached adapters must not receive a synthetic fresh timestamp.
- Load the actual Home Assistant mapping and align the EV mapping with the entities discovered in the running instance.
- Keep unavailable Matter evidence truthful. If node 5 is obsolete, remove or quarantine it through an explicit supported configuration/API operation; never delete Docker volumes or silently convert it to available.
- Keep all agent-facing and physical execution paths fail-closed.
- Do not modify KNX Virtual configuration in this change.

## Task 1 — Add RED tests for the four live failures

1. Add unit/integration tests proving a snapshot with old `observed_at` but a recent active receipt is classified according to the single freshness policy, while a cached adapter read with an old receipt remains stale.
2. Add a test proving `StateService.get_state` and `health.readyz` return the same freshness classification and reason codes for the same store contents.
3. Add a runtime composition test proving the periodic refresher updates stable live-read sources and does not rejuvenate cached Matter/Zigbee state.
4. Add configuration tests proving the Home Assistant mapping path is loaded by `runtime_factory`, the actual EV entity IDs are mapped, and battery dispatch routes remain disabled without the exact dispatch binding/profile.
5. Add Matter tests proving an unavailable node remains unavailable and is excluded from global readiness only when explicitly classified as optional; an active/required unavailable node keeps readiness degraded.

## Task 2 — Implement one JIT freshness/readiness authority

1. Introduce a server-owned freshness policy/evaluator that considers the runtime receipt of the latest evidence and preserves source observation age for audit and physical predicates.
2. Make `StateStore` expose the same effective classification used by application state reads and health; include deterministic reason codes for expired, unavailable, invalid, and optional-source conditions.
3. Update `StateService` to use that evaluator without mutating persisted state merely to answer a read.
4. Update `/readyz` to consume the same report and to refuse readiness when required freshness is degraded; optional unavailable sources must be reported but must not mask a healthy required path.

## Task 3 — Add periodic source refresh with lifecycle ownership

1. Add a validated settings value for the refresh interval, with a bounded default derived from the stale-after policy.
2. Add a dedicated application refresher runner owned by `RuntimeLifecycle`; it refreshes through the existing discovery/adapter boundary at a bounded cadence and records failures as source-unavailable evidence.
3. Serialize refresh against discovery/event state updates where necessary and avoid audit storms or unbounded task creation.
4. Wire the runner from `runtime_factory` and expose its liveness/last-refresh metadata for diagnostics.

## Task 4 — Repair Home Assistant mapping and lab configuration

1. Update the EV mapping document to the actual entity IDs observed from Home Assistant.
2. Ensure the lab environment references the mapping document without printing or replacing the existing token.
3. Ensure runtime creation passes the mapping and only exposes battery writes when the exact dispatch binding, profile, and qualification requirements are present.
4. Add an explicit configuration validation/error path for a mapping that advertises writable battery routes without a matching dispatch binding.

## Task 5 — Reconcile Matter node availability without data loss

1. Capture the live Matter node identity/availability evidence and inspect the compose configuration and persisted node ownership without deleting volumes.
2. If node 5 is a stale lab pairing, use the supported Matter Server removal/quarantine mechanism or an explicit optional-node setting, recording the decision in the lab configuration and audit evidence.
3. If node 5 is a real required source, leave it unavailable and make readiness correctly degrade with a reason code; do not manufacture state.
4. Restart only the affected containers if required, then verify that node 7 remains available and the gateway still sees the expected node set.

## Task 6 — Verify the complete composition

1. Run focused RED-to-GREEN tests, then unit, contract, integration, composition, lint/type/import checks, and documentation/schema checks required by `AI_WORKFLOW.md`.
2. Run the live lab smoke path without changing KNX Virtual and without deleting Home Assistant, Matter, or other persistent volumes.
3. Refresh Graphify after structural edits and run the repository system-composition review/check.
4. Inspect `git diff` and `git status`, separating only this change from the pre-existing dirty worktree. Do not commit or push.

## Stop condition

Stop only after the four failures have regression tests, the runtime has one coherent freshness/readiness decision, HA mapping produces the expected canonical battery/EV capabilities, Matter availability is truthful and explicitly classified, and all available verification gates pass. If a live external dependency remains unavailable, report the exact evidence and keep the code fail-closed rather than claiming full lab success.

## Verification notes — 2026-08-31

- The live runtime uses the combined Home Assistant mapping and keeps the
  existing persistent lab volumes. Battery and EV telemetry resolve to their
  semantic capabilities; battery actuator commands remain hidden without the
  exact dispatch binding and qualification.
- Matter Server's persisted stale node 5 was removed with the supported
  `remove_node` operation after verifying it was not the current virtual
  device; node 7 remains available. No Docker volume or ETS/KNX configuration
  was deleted or changed.
- Live inventory reconciliation now runs independently of state polling. It
  removes persisted source references from adapters no longer present in the
  authoritative discovery snapshot, so historical fixture devices cannot
  block readiness after a gateway restart.
- `/readyz` and `StateService` use the same JIT receipt-age projection. A
  source that only returns cached state is allowed to age to `STALE`; the
  runtime must then become not-ready instead of rejuvenating the evidence.
- A partial composite read now reconciles disconnected child adapters into
  source-specific `UNAVAILABLE` state immediately.
- Gateway shutdown is cancellation-safe: the lifecycle cleanup completes and
  releases runtime ownership before an interrupt propagates to the CLI.
- Final full gate: `1447 passed, 15 skipped`; Import Linter and architecture
  contracts kept; Ruff, mypy, runtime contract docs and `git diff --check`
  clean. The live multi-adapter composition smoke passed (`1 passed`), as did
  the live battery HTTP/Modbus/HA and KNX Virtual readback/command smokes
  (`1 passed` each) against the running lab. One existing Testcontainers
  deprecation warning remains.
