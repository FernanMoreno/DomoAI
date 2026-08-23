# Terminal Plan Immutability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent terminal plan resurrection and duplicate physical execution through immutable intent and guarded lifecycle persistence.

**Architecture:** Keep PlanService as lifecycle admission authority and make PlanRepository enforce the same transition graph durably. The executor retains its atomic claim, while MCP validation and persistence stop terminal rewrites before claim time.

**Tech Stack:** Python 3.12, Pydantic v2, SQLite, FastMCP, pytest, ruff, mypy.

**Spec:** `specs/117-terminal-plan-immutability/spec.md`

## Global Constraints

- Terminal states are irreversible.
- UNKNOWN is terminal and non-replayable.
- No rejected lifecycle request may call an adapter.
- Existing plan ids remain compatible.
- Tests must fail before production code changes.
- Real SQLite persistence must be used for restart/CAS scenarios.

---

### Task 1: Capture terminal resurrection regression

**Files:** Modify `tests/integration/test_plan_lifecycle.py`; test `tests/contract/test_domotics_mcp_contract.py`.

- [ ] Write a test that validates, executes and settles a plan as COMPLETED, restarts against the same SQLite file, calls validation with the same id, and asserts terminal behavior with zero adapter writes.
- [ ] Run `uv run pytest -q tests/integration/test_plan_lifecycle.py -k terminal_revalidation_after_restart -vv`; expected result is a failure caused by validation persisting READY over COMPLETED.
- [ ] Add a contract test for same id with changed command and assert typed identity conflict.
- [ ] Run the focused contract test and confirm it fails for the missing immutable digest.

### Task 2: Add immutable definition evidence

**Files:** Modify `src/domoai/domain/models.py` and `src/domoai/application/plan_service.py`; test `tests/unit/application/test_plan_service.py`.

- [ ] Write digest tests showing changes to command value, unit, target, dependency or capability fingerprint change the digest while validation expiry does not.
- [ ] Run the focused digest tests and confirm they fail because the API is absent.
- [ ] Implement deterministic canonical JSON plus SHA-256 definition evidence on the validated plan.
- [ ] Run the focused digest tests and confirm they pass.

### Task 3: Enforce guarded lifecycle persistence

**Files:** Modify `src/domoai/persistence/repositories.py` and `src/domoai/domain/transitions.py`; test `tests/unit/persistence/test_plan_repository.py`.

- [ ] Write tests for terminal-to-ready rejection, identical duplicate idempotence, changed-definition rejection and concurrent CAS.
- [ ] Run the focused repository tests and confirm they fail because generic save permits replacement.
- [ ] Implement lifecycle-specific operations with expected status/version predicates and terminal protection.
- [ ] Run the focused repository tests and confirm they pass.

### Task 4: Route MCP validation through lifecycle authority

**Files:** Modify `src/domoai/mcp/domotics_server.py`, `src/domoai/application/facade.py` and `src/domoai/application/plan_service.py`; test `tests/contract/test_domotics_mcp_contract.py`.

- [ ] Extend contract assertions for terminal plans, changed definitions and identical duplicate requests.
- [ ] Run the contract tests and confirm they fail until MCP uses guarded operations.
- [ ] Resolve persisted identity before save, compare definition evidence and use the guarded validation operation.
- [ ] Run the contract tests and confirm they pass.

### Task 5: Verify restart and composition

**Files:** Modify `src/domoai/runtime/executor.py` only if needed; test `tests/integration/test_plan_lifecycle.py` and `tests/integration/test_persistence_lifecycle.py`.

- [ ] Run the focused lifecycle suite and confirm zero adapter writes for terminal duplicates.
- [ ] Run `uv run lint-imports && uv run ruff check . && uv run mypy src`; record pre-existing architecture failures separately.
- [ ] Run `uv run pytest -q -m 'not composition'` and the SQLite restart walkthrough.
- [ ] Review `git diff --check` and inspect all changed source/test/spec files without cleaning unrelated worktree changes.

## Dependencies and Execution Order

- Task 1 blocks Tasks 2-5.
- Task 2 precedes Task 3.
- Task 3 precedes Task 4.
- Task 4 precedes Task 5.

## Implementation Strategy

Red test, immutable evidence, repository CAS, MCP routing, then restart/concurrency/architecture/composition verification.
