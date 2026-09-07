# Fase 2 completa — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` or an equivalent task-by-task workflow to implement this plan.

**Goal:** Convert the existing safe runtime primitives into a complete, portable Phase 2 agentic product surface.

**Architecture:** Add read-only scenario comparison beside the proposal-only optimizer; add a scene facade that builds an ordered validated bundle and delegates all mutation to `BundleCommitService`; publish Skills v4 from the existing validator; certify two MCP clients through the existing gateway/session boundaries.

**Tech Stack:** Python 3.12, Pydantic, FastMCP/MCP, SQLite repositories, pytest, Ruff, mypy, Import Linter, project-composition.

**Spec:** `specs/190-phase2-complete/spec.md`

## Global Constraints

- Preserve unrelated dirty-worktree changes.
- No new physical executor and no adapter/vendor/solver calls from Skills.
- Keep proposal tools read-only; mutation requires existing auth, approval, admission and digest checks.
- Physical commissioning remains an external gate; local tests must fail closed when evidence is absent.

## Task 1 — Public scenario comparison

**Files:** `src/domoai/optimizer/counterfactual.py`, `src/domoai/mcp/ortools_server.py`, `schemas/v1/product-summary.schema.json`, contract/integration tests.

1. Add an async bounded-worker comparison path reusing counterfactual diff semantics.
2. Register `compare_scenarios` as a read-only structured MCP tool.
3. Validate all scenarios against the canonical registry and cap variation count.
4. Return typed baseline/variation summaries and no fabricated diff for failures.
5. Add malformed-input, infeasible-baseline and two-client parity tests.

## Task 2 — Safe scene facade

**Files:** `src/domoai/domain/product.py` or a focused scene model, `src/domoai/mcp/domotics_server.py`, schemas, tests.

1. Define a bounded scene request with ordered validated plan members and scene digest.
2. Verify every plan, runtime revision, authority scope and validation digest.
3. Route to `BundleCommitService.commit`; never call an adapter or bypass admission.
4. Preserve idempotency by scene/bundle digest and expose member readback status.
5. Add rejection tests for stale revision, missing approval, mismatched digest and replay.

## Task 3 — Portable agent Skills

**Files:** `skills/core/*`, `src/domoai/skills/catalog.py`, README/docs, contract tests.

1. Add the audited EV, thermal comfort, solar, night and vacation procedures.
2. Keep every Skill on the v4 validator and existing MCP operation bindings.
3. Add explicit stale-state, no-alarm-disarm, no-fabricated-solar, approval and UNKNOWN rules.
4. Expand catalog tests and ensure forbidden direct routes remain enforced.

## Task 4 — Client and HIL qualification evidence

**Files:** `tests/integration/test_mcp_gateway_http.py`, parity/composition tests, `docs/evidence/`.

1. Test two authenticated clients with distinct identities and read-only/mutate scopes.
2. Test disconnect/reconnect, timeout and duplicate request behavior at the MCP boundary.
3. Prove both clients observe one catalog and one runtime revision without shared request authority.
4. Add a fail-closed evidence report for physical commissioning/HIL prerequisites.

## Task 5 — Verification

1. Run focused contract/unit/integration tests.
2. Run architecture/import and documentation checks.
3. Run full pytest and `project-composition-check "$(cat .ai/project-name)"`.
4. Refresh Graphify and perform system-composition review.
5. Review diff and mark all task artifacts complete.
