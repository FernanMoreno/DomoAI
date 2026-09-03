# Freshness and State Truth Implementation Plan

> **For agentic workers:** execute the checked tasks with tests first and run
> the composition gates before marking the block complete.

**Goal:** Make every physical precondition use server-owned observation age,
source availability and canonical multi-source state without cache
rejuvenation.

**Architecture:** Keep source observations in `StateStore` keyed by
`(device, capability, adapter, external_id)`, derive one canonical view with
conflict resolution, and let `FreshnessEvaluator` perform the final age/status
decision at JIT execution. `RuntimeEventConsumer` owns source-loss transitions;
reconnection/discovery is the only path that can restore a source.

**Spec:** `specs/158-physical-authority-program/blocks/03-freshness-state-truth.md`

## Constraints

- `observed_at` is when the source observed the value; `received_at` is the
  source/runtime receipt evidence. A later cache read cannot replace either
  timestamp when the source supplied it.
- `CURRENT` is valid only while its observed age is within the configured
  server-owned max age.
- `UNAVAILABLE` and `INVALID` never become authorized through stale policy.
- A source loss affects only that source; independent healthy sources remain
  usable and conflicts resolve fail-closed.
- `dev/lab/` and the live KNX/Home Assistant setup are not changed.

## Tasks

- [x] Map adapter, discovery, event-consumer, state-store and executor
  boundaries with Graphify and source inspection.
- [x] Add adversarial tests for old `CURRENT`, source timestamps, partial
  source loss, incremental conflict and distinct freshness decisions.
- [x] Preserve explicit HA and discovery source timestamps through the
  `AdapterPort` boundary.
- [x] Degrade only the source named by a structured composite failure and
  restore it only after reconnection/discovery.
- [x] Resolve incremental observations per source and fail closed on a
  conflicting canonical value.
- [x] Make freshness reasons explicit for expired, stale-not-allowed,
  unavailable, invalid and future observations.
- [x] Run full architecture, contract and composition gates; update the block
  status only after the final cross-subsystem review.

## Verification record so far

- Focused B03 tests: `55 passed` before the explicit reason-code additions;
  the new freshness tests and source-loss/conflict tests also pass (`10
  passed`).
- Full suite after the final B03 changes: `1379 passed, 15 skipped, 1 warning`.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed.
- `uv run lint-imports`: 4 contracts kept, 0 broken.
- `git diff --check`: passed.
- `scripts/check_architecture_contracts.py`: passed through the repository
  composition gate.
- The optional `project-composition-check` and
  `project-composition-review` executables are not installed in this WSL
  environment; the repository's configured architecture/composition script
  and the real MQTT Testcontainer composition tests were used instead.
