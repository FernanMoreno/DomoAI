# Runtime Physical Authority Closure Implementation Plan

## Goal

Close the audit findings that allow physical authority, state truth, or qualification scope to be lost between an agent request and an adapter write.

## Execution order

1. Add adversarial contract tests and evidence fields.
2. Implement central admission and bundle/approval/dry-run gates.
3. Implement source-preserving freshness and canonical resolution.
4. Implement battery authority/supervision and single readback persistence.
5. Implement optimizer/HIL bounds and honest qualification.
6. Finish governance/documentation/CI and run composition review.

## Work items

### A. Admission and authority

- Add `ExecutionAdmission` with evaluate/commit modes and structured decision reasons.
- Expand bundle members to all predecessors and move dependency checks into admission.
- Make direct execute, cancel, and reschedule reject bundle members.
- Persist approval scope, bundle/recurrence digest, and server expiry; make dry-run non-mutating.
- Add tests for direct dependent execution, fan-in, scope/lifetime, and dry-run database invariants.

### B. State truth

- Add a server-owned freshness policy and evaluate age at JIT validation/execution.
- Preserve Home Assistant source timestamps through mapper and adapter snapshot projection.
- Add source-keyed observations and canonical conflict/availability resolution; avoid global stale on one source failure.
- Add tests for expired current, HA timestamp preservation, partial failure, and incremental conflict.

### C. Battery and persistence

- Gate battery writes on explicit binding/qualification, independent of mapping routes.
- Route takeover through the binding provider under composite adapters.
- Add lease renew/assert/release/supervision and startup safe-stop reconciliation.
- Add dynamic battery/EV guards and ensure terminal SOC bounds.
- Remove executor double persistence and add tests for lease loss, crash no-replay/stop, composite routing, and one write path.

### D. Optimizer and HIL

- Add pre-worker horizon bounds.
- Enforce profile/deployment HIL power ceilings.
- Pass exact CLI binding into runtime and reject runtime/profile mismatch.
- Require takeover evidence, structured check status, and final stop/readback cleanup.
- Bind qualification to observed provider/hardware/firmware/profile/runtime/schema/freshness.

### E. Governance and release

- Add identity claim persistence and separate audit database configuration when isolation is required.
- Add recurrence-specific approval digest, monotonic risk override behavior, DST and stale/unavailable fixes, and audit limit validation.
- Remove hardcoded README test count and document required CI checks.

## Verification gates

- Focused failing tests before each implementation block.
- Unit/contract/integration/composition suites.
- Ruff, mypy, import-linter, schema/package checks, dependency audit where available.
- `scripts/composition_check.sh` and `project-composition-review . codex`.
- Final review explicitly records residual hardware/remote-governance evidence.

## Closure notes (2026-08-29)

Superseded, not implemented from this outline. Already executed end-to-end
via `specs/156-physical-authority-closure/` (48/48 tasks, independently
verified `[x]`). An Explore agent audit cross-checked every individual
bullet in sections A-E against real source and ran the relevant suites live
(not name-matching): all items implemented with passing tests, except one
cosmetic divergence — item A-1 describes `ExecutionAdmission` with separate
"evaluate/commit modes and structured decision reasons"; the actual
implementation uses a single `admit()` method with `DomainError(code,
message, details)` for rejections. The safety guarantee (single admission
boundary checked before claim) is real and tested either way; this is a
naming/API-shape gap in the plan's wording, not a functional gap, and does
not warrant a rewrite to match the outline literally.

Real finding worth keeping, not a code gap: this work (and the commits it
depends on being present) is entirely **uncommitted** — it sits in the
working tree on top of `8e03f26`. The commit log entries someone might
associate with this plan (`74d8687`, `61439f3`, `3389f7f`) predate it and
are not where this scope landed. Flagging so a future session doesn't
assume `git log` alone proves this shipped.
