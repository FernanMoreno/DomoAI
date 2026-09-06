# Approval Authority JIT Implementation Plan

**Status:** Implemented — verified 2026-09-03

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every persisted confirmation approval verifiable against the server-authoritative grant store immediately before physical execution.

**Architecture:** Keep `ApprovalStore` as the sole issuer and verifier of approval grants. Add a consumed-grant verification path that compares the persisted `Plan.approval` projection with the authoritative grant, then invoke it from the already shared `ExecutionAdmission` after bundle scope is resolved and before the executor claims the plan.

**Tech Stack:** Python 3.12, Pydantic domain models, SQLite approval persistence, pytest/asyncio, Import Linter.

**Spec:** `specs/158-physical-authority-program/blocks/02-approval-authority.md`

## Global Constraints

- `valid_until` and approval expiry remain server/trusted-host-owned.
- `dry_run` remains non-consuming and non-mutating.
- A persisted `APPROVED` plan must retain and revalidate its complete authority scope.
- No lab fixture or physical KNX/HA configuration is modified by this change.
- Existing legacy token behavior remains limited to explicitly enabled local/development compatibility.

### Task 1: Add authoritative consumed-grant verification

**Files:**
- Modify: `src/domoai/runtime/approval_store.py`
- Modify: `src/domoai/persistence/repositories.py`
- Test: `tests/unit/runtime/test_approval_store.py`
- Test: `tests/unit/persistence/test_approval_grant_repository.py`

**Interfaces:**
- Produces `ApprovalStore.verify_consumed(plan, bundle_digest=None, recurrence_digest=None) -> ApprovalGrant`.
- Produces `ApprovalGrantPersistence.is_consumed_sync(approval_id: str) -> bool`.

- [x] **Step 1: Write the failing tests**

  Add tests proving that a persisted approved-plan projection without a matching consumed authoritative grant is rejected, that a valid consumed grant survives a repository restart, and that every persisted approval field is compared with the grant.

- [x] **Step 2: Run the focused tests and verify the expected red failure**

  Run:

  ```bash
  uv run pytest -q tests/unit/runtime/test_approval_store.py tests/unit/persistence/test_approval_grant_repository.py
  ```

  Expected: failure because the consumed-grant verification interface does not yet exist.

- [x] **Step 3: Implement the minimal verification path**

  Factor grant loading and binding checks so both pending validation and consumed verification use the same plan/digest/window/schedule/bundle/recurrence checks. Add the persistence status query, require the grant to be consumed for JIT execution, reject expired grants, and compare the persisted `Approval` projection against the authoritative `ApprovalGrant` including identity, session, scope, lifetime, and identifiers.

- [x] **Step 4: Run the focused tests and verify green**

  Run the same command and require all tests to pass without warnings.

### Task 2: Enforce approval verification at the physical admission boundary

**Files:**
- Modify: `src/domoai/application/execution_admission.py`
- Modify: `src/domoai/application/runtime_factory.py`
- Modify: `tests/unit/application/test_execution_admission.py`
- Add or modify: `tests/composition/test_physical_authority_closure_composition.py`

**Interfaces:**
- `ExecutionAdmission(..., approval_store: ApprovalStore | None = None)` verifies approved confirmation plans after resolving their bundle.
- Runtime construction creates one `ApprovalStore` before the executor/admission graph and injects that same instance into `ExecutionAdmission`.

- [x] **Step 1: Write the failing admission/composition tests**

  Add a test for a forged `APPROVED` plan and a test for an approved plan whose persisted bundle/scope metadata differs from its authoritative consumed grant. Assert the adapter boundary is never reached and the error is `approval_required` or `approval_assertion_expired` as appropriate.

- [x] **Step 2: Run the focused tests and verify red**

  Run:

  ```bash
  uv run pytest -q tests/unit/application/test_execution_admission.py tests/composition/test_physical_authority_closure_composition.py
  ```

  Expected: the new tests fail because admission currently only inspects the plan projection and does not consult `ApprovalStore`.

- [x] **Step 3: Implement the admission wiring**

  Resolve the committed bundle first, derive the expected bundle scope, require an approval store for confirmation-required approved plans, and call `verify_consumed` before returning an admission decision. Construct the store before `ExecutionAdmission` in `runtime_factory.py`; reuse it for MCP and `BundleCommitService`.

- [x] **Step 4: Run focused unit and composition tests**

  Require the new tests plus the existing bundle, dry-run, recurrence, and approval tests to pass:

  ```bash
  uv run pytest -q tests/unit/application/test_execution_admission.py tests/unit/runtime/test_approval_store.py tests/unit/persistence/test_approval_grant_repository.py tests/contract/test_domotics_mcp_contract.py tests/composition/test_physical_authority_closure_composition.py
  ```

### Task 3: Verify all execution routes and document the closed contract

**Files:**
- Modify: `tests/contract/test_domotics_mcp_contract.py`
- Modify: `tests/composition/test_temporal_consent_composition.py`
- Modify: `specs/158-physical-authority-program/blocks/02-approval-authority.md`
- Modify: `specs/158-physical-authority-program/spec.md`
- Modify: `docs/contracts.md`

- [x] **Step 1: Add cross-route expiry and restart scenarios**

  Cover direct MCP execution of a persisted approved plan, scheduler execution, bundle execution, replay after restart, and repeated dry-run. Verify no adapter call and no authority mutation on rejection or dry-run.

- [x] **Step 2: Run architecture and composition gates**

  Run:

  ```bash
  uv run pytest -q
  uv run ruff check .
  uv run mypy src
  uv run lint-imports
  git diff --check
  ```

- [x] **Step 3: Mark only proven requirements complete**

  Update the B02 status and verification text only after the tests prove the acceptance scenarios. Record any remaining limitations instead of claiming production qualification.

- [x] **Step 4: Run the project composition review**

  The project-wide composition gate and a manual cross-subsystem review were
  completed on 2026-09-03 after the subsequent runtime blocks were integrated.
  `project-composition-check "$(cat .ai/project-name)"` passed with 466 tests,
  18 skips, 1 known deprecation warning and Import Linter 4 kept/0 broken.
  The convenience wrapper `project-composition-review domoai codex` currently
  fails before launching the reviewer because it supplies the Codex sandbox and
  approval flags twice; that launcher defect is recorded rather than treated as
  a repository failure.

## Verification record

- Focused authority/admission/HIL suite: `77 passed`.
- Full suite: **1582 passed, 18 skipped, 1 warning**.
- The warning is the existing Testcontainers `wait_for_logs` deprecation.
- No `dev/lab/` fixture or KNX/Home Assistant configuration was modified by
  this plan.

- Latest composition evidence: `project-composition-check "$(cat
  .ai/project-name)"` — **466 passed, 18 skipped, 1 warning**; Import Linter
  **4 kept, 0 broken**.
- The complete regression command was run with temporary uv cache locations:
  `UV_CACHE_DIR=/tmp/domoai-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/domoai-uv-python
  uv run pytest -q` — **1582 passed, 18 skipped, 1 warning**.

  Run `project-composition-check domoai` and the configured `project-composition-review domoai codex` command, then inspect the final diff and report residual risks.
