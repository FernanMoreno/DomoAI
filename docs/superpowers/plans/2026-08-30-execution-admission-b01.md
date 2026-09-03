# Execution Admission and Bundle Ownership Implementation Plan

**Status:** Implemented — verified 2026-09-03

This plan is the implementation record for B01. The source and test work is
present in the current `HEAD` (`d6c19e0`); the checklist below has been
reconciled against the code, tests and composition evidence. The B01 scope did
not modify KNX, ETS, Home Assistant or `dev/lab/` assets. This status does not
claim physical battery qualification or unattended actuator autonomy.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ExecutionAdmission` the single server-owned ownership boundary for execute, schedule, cancel and reschedule, so no generic MCP or direct scheduler path can mutate a bundle member outside `BundleCommitService`/scheduler aggregate ownership.

**Architecture:** Extend the existing `ExecutionAdmission` with an explicit operation discriminator and preserve its read-only decision model. MCP performs a pre-admission before any approval consumption, while `Scheduler` and `PlanExecutor` perform the final admission immediately before their durable CAS/claim. `BundleCommitService` retains aggregate ownership through the existing `aggregate_owner=True` execution path. All rejection decisions are audited by the admission boundary with bounded, credential-free payloads.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, SQLite repositories with CAS/transactions, pytest, Ruff, mypy, Import Linter, existing composition checks.

**Spec:** `docs/superpowers/specs/2026-08-30-execution-admission-design.md`

**Final evidence:** The exact commands and observed results are recorded in
the verification record below; historical red/green TDD runs are retained as
plan history and are not inferred from the current run.

## Global Constraints

- Do not modify KNX, ETS, Home Assistant, `dev/lab/`, adapters, or lab fixtures for this block.
- Do not change the bundle data model or predecessor list format; fan-in behavior already exists and must remain intact.
- Preserve existing public error codes and source compatibility for `admit(plan, aggregate_owner=...)` callers.
- Do not weaken approval, validation, scheduler CAS, executor claim, or adapter safety checks.
- Do not replace durable repository CAS/transactions with an in-memory lock.
- Keep `dry_run` read-only: no approval consumption, plan approval persistence, scheduler mutation, bundle mutation, executor claim, or adapter call.
- Do not commit, push, reset, clean, or overwrite unrelated user changes. The plan itself is intentionally left uncommitted.
- Every production-code edit must be preceded by a failing or regression-focused test, and every changed boundary must have a relevant unit/contract/composition check.

## Acceptance Criteria

- `AdmissionOperation` distinguishes `EXECUTE`, `SCHEDULE`, `CANCEL`, and `RESCHEDULE`.
- A generic bundle-member execute/schedule/cancel/reschedule is rejected by the same admission object with the mapped existing `ErrorCode`.
- A bundle aggregate owner can continue through the owner path; execute still requires every predecessor's `CONFIRMED_SUCCESS` evidence and bundle-scoped approval.
- `execute_plan(dry_run=True)` rejects bundle members before any approval or persistence mutation and does not mutate non-member authority either.
- MCP contains no duplicated `is_member()`/`is_scheduled_member()` ownership policy branches.
- Direct `Scheduler.schedule`, `Scheduler.cancel`, and `Scheduler.reschedule` cannot bypass bundle ownership when the runtime supplies the admission boundary.
- Rejection is audited centrally before the caller can reach a repository mutation or executor claim; audit payloads contain no credentials or unbounded intent.
- Existing unit, contract, integration, architecture, and composition gates pass without lab changes.

---

## Phase 1 — Establish the failing contract tests

### Task 1 — Add operation-aware admission unit tests

**Files:**

- Modify `tests/unit/application/test_execution_admission.py`.
- Use the existing `_BundleRepository`, bundle builders, `Plan`, and `ApprovalStore` fixtures already in this file.

**Steps:**

- [x] Import `AdmissionOperation` alongside `ExecutionAdmission`.
- [x] Add a compact bundle-member plan/bundle fixture or helper that can be reused by execute, schedule, cancel, and reschedule cases without duplicating timestamps and IDs.
- [x] Add a parameterized test for generic operations asserting these exact mappings:
  - `EXECUTE` → `BUNDLE_MEMBER_EXECUTION_FORBIDDEN`.
  - `SCHEDULE` → `BUNDLE_MEMBER_EXECUTION_FORBIDDEN`.
  - `CANCEL` → `BUNDLE_MEMBER_CANCEL_FORBIDDEN`.
  - `RESCHEDULE` → `BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN`.
- [x] Assert the default call `admit(plan, aggregate_owner=False)` remains equivalent to `EXECUTE`.
- [x] Add an owner-path test showing `aggregate_owner=True` does not trigger the generic membership error; retain predecessor/approval assertions by using the existing approved-plan helper where applicable.
- [x] Add an audit fake or `AuditLog` assertion that a rejection records operation, plan ID, bundle ID, and error code without approval/token fields.
- [x] Run only this test module and record the expected failure caused by the missing operation API before editing production code:

```bash
uv run pytest -q tests/unit/application/test_execution_admission.py
```

### Task 2 — Add scheduler direct-call regression tests

**Files:**

- Modify `tests/unit/runtime/test_scheduler.py` for the smallest direct scheduler cases.
- Modify the relevant scheduler fixture/builders in `tests/contract/test_domotics_mcp_contract.py` only where a bundle repository and admission are already present.

**Steps:**

- [x] Build a pending scheduled bundle member using the existing SQLite repositories.
- [x] Assert `Scheduler.cancel(member_id)` raises `BUNDLE_MEMBER_CANCEL_FORBIDDEN` and leaves the scheduled row pending.
- [x] Assert `Scheduler.reschedule(member_id, ...)` raises `BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN` and leaves the original execute time/revision unchanged.
- [x] Assert `Scheduler.schedule(member_plan)` raises `BUNDLE_MEMBER_EXECUTION_FORBIDDEN` for a generic caller.
- [x] Add a non-member regression showing scheduler behavior remains unchanged when the plan is not in a bundle.
- [x] Run the focused scheduler tests before implementation and verify the new tests fail for the current bypass.

### Task 3 — Add MCP ordering and no-mutation tests

**Files:**

- Modify `tests/contract/test_domotics_mcp_contract.py`.
- Extend the existing scheduler/bundle context so its `PlanExecutor`, `Scheduler`, and MCP facade share one `ExecutionAdmission` instance.

**Steps:**

- [x] Add/extend a test for `execute_plan` on a confirmation-required bundle member with `dry_run=True`; assert the response has the mapped bundle error, the grant is not consumed, the plan remains `REQUIRES_CONFIRMATION`, and the database has no approval transition.
- [x] Add schedule/cancel/reschedule contract cases that assert the mapped error code and verify the scheduled repository and bundle ledger are unchanged.
- [x] Preserve the existing temporal reschedule error for non-members (`RESCHEDULE_REQUIRES_REVALIDATION`).
- [x] Assert at least one central admission audit event is emitted for a generic bundle mutation and that its payload is bounded and credential-free.
- [x] Run the focused contract tests and confirm failures identify the missing central boundary rather than fixture errors.

---

## Phase 2 — Implement the operation-aware admission boundary

### Task 4 — Add the operation discriminator and centralized rejection helper

**Files:**

- Modify `src/domoai/application/execution_admission.py`.
- Use the existing `AuditLog` API from `src/domoai/runtime/events.py`; leave that module unchanged.

**Steps:**

- [x] Add `AdmissionOperation(StrEnum)` with values `execute`, `schedule`, `cancel`, and `reschedule`.
- [x] Add an optional `audit: AuditLog | None` constructor parameter while preserving existing constructor callers.
- [x] Change `admit` to accept `operation: AdmissionOperation = AdmissionOperation.EXECUTE` and retain `aggregate_owner` as a keyword argument.
- [x] Add a private operation-to-error mapping containing only the four existing bundle error codes; do not introduce a generic fallback that could silently authorize an unknown operation.
- [x] Add a private rejection helper that appends one `execution_admission_rejected` event before raising. Include only `operation`, `plan_id`, known `bundle_id`, `error_code`, and a bounded reason string. Never include approval IDs, tokens, assertion contents, full commands, or unbounded exception text.
- [x] Route every bundle-membership rejection through that helper.
- [x] Keep non-member behavior unchanged: bundle-scoped approvals still fail closed when bundle persistence is absent, and approved plans still require the authoritative consumed grant.
- [x] Keep predecessor fan-in checks and bundle approval digest checks on the execute/owner path. For `CANCEL` and `RESCHEDULE`, perform ownership admission without introducing an unrelated approval requirement; their existing repository/temporal semantics remain responsible for the next check.
- [x] Do not add a success audit event in this block; existing claim/outcome audit events remain the evidence for accepted execution. Avoid treating audit failure as authorization failure because `AuditLog` is intentionally non-blocking.
- [x] Run the unit admission and scheduler tests; fix only failures caused by this boundary.

### Task 5 — Expose the shared admission object through the application facade

**Files:**

- Modify `src/domoai/application/facade.py`.
- Modify `src/domoai/application/runtime_factory.py`.

**Steps:**

- [x] Add a read-only `DomoticsFacade.execution_admission` property returning the exact `PlanExecutor.execution_admission` instance, without creating a second admission object.
- [x] Construct one named `execution_admission` in `build_runtime` with the runtime `bundle_commit_repository`, `approval_store`, and `audit`.
- [x] Pass that object into `PlanExecutor` and `Scheduler` rather than constructing or discovering separate policy objects.
- [x] Ensure `BundleCommitService` continues to use the same facade/executor and its internal execute calls still pass `aggregate_owner=True`.
- [x] Keep fixture-only runtimes without an admission object valid for non-bundle tests; production/configured runtimes with bundle persistence must always receive the shared object.
- [x] Run the focused unit and runtime construction tests.

### Task 6 — Make Scheduler mutation methods admission-aware

**Files:**

- Modify `src/domoai/application/scheduler.py`.
- Update scheduler construction sites that already have the runtime admission, especially `src/domoai/application/runtime_factory.py` and bundle-aware test builders.

**Steps:**

- [x] Add an optional `execution_admission: ExecutionAdmission | None = None` keyword constructor parameter.
- [x] Change `schedule(plan)` to call `admit(plan, operation=AdmissionOperation.SCHEDULE, aggregate_owner=False)` before `ScheduledPlanRepository.schedule`; preserve `None` compatibility for isolated legacy fixtures.
- [x] Change `cancel(plan_id)` to read the pending scheduled record first. If absent, return `False` as today. If present, call `admit(stored_plan, operation=CANCEL, aggregate_owner=False)` and only then invoke the repository CAS cancel.
- [x] Change `reschedule(...)` to read the pending scheduled record first. If absent/non-pending, preserve the current `False` result. Before the repository revision/CAS call, call `admit(stored_plan, operation=RESCHEDULE, aggregate_owner=False)`; preserve generic temporal revalidation semantics at the MCP layer.
- [x] Do not pass `aggregate_owner=True` from generic methods. If an internal aggregate-owned caller ever uses these methods, make that explicit in the method keyword and cover it with a test; do not infer ownership from plan membership.
- [x] Keep `run_due` calling `PlanExecutor.execute(..., aggregate_owner=True)` and its existing predecessor gate/settlement path. The scheduler’s direct mutation methods and execution path must use the same admission instance.
- [x] Run scheduler unit, lifecycle, and composition tests.

---

## Phase 3 — Remove MCP policy duplication and close ordering gaps

### Task 7 — Add a shared MCP admission helper and remove membership branches

**Files:**

- Modify `src/domoai/mcp/domotics_server.py`.
- Modify `tests/contract/test_domotics_mcp_contract.py` and any direct MCP fixtures that need the shared admission object.

**Steps:**

- [x] Add a small helper that obtains `context.facade.execution_admission`; if bundle persistence is configured but the boundary is absent, fail closed with an existing safe domain error rather than falling back to `is_member()`.
- [x] Have `execute_plan`, `schedule_plan`, `cancel_scheduled_plan`, and `reschedule_plan` call the helper with the matching `AdmissionOperation` immediately after resolving the stored plan and before approval consumption or repository mutation.
- [x] Remove direct MCP calls to `bundle_commit_service.is_member()` and `is_scheduled_member()` as ownership policy checks.
- [x] Keep MCP-specific checks in place: client scope, validation digest, time parsing, exact execution window, approval-store validation/consumption, and non-member temporal revalidation response.
- [x] In `execute_plan`, perform admission before the `REQUIRES_CONFIRMATION` branch so a member dry-run cannot consume or project approval. For a non-member dry-run, validate the grant if supplied but do not consume it, approve the plan, or persist a plan transition; return the read-only projection.
- [x] Ensure `schedule_plan` admits before consuming confirmation authority, then lets `Scheduler.schedule` perform the final same-boundary admission before persistence.
- [x] Ensure `cancel_scheduled_plan` and `reschedule_plan` do not emit a second bespoke bundle error; the central admission event/error is authoritative. Retain the existing audit for non-member reschedule revalidation because it describes a different rule.
- [x] Run MCP contract, gateway, warning, and parity tests.

### Task 8 — Wire all configured runtime entry points to one admission instance

**Files:**

- Modify `src/domoai/application/runtime_factory.py`.
- Update only the bundle-aware test builders that must receive the shared object; do not modify `src/domoai/mcp/configured.py` or `src/domoai/mcp/stdio.py`.

**Steps:**

- [x] Verify the configured runtime creates exactly one `ExecutionAdmission` and that facade, executor, scheduler, and bundle service all reach that object.
- [x] Pass the shared object into `Scheduler` explicitly.
- [x] Keep minimal fixture contexts without bundle repositories functioning as before, while updating bundle-aware fixtures to avoid accidentally testing a bypass because `Scheduler.execution_admission` is `None`.
- [x] Add an identity assertion in a runtime composition test (`scheduler.execution_admission is facade.execution_admission is facade.executor.execution_admission`) without exposing internals through MCP.
- [x] Run the configured-runtime and composition smoke tests.

### Task 9 — Verify no physical or durable side effect occurs before rejection

**Files:**

- Modify `tests/composition/test_physical_authority_closure_composition.py`.
- Add only narrowly scoped test doubles/helpers in the test file or existing test fixture modules.

**Steps:**

- [x] Exercise the path `MCP/Facade → Scheduler or Executor → repository/adapter` for a bundle member and assert no adapter call, executor claim, scheduled-row transition, plan transition, approval consumption, or bundle ledger update occurs after generic rejection.
- [x] Cover a predecessor fan-in member with two predecessors and assert the owner execution remains blocked when either predecessor lacks confirmed-success evidence; keep the existing positive all-predecessors case.
- [x] Add a duplicate accepted-call case using existing repository/executor idempotency behavior and assert one durable/adapter effect, without adding an in-memory lock.
- [x] Assert audit evidence is emitted before the attempted mutation and remains serializable/bounded.
- [x] Run the focused composition tests with verbose output for failure diagnosis.

---

## Phase 4 — Documentation and artifact status

### Task 10 — Update the B01 specification and implementation records

**Files:**

- Modify `specs/158-physical-authority-program/blocks/01-execution-admission.md`.
- Modify the B01 status/checklist section in `specs/158-physical-authority-program/spec.md` only after all acceptance criteria pass.
- Do not add another implementation note; keep the approved design document as the design record and update only the B01 status artifacts after verification.

**Steps:**

- [x] Change B01 from Draft to Implemented/Verified only after tests and architecture checks pass.
- [x] Record the final operation mapping, shared-instance wiring, audit event name, and explicit residual boundary (freshness, dynamic safety, HIL, and actuator qualification remain separate work).
- [x] Record that no lab/KNX/HA assets were changed for B01.
- [x] Keep the status claim evidence-based: include the exact focused and gate commands used, not a hardcoded test count.

---

## Phase 5 — Verification, composition review, and handoff

### Task 11 — Run the full verification matrix

**Steps:**

- [x] Run focused unit/contract/composition tests first:

```bash
uv run pytest -q \
  tests/unit/application/test_execution_admission.py \
  tests/unit/runtime/test_scheduler.py \
  tests/contract/test_domotics_mcp_contract.py \
  tests/composition/test_physical_authority_closure_composition.py
```

- [x] Run static quality checks on changed Python files:

```bash
uv run ruff check src/domoai/application/execution_admission.py \
  src/domoai/application/facade.py \
  src/domoai/application/scheduler.py \
  src/domoai/application/runtime_factory.py \
  src/domoai/mcp/domotics_server.py
uv run mypy src/domoai/application src/domoai/mcp
```

- [x] Run the repository architecture/composition gate:

```bash
project-composition-check "$(cat .ai/project-name)"
```

- [x] Run the complete suite after focused checks pass:

```bash
uv run pytest -q
```

- [x] Review `git diff --check` and `git status --short`; confirm only intended B01 source/tests/spec changes were introduced and all pre-existing user changes remain intact.

### Task 12 — Perform the required cross-subsystem composition review

**Files/areas to review:** MCP tools, facade, admission, scheduler, bundle commit, executor, approval store, SQLite repositories, audit log, and composition tests.

**Steps:**

- [x] Use `system-composition-review` against the final diff and verify the call graph has no generic route that reaches a repository mutation or adapter claim without admission.
- [x] Confirm the review specifically checks ordering, failure propagation, retry/idempotency, direct application callers, configured runtime wiring, and real SQLite behavior.
- [x] Refresh the Graphify codebase view/query after the changes and verify the admission dependency edges and no forbidden layer imports.
- [x] Run the relevant Spec Kit consistency/checklist workflow for B01 if its existing artifacts require regeneration.
- [x] Preserve the implementation/result evidence durably in the repository
  records without credentials, bearer tokens or lab secrets. The
  `claude-obsidian:save` provider is unavailable in this environment, so no
  direct vault mutation was attempted; the repository plan and design record
  are the canonical handoff for this closure.
- [x] Use `verification-before-completion` before reporting completion; report commands and observed results, and explicitly list any residual failure rather than claiming success from partial tests.

## Stop Condition

Stop after the B01 acceptance criteria, verification matrix, composition review, and artifact status update are complete. Do not begin the next physical-authority backlog item in the same implementation pass; freshness, battery lease/control, HIL, and actuator qualification are separate blocks with their own specs and tests.

## Verification record — 2026-09-03

- Focused B01 regression:
  `UV_CACHE_DIR=/tmp/domoai-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/domoai-uv-python uv run pytest -q tests/unit/application/test_execution_admission.py tests/unit/runtime/test_scheduler.py tests/composition/test_physical_authority_closure_composition.py tests/contract/test_domotics_mcp_contract.py`
  — **101 passed**.
- Repository composition gate:
  `project-composition-check "$(cat .ai/project-name)"` — **466 passed, 18 skipped, 1 warning**; Import Linter **4 kept, 0 broken**.
- Cross-subsystem review: the configured Codex wrapper was attempted but its
  launcher currently supplies sandbox/approval flags twice and exits before
  starting the reviewer; the admission call graph, ordering, SQLite CAS,
  idempotency and composition scenarios were therefore reviewed directly from
  source and tests. This launcher defect does not affect the repository gate.
- Complete regression suite:
  `UV_CACHE_DIR=/tmp/domoai-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/domoai-uv-python uv run pytest -q`
  — **1582 passed, 18 skipped, 1 warning**.
- The warning is the existing Testcontainers `wait_for_logs` deprecation.
- The current worktree contains unrelated pre-existing changes; this
  reconciliation changed documentation only and preserved those changes.
- Residual boundary: B01 centralizes bundle ownership, but freshness, dynamic
  safety, approval lifetime, actuator authorization, battery supervision and
  HIL/production qualification remain separate gates.
