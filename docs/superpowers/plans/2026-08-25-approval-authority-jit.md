# Approval Authority JIT Implementation Plan

> **For agentic workers:** Execute this plan task-by-task with TDD and review the composition boundary after the focused tests.

**Goal:** Prevent an already `APPROVED` persisted plan from crossing the physical execution boundary when its approval evidence is expired, altered, or outside its bundle scope.

**Architecture:** Keep approval evidence immutable on the approved `Plan`. `PlanService` validates server-owned lifetime, validation digest, execution-window digest, and schedule revision at execution time. `ExecutionAdmission` validates the persisted bundle scope before the executor claims the plan. The MCP `dry_run` path remains validation-only and does not consume or persist authority.

**Tech Stack:** Python 3.12, Pydantic v2 domain models, async SQLite repositories, pytest/pytest-asyncio, existing MCP and runtime composition.

**Spec:** `specs/158-physical-authority-program/blocks/02-approval-authority.md`

## Global Constraints

- All physical execution paths MUST pass the server-owned admission boundary.
- Approval and validation lifetimes MUST be server/trusted-host-owned evidence.
- `dry_run` MUST NOT mutate approval, plan, bundle, schedule, persistence, or authority audit state.
- Existing uncommitted changes are user-owned; modify only the focused B02 paths.
- `dev/lab/` MUST NOT be modified.
- Backward-compatible persisted plans without `approval_id` MUST fail closed when they require confirmation.

---

## File map

- Modify `src/domoai/domain/models.py`: persist the opaque approval identifier as part of approval evidence.
- Modify `src/domoai/application/plan_service.py`: enforce approval lifetime and temporal evidence during `assert_executable`; include `approval_id` when converting a grant to plan evidence.
- Modify `src/domoai/application/execution_admission.py`: enforce persisted bundle approval scope for aggregate-owned execution.
- Modify `schemas/v1/plan.schema.json`: regenerate the canonical schema after the model change.
- Modify `tests/unit/application/test_plan_service.py`: prove expired/tampered approval evidence is rejected before execution.
- Modify `tests/unit/application/test_execution_admission.py` or create it if absent: prove bundle scope cannot be bypassed.
- Modify `tests/unit/domain/test_models.py`: prove approval identifier round-trips and legacy missing identifiers remain parseable.
- Modify `tests/integration/test_plan_lifecycle.py`: prove the executor makes zero adapter calls for expired approved evidence.
- Modify `tests/contract/test_domotics_mcp_contract.py` only if the public error envelope or serialized approval shape changes.

### Task 1: Add the failing authority-evidence tests

**Files:**
- Modify: `tests/unit/application/test_plan_service.py`
- Modify: `tests/unit/application/test_execution_admission.py` (create if absent)
- Modify: `tests/integration/test_plan_lifecycle.py`

**Interfaces:**
- Consumes: current `Approval`, `PlanService.assert_executable`, `ExecutionAdmission.admit`, and `PlanExecutor.execute`.
- Produces: regression tests that fail against the current gap without changing production behavior.

- [X] **Step 1: Add a test for an expired persisted approval**

  Build a validated confirmation plan, issue and convert its grant to an approved plan, advance a `FixedClock` past `approval.expires_at`, and assert `assert_executable` raises `DomainError` with `ErrorCode.APPROVAL_ASSERTION_EXPIRED`.

- [X] **Step 2: Add a test for altered window evidence**

  Build an approved scheduled plan, replace its execution window or schedule revision without changing the validation digest, and assert execution is rejected with `ErrorCode.APPROVAL_REQUIRED`.

- [X] **Step 3: Add a test for missing approval identifier**

  Build a confirmation plan in `APPROVED` status with legacy approval evidence that has no identifier and assert the plan is rejected before any adapter call.

- [X] **Step 4: Add a test for bundle approval scope**

  Build a bundle member whose persisted approval digest is different from the bundle aggregate digest and assert aggregate-owned admission rejects it with no executor call.

- [X] **Step 5: Run only the new tests and verify RED**

  Run:

  ```bash
  uv run pytest tests/unit/application/test_plan_service.py -k 'approval or window' -q
  uv run pytest tests/unit/application/test_execution_admission.py -q
  uv run pytest tests/integration/test_plan_lifecycle.py -k 'approved or approval' -q
  ```

  Expected: the new tests fail because the current implementation does not enforce the persisted approval lifetime/identifier/scope at this boundary.

### Task 2: Persist immutable approval identity and enforce lifetime

**Files:**
- Modify: `src/domoai/domain/models.py`
- Modify: `src/domoai/application/plan_service.py`
- Modify: `schemas/v1/plan.schema.json`
- Test: `tests/unit/domain/test_models.py`
- Test: `tests/unit/application/test_plan_service.py`

**Interfaces:**
- Consumes: `ApprovalGrant.approval_id`, `ValidationResult.valid_until`, `Plan.execution_window`, and `Plan.schedule_revision`.
- Produces: `Approval.approval_id: str | None`, populated by `PlanService.approve`, and a fail-closed `assert_executable` check for confirmation plans.

- [X] **Step 1: Add the minimum model field**

  Add an optional `approval_id` to `Approval` so old v1 JSON can still be loaded. Do not make legacy data executable: the execution guard will reject missing identifiers for confirmation plans.

- [X] **Step 2: Add the failing checks to `PlanService.assert_executable`**

  For `ValidationStatus.REQUIRES_CONFIRMATION`, require:

  ```text
  approval.status == approved
  approval.approval_id is not None
  approval.validation_digest == validation.digest
  approval.validation_valid_until == validation.valid_until
  approval.expires_at is None or now < approval.expires_at
  approval.window_digest == current execution_window digest
  approval.schedule_revision == plan.schedule_revision
  ```

  Raise `APPROVAL_ASSERTION_EXPIRED` for expired approval/validation lifetime and `APPROVAL_REQUIRED` for missing or mismatched scope.

- [X] **Step 3: Populate the identifier when approving**

  Set `Approval.approval_id = grant.approval_id` in `PlanService.approve` and include it in the existing audit payload without exposing the bearer token or assertion secret.

- [X] **Step 4: Regenerate the plan schema**

  Run the repository schema export command and inspect that only the expected approval field is added to the v1 plan schema.

- [X] **Step 5: Run the focused unit tests and verify GREEN**

  Run:

  ```bash
  uv run pytest tests/unit/domain/test_models.py tests/unit/application/test_plan_service.py -q
  ```

  Expected: all approval evidence tests pass and unrelated plan validation behavior remains unchanged.

### Task 3: Enforce bundle scope at the common admission boundary

**Files:**
- Modify: `src/domoai/application/execution_admission.py`
- Modify: `tests/unit/application/test_execution_admission.py`
- Test: `tests/unit/runtime/test_bundle_commit.py`

**Interfaces:**
- Consumes: `BundleCommitRepository.get_for_plan`, `BundleCommit.bundle_digest`, `Plan.approval.bundle_digest`, and the existing `aggregate_owner` flag.
- Produces: a single fail-closed check that rejects a bundle member when approval scope is absent or differs from the live aggregate digest.

- [X] **Step 1: Implement scope comparison after bundle membership is resolved**

  For a bundle member, require an approved confirmation plan to carry the exact current `bundle_digest`. For an aggregate-owned plan with a confirmation approval whose bundle digest does not match, raise `APPROVAL_REQUIRED` before predecessor evaluation or physical claim.

- [X] **Step 2: Preserve non-confirmation and legacy-safe behavior**

  Plans that do not require confirmation keep the existing admission behavior. Direct execution of any member remains forbidden regardless of approval scope.

- [X] **Step 3: Run admission and bundle tests**

  Run:

  ```bash
  uv run pytest tests/unit/application/test_execution_admission.py tests/unit/runtime/test_bundle_commit.py -q
  ```

  Expected: direct member execution remains rejected and aggregate-owned members now reject mismatched approval scope.

### Task 4: Prove composed execution and dry-run immutability

**Files:**
- Modify: `tests/integration/test_plan_lifecycle.py`
- Modify: `tests/contract/test_domotics_mcp_contract.py` only if required by the serialized shape.

**Interfaces:**
- Consumes: MCP `execute_plan`, `PlanRepository`, `ApprovalStore`, `PlanExecutor`, and a recording adapter.
- Produces: cross-boundary proof that expired/tampered authority never reaches the adapter and dry-run remains read-only.

- [X] **Step 1: Add an expired-approved-plan composition test**

  Persist an approved plan, advance the shared clock beyond its approval expiry, call the real facade/executor path, and assert zero adapter calls plus the expected domain error.

- [X] **Step 2: Add a dry-run snapshot test**

  Capture plan status, approval evidence, grant consumption, repository payload and audit count; invoke MCP `execute_plan(..., dry_run=True)` and assert all captured authority state is unchanged.

- [X] **Step 3: Run the composed tests**

  Run:

  ```bash
  uv run pytest tests/integration/test_plan_lifecycle.py -k 'approval or dry_run' -q
  uv run pytest tests/contract/test_domotics_mcp_contract.py -k 'execute_plan or approval' -q
  ```

  Expected: no physical adapter call occurs for invalid authority and the public error envelope remains stable.

### Task 5: Composition and release verification

**Files:**
- No production files; inspect the final diff and generated schema.

- [X] **Step 1: Run focused static checks**

  ```bash
  uv run ruff check src/domoai/application src/domoai/domain src/domoai/runtime tests/unit/application tests/unit/runtime
  uv run mypy src/domoai/application src/domoai/domain src/domoai/runtime
  uv run lint-imports
  uv run python scripts/check_architecture_contracts.py
  ```

- [X] **Step 2: Run regression and composition tests**

  ```bash
  uv run pytest tests/unit tests/contract -q --disable-warnings --maxfail=1
  uv run pytest tests/integration/test_plan_lifecycle.py tests/unit/runtime/test_bundle_commit.py -q --disable-warnings --maxfail=1
  ```

- [X] **Step 3: Perform the composition review**

  Review MCP → plan persistence → approval evidence → execution admission →
  claim → executor → adapter for expiration, scope mismatch, duplicate request,
  restart/legacy evidence, and dry-run. Record residual recurrence-digest risk
  as the next B09 task rather than silently claiming it closed.

- [X] **Step 4: Review the final diff**

  Confirm `dev/lab/` is unchanged, no token or assertion material appears in
  tests/logs/schemas, and only B02 files plus focused tests/docs changed.

## Dependencies

Task 1 → Task 2 → Task 3 → Task 4 → Task 5.

Tasks 2 and 3 can be reviewed independently after Task 1's RED tests, but the
composed execution proof in Task 4 depends on both.

## Stop condition

Stop after Task 5. Do not begin B03 freshness, B04 battery supervision, or
recurrence JIT propagation in this slice; they require separate specs and
separate composition gates.

## Closure notes (2026-08-29)

Investigation before implementing found most of this plan's scope already
implemented and tested from earlier session work: approval expiry
enforcement, validation/window/schedule-revision matching in
`PlanService.assert_executable`, bundle-scope enforcement in
`ExecutionAdmission.admit` (`tests/unit/application/
test_execution_admission.py::test_bundle_admission_rejects_approval_scoped_to_another_bundle`),
and dry-run non-mutation (`tests/contract/
test_domotics_mcp_contract.py::test_execute_plan_dry_run_does_not_consume_or_approve_authority`)
were already correct and covered. The genuine remaining gap was narrower
than the plan implied: `Approval` had no `approval_id` field at all, so a
persisted approval record could never be told apart from a legacy one with
no identifier.

Closed this session: added `Approval.approval_id: str | None` (domain
model, additive/backward-compatible — old persisted JSON rows load with
`None`), populated it in `PlanService.approve`, and added the fail-closed
check in `assert_executable` rejecting confirmation-required plans whose
approval lacks an identifier. Added regression tests for the two remaining
untested scenarios (missing identifier — genuinely RED before the fix;
schedule/window tampering after approval — confirmed already-GREEN,
documents existing correct behavior) plus one new composition-level test
proving zero adapter calls through the real `PlanExecutor` path for an
expired approval. Regenerated `schemas/v1/plan.schema.json` (only file
affected by this change). `system-composition-review` verdict: PASS — only
one production site ever constructs `Approval` (`PlanService.approve`), so
the new check cannot reject any legitimate non-legacy flow.

Evidence: full suite `1333 passed, 14 skipped, 104.03s`; Ruff, mypy,
`lint-imports`, architecture-contract tests (`3 passed`) all clean. Changes
confined to B02 files per the Global Constraints (`dev/lab/` untouched by
this task). Uncommitted on `feat/composition-safety-gaps`.

