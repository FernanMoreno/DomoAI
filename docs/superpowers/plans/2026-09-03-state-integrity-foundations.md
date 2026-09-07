# State Integrity Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Close the first Phase 0 state-integrity slice: ordered source evidence, strict state truth and atomic durable StateStore publication.

**Architecture:** Keep the canonical state boundary in `domain/models.py` and `runtime/state_store.py`. Add optional cursor evidence for ordered providers, batch event application through `DiscoveryService`, and candidate-first persistence so memory changes only after the existing SQLite transaction succeeds. Preserve cursorless callers under an explicit unordered policy and add fresh-only diagnostics additively.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, SQLite, pytest, Ruff, mypy, Import Linter, existing composition tooling.

**Spec:** `specs/177-state-integrity-foundations/spec.md`

## Global Constraints

- Keep public schema version `v1` and make cursor/diagnostic fields additive.
- Do not modify unrelated existing worktree changes.
- No ordered stream may regress after old or duplicate evidence.
- Gap/epoch loss requires resync and must block state-dependent autonomous execution.
- Persistence failure must leave the previous canonical state and metadata active.
- Production code follows a failing test; verify every red/green cycle.

### Task 1: Domain contracts

**Files:**
- Modify: `src/domoai/domain/models.py`
- Test: `tests/unit/domain/test_models.py`

**Interfaces:**
- Produces `SourceOrderingPolicy`, `SourceCursor`, `StateSnapshot.source_cursor`, and event cursor fields consumed by later tasks.

- [x] **Step 1: Write the failing tests** for `SourceCursor` identity/sequence rules, event-level cursor decoding, finite float rejection, `CURRENT` requiring a value, `INVALID`/`UNAVAILABLE` requiring `None`, and cursor/source mismatch rejection in `tests/unit/domain/test_models.py`.
- [x] **Step 2: Run the domain tests to verify the failures** with `uv run pytest -q tests/unit/domain/test_models.py`; expected failures are missing cursor fields and missing snapshot invariants, while unrelated existing tests remain green.
- [x] **Step 3: Implement the minimal domain contracts** in `src/domoai/domain/models.py`: use strict non-empty strings, non-negative sequence, optional `resync`, optional snapshot cursor, event payload alias decoding, timezone checks, finite float checks and status/value validators.
- [x] **Step 4: Run the domain tests to verify green** with `uv run pytest -q tests/unit/domain/test_models.py`.

### Task 2: Ordered StateStore decisions

**Files:**
- Modify: `src/domoai/runtime/state_store.py`
- Test: `tests/unit/runtime/test_state_store.py`

**Interfaces:**
- Consumes `SourceCursor` from Task 1.
- Produces `StateStoreMetadata.source_cursors`, `source_ordering_policies`, `resync_required`, `StateStore.save_many()`, `StateStore.durability_status` and `StateStore.ordering_report()` for later tasks.

- [x] **Step 1: Write the failing tests** for first-cursor acceptance, old-after-new no-op, duplicate no-op without version increment, gap invalidation, epoch-change invalidation, explicit resync recovery, restart cursor restoration, and cursorless unordered reporting in `tests/unit/runtime/test_state_store.py`.
- [x] **Step 2: Run the StateStore tests to verify the failures** with `uv run pytest -q tests/unit/runtime/test_state_store.py`; expected failures show absent cursor metadata and absent batch method.
- [x] **Step 3: Implement cursor evaluation and candidate state** in `src/domoai/runtime/state_store.py`: add metadata defaults for backward-compatible constructors, evaluate all snapshots in one batch, ignore old/duplicate cursor batches, invalidate affected canonical snapshots on gap/epoch without `resync`, clear degradation on explicit resync, and keep cursorless saves under `unordered` policy.
- [x] **Step 4: Run the StateStore tests to verify green** with `uv run pytest -q tests/unit/runtime/test_state_store.py`.

### Task 3: Candidate-first durability and repository metadata

**Files:**
- Modify: `src/domoai/runtime/state_store.py`
- Modify: `src/domoai/persistence/repositories.py`
- Test: `tests/unit/runtime/test_state_store.py`
- Test: `tests/unit/persistence/test_runtime_state_repository.py`
- Test: `tests/composition/test_state_metadata_durability_composition.py`

**Interfaces:**
- Consumes Task 2 metadata and candidate state.
- Preserves `RuntimeStatePersistencePort.persist(snapshots, metadata)` and extends its serialized metadata without changing the method signature.

- [x] **Step 1: Write the failing durability tests** using a persistence double that raises: verify the old snapshot, version, cursor and metadata remain visible, `durability_status` becomes degraded, and restart reads the last committed SQLite state; add round-trip tests for cursor/policy/resync metadata.
- [x] **Step 2: Run the durability tests to verify the failures** with `uv run pytest -q tests/unit/runtime/test_state_store.py tests/unit/persistence/test_runtime_state_repository.py tests/composition/test_state_metadata_durability_composition.py`.
- [x] **Step 3: Implement candidate-first commit** in StateStore: build copied dictionaries and metadata, persist candidates first, install them only after success, retain old values on exception, and apply the same ordering to stale/unavailable/delete paths that share the persistence port.
- [x] **Step 4: Implement strict state serialization and tolerant legacy loading** in `repositories.py`: serialize new metadata, validate cursor payloads, preserve absent legacy fields as unordered, and pass `allow_nan=False` for state snapshots and runtime metadata.
- [x] **Step 5: Run the durability tests to verify green** with the same focused command.

### Task 4: Batch event propagation

**Files:**
- Modify: `src/domoai/application/discovery_service.py`
- Modify: `src/domoai/application/event_consumer.py`
- Test: `tests/integration/test_runtime_event_consumer.py`

**Interfaces:**
- Consumes `StateChangedEvent.source_cursor` and `StateStore.save_many()`.
- Produces one serialized state mutation per event batch, preserving the existing `save_state_snapshots()` return shape.

- [x] **Step 1: Write the failing integration tests** for one cursor covering multiple embedded capabilities, old event after new event, duplicate event idempotency, gap/epoch fail-closed behavior and event-level cursor propagation in `tests/integration/test_runtime_event_consumer.py`.
- [x] **Step 2: Run the integration tests to verify the failures** with `uv run pytest -q tests/integration/test_runtime_event_consumer.py`.
- [x] **Step 3: Wire batch persistence** in `DiscoveryService._save_state_snapshots_unlocked()` and `_event_snapshots()`: normalize all source-owned snapshots, attach an event cursor where an embedded item lacks one, then call `save_many()` once; preserve narrow no-read behavior for authoritative events.
- [x] **Step 4: Run the integration tests to verify green** with `uv run pytest -q tests/integration/test_runtime_event_consumer.py`.

### Task 5: Fresh-only structured diagnostics

**Files:**
- Modify: `src/domoai/application/state_service.py`
- Modify: `src/domoai/mcp/domotics_server.py`
- Modify: `src/domoai/mcp/resources.py`
- Test: `tests/unit/application/test_state_service.py`
- Test: `tests/contract/test_state_integrity_contract.py`

**Interfaces:**
- Produces `StateService.get_with_diagnostics()` while preserving `StateService.get()`.
- Extends MCP `get_state` with `diagnostics` and keeps the existing `states` array.

- [x] **Step 1: Write the failing tests** for `invalid` exclusion when `allow_stale=False`, structured diagnostic reason/status, unchanged default list behavior, and strict JSON response serialization.
- [x] **Step 2: Run the service/contract tests to verify the failures** with `uv run pytest -q tests/unit/application/test_state_service.py tests/contract/test_state_integrity_contract.py`.
- [x] **Step 3: Implement additive diagnostics** in `state_service.py` and `domotics_server.py`; classify effective stale, unavailable and invalid snapshots and return them in `diagnostics` without adapter I/O.
- [x] **Step 4: Make the state response serializer strict** in `resources.py` and any state-specific persistence path, using `allow_nan=False` while preserving current key ordering and schema version.
- [x] **Step 5: Run the service/contract tests to verify green** with the same focused command.

### Task 6: Schema/docs and cross-boundary verification

**Files:**
- Modify: `schemas/v1/` generated state schemas if the repository generator changes them
- Modify: `docs/contracts.md` only in the Phase 0 state-integrity section if required by the generated contract
- Test: `tests/composition/test_state_metadata_durability_composition.py`
- Test: `tests/contract/test_state_integrity_contract.py`

- [x] **Step 1: Add the real SQLite composition scenario** proving event batch → DiscoveryService → StateStore → SQLite metadata/snapshot → restart, including failure rollback and no replay regression.
- [x] **Step 2: Run the composition scenario to verify its failure or missing contract** with `uv run pytest -q tests/composition/test_state_metadata_durability_composition.py tests/contract/test_state_integrity_contract.py`.
- [x] **Step 3: Regenerate/check schemas and document the additive v1 contract** with `uv run python scripts/export_schemas.py`, then run `git diff --check -- schemas/v1/` and verify the generated diff is limited to the intended additive contract; update `docs/contracts.md` only if the repository convention requires it.
- [x] **Step 4: Run the complete feature verification** with `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run python scripts/check_architecture_contracts.py`, and `project-composition-check "$(cat .ai/project-name)"`.
- [x] **Step 5: Review the final diff and refresh Graphify** so the final report reflects the new state/event/persistence edges; do not stage or commit unrelated worktree files.

## Dependencies & Execution Order

- Task 1 precedes Tasks 2–5 because all consumers use the new domain evidence.
- Task 2 precedes Task 3 because persistence must store the cursor decisions.
- Task 2 precedes Task 4 because event batches call `save_many()`.
- Task 5 can proceed after Task 1 and may run in parallel with Task 4 once the service tests are independent.
- Task 6 follows Tasks 3–5 and is the completion gate.

## Stop Condition

Stop this slice when ordered replay/duplicate/gap/epoch scenarios, strict
state validation, fresh-only diagnostics, persistence rollback/restart and
the required architecture/composition gates are verified. Do not start A-002,
A-003, A-004, A-005 or A-006 in this plan.
