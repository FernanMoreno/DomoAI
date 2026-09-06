# Phase 3B multi-host coordination foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add provider-neutral lease/fencing, idempotent physical intent, bounded household work and instance-aware history while keeping production multi-host disabled without an external coordinator.

**Architecture:** A typed coordination port issues monotonic lease epochs per tenant/household/deployment. A fail-closed guard is checked immediately before each adapter write and the epoch travels in `ExecutionContext`. SQLite stores additive intent/history records; a deterministic coordinator and disposable databases prove races and recovery without pretending to be a production distributed service.

**Tech Stack:** Python 3.12, Pydantic v2, SQLite/WAL, asyncio, pytest, Ruff, mypy, Import Linter, Graphify.

**Spec:** `specs/191-phase3-multihost-foundation/spec.md`

## Global Constraints

- Preserve legacy v1 payload loading and the current single-writer ownership path.
- Do not add Redis, etcd, Postgres, Kubernetes clients or remote services.
- Missing, expired, lost or stale fencing must reject before an adapter call.
- Intent replay must be durable and idempotent by household plus `idempotency_key`.
- Queue and metric cardinality must remain bounded.
- Production settings must reject multi-host mode without an external provider.
- No commit, push, release or remote mutation.

### Task 1: Coordination domain contract

**Files:**
- Create: `src/domoai/domain/coordination.py`
- Create: `src/domoai/application/coordination.py`
- Test: `tests/unit/domain/test_coordination.py`
- Test: `tests/unit/application/test_coordination.py`

**Interfaces:**
- Produces `LeaseScope`, `FencingToken`, `LeaseState`, `LeaseCoordinator` and `FencingViolation`.
- Produces `DeterministicLeaseCoordinator.acquire()`, `.renew()`, `.release()` and `.validate()` for tests.
- A token contains `tenant_id`, `household_id`, `deployment_id`, `owner_id`, positive `epoch`, `lease_id`, `issued_at` and `expires_at`.

- [x] Write tests for same-scope race, monotonic takeover, wrong-owner renewal, expiry and stale-token rejection.
- [x] Run `uv run pytest -q tests/unit/domain/test_coordination.py tests/unit/application/test_coordination.py`; observe expected missing-symbol failures.
- [x] Implement strict models, async protocol and deterministic coordinator with one lock per scope; never return a lower epoch.
- [x] Rerun the focused tests and confirm all coordination cases pass.

### Task 2: Execution fencing and context propagation

**Files:**
- Modify: `src/domoai/runtime/execution_context.py`
- Modify: `src/domoai/application/execution_admission.py`
- Modify: `src/domoai/application/executor.py`
- Test: `tests/unit/runtime/test_execution_context.py`
- Test: `tests/unit/application/test_execution_admission.py`
- Test: `tests/composition/test_phase3_multihost_fencing.py`

**Interfaces:**
- `ExecutionContext` gains optional `scope`, `fencing_epoch` and `lease_id` fields without changing old constructor calls.
- `FencingGuard.assert_writable(token)` validates the current coordinator state.
- `ExecutionAdmission` accepts an optional guard and rejects invalid authority before the adapter.

- [x] Add tests proving current epoch reaches the adapter context and old/missing/expired epoch makes adapter call count remain zero.
- [x] Run the focused tests and observe failure before production changes.
- [x] Add the optional context fields and guard check at the last safe boundary before `adapter.execute`.
- [x] Convert lease loss after claim into `UNKNOWN`/fail-closed outcome and preserve existing cleanup.
- [x] Run unit and composition tests, including replay of a stale host after takeover.

### Task 3: Durable physical intent and recovery ledger

**Files:**
- Create: `src/domoai/persistence/migrations/017_multihost_coordination.sql`
- Modify: `src/domoai/persistence/repositories.py`
- Create: `src/domoai/persistence/coordination.py`
- Test: `tests/integration/test_phase3_multihost_ledger.py`
- Test: `tests/contract/test_phase3_multihost_contract.py`

**Interfaces:**
- `PhysicalIntentStatus` has `prepared`, `confirmed`, `rejected`, `unknown`.
- `PhysicalIntent` stores household, idempotency key, plan/command, fencing epoch, status and timestamps.
- `PhysicalIntentRepository.claim()` is unique by household plus key; `.settle()` is idempotent; `.recover_inflight()` marks prepared rows unknown.

- [x] Write disposable-SQLite tests for duplicate claims, crash recovery, stale epoch metadata and idempotent settlement.
- [x] Run those tests and observe the missing migration/repository failure.
- [x] Add migration 017 and repository transactions with unique constraints and bounded payloads.
- [x] Wire the executor's physical dispatch to claim before adapter call and settle after acknowledgement/readback.
- [x] Run integration and contract tests; verify duplicate requests do not call the adapter twice.

### Task 4: Per-household bounded work queues

**Files:**
- Create: `src/domoai/application/household_queue.py`
- Modify: `src/domoai/application/runtime_factory.py`
- Modify: `src/domoai/config/settings.py`
- Test: `tests/unit/application/test_household_queue.py`
- Test: `tests/composition/test_phase3_multihost_queue.py`

**Interfaces:**
- `HouseholdWorkQueues(max_per_household, max_total)` exposes async `submit(household_id, work)`, `depths()` and `close()`.
- Submission returns a typed receipt or raises a bounded overload error; queues never share capacity silently.

- [x] Add tests for isolated overload, FIFO order, cancellation and clean shutdown.
- [x] Run focused tests and observe the missing queue failure.
- [x] Implement per-household semaphores/queues with explicit total admission and no unbounded task creation.
- [x] Attach the queue to the physical dispatch path only; keep solver and telemetry lanes separate.
- [x] Run queue and runtime composition tests and verify no leaked tasks.

### Task 5: Instance identity, history and metrics

**Files:**
- Create: `src/domoai/runtime/instance.py`
- Modify: `src/domoai/runtime/operational_metrics.py`
- Modify: `src/domoai/mcp/remote_metrics.py`
- Modify: `src/domoai/application/metrics.py`
- Modify: `src/domoai/persistence/repositories.py`
- Test: `tests/unit/runtime/test_instance_metrics.py`
- Test: `tests/integration/test_phase3_multihost_metrics.py`

**Interfaces:**
- `InstanceIdentity` exposes bounded `instance_id` and `process_start_time` only.
- `RuntimeOperationalMetrics.record_fencing(event)` and `.snapshot()` expose allowlisted coordination counters.
- `MetricHistoryRepository.append()` caps samples by count and age; no raw bearer or event payload is persisted.

- [x] Add tests for stable instance identity, bounded history, lease/fencing counters and secret-safe rendering.
- [x] Run focused tests and observe missing fields/repository behavior.
- [x] Implement identity generation/injection, counters and migration-backed bounded history.
- [x] Extend Prometheus allowlist with instance/process and coordination metrics without high-cardinality labels.
- [x] Run unit/integration metrics tests and inspect rendered output for secrets.

### Task 6: Fail-closed deployment gate

**Files:**
- Modify: `src/domoai/config/settings.py`
- Modify: `src/domoai/application/runtime_factory.py`
- Modify: `src/domoai/mcp/health.py`
- Modify: `docs/unified-mcp.md`
- Modify: `docs/evaluacion-coordinacion-distribuida-pools-metricas.md`
- Test: `tests/contract/test_phase3_multihost_settings.py`
- Test: `tests/composition/test_phase3_multihost_deployment.py`

**Interfaces:**
- Settings expose `instance_id`, `multi_host_enabled` and bounded queue/history limits.
- `build_runtime(..., lease_coordinator=None)` rejects multi-host mode without an external provider.
- Health reports `multi_host: disabled` rather than ready when no external coordinator exists.

- [x] Add tests for default single-writer compatibility and fail-closed multi-host startup.
- [x] Run focused settings/composition tests and observe failure.
- [x] Add configuration validation and dependency injection without selecting the deterministic test coordinator in production.
- [x] Update deployment/health documentation and keep active-active explicitly unsupported.
- [x] Run configuration and gateway tests.

### Task 7: Full verification and composition review

**Files:**
- Modify: `schemas/v1/*.schema.json` via `scripts/export_schemas.py`
- Modify: `docs/auditoria-fase-3-escalabilidad-identidad.md`
- Modify: `specs/191-phase3-multihost-foundation/tasks.md`

- [x] Run focused tests covering race, takeover, stale epoch, replay, crash, queue isolation and metrics history.
- [x] Run `uv run python scripts/export_schemas.py` and `uv run python scripts/check_runtime_contract_docs.py`.
- [x] Run `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` and the full `uv run pytest -q` suite.
- [x] Run `project-composition-check "$(cat .ai/project-name)"` and inspect the final diff plus `git diff --check`.
- [x] Refresh/query Graphify and perform the required composition review; record residual risk that no real external coordinator was used.
- [x] Mark all completed tasks and document that production active-active remains blocked.
