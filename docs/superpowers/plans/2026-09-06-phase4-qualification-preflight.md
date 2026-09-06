# Phase 4 qualification preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an honest, repeatable Phase 4 preflight that proves local software/process gates and records physical residuals as blocked.

**Architecture:** Add a strict domain report and a small lab orchestration module. Reuse `run_digital_twin()` and `LabRunner.smoke()` through injected callables, then render one validated report to JSON and Markdown. The preflight remains outside runtime authority and never invokes adapters.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Markdown, existing DomoAI lab CLI.

**Spec:** `specs/195-phase4-qualification-preflight/spec.md`

## Global Constraints

- Physical gates must remain `blocked_external_dependency` when hardware/provider dependencies are unavailable.
- The report must never contain secrets or `hardware` evidence scope.
- Existing `twin`, `smoke`, HIL and production readiness behavior remains compatible.
- No production files, credentials, remote endpoints or commits are introduced.

---

### Task 1: Strict preflight report contract

**Files:**
- Create: `src/domoai/domain/phase4_preflight.py`
- Create: `tests/contract/test_phase4_preflight_contract.py`

**Interfaces:**
- `Phase4Gate` validates one gate record.
- `Phase4PreflightReport` validates overall status, timestamps, gates, residuals and secret-safe details.
- `render_json()` returns stable JSON; Markdown is rendered by the lab module only after validation.

- [x] Step 1: Write failing tests for blocked physical gates, derived status and secret rejection.
- [x] Step 2: Run the contract tests and confirm the report types are absent.
- [x] Step 3: Implement strict enums/models and stable JSON serialization.
- [x] Step 4: Run the contract tests and confirm they pass.

### Task 2: Preflight orchestration and rendering

**Files:**
- Create: `src/domoai/lab/phase4_preflight.py`
- Create: `tests/unit/lab/test_phase4_preflight.py`

**Interfaces:**
- `run_phase4_preflight(seed, run_twin, run_process_smoke, clock)` returns `Phase4PreflightReport`.
- `render_preflight_markdown(report)` renders only typed report fields.
- The injected runners return a digital-twin evidence object and a process-smoke exit code.

- [x] Step 1: Write failing tests for software pass + physical blocked and software failure.
- [x] Step 2: Implement orchestration with stable gate IDs and residuals.
- [x] Step 3: Add sanitized JSON/Markdown rendering.
- [x] Step 4: Run unit tests and confirm deterministic output.

### Task 3: CLI command and evidence files

**Files:**
- Modify: `src/domoai/lab/cli.py`
- Create: `tests/unit/lab/test_phase4_preflight_cli.py`
- Modify: `docs/auditoria-fase-4-ventaja-diferencial.md`
- Create: `docs/evidence/phase4-preflight-latest.json`
- Create: `docs/evidence/phase4-preflight-latest.md`

**Interfaces:**
- `domoai-lab preflight --seed 187 --report PATH --json-report PATH` writes both formats.
- Existing commands keep their parser and exit behavior.

- [x] Step 1: Add parser tests for the new command and default paths.
- [x] Step 2: Wire the command to existing twin/process boundaries.
- [x] Step 3: Execute the preflight and record the actual local result.
- [x] Step 4: Update the Phase 4 audit with the evidence and residual links.

### Task 4: Verification and closure

**Files:**
- Modify: `specs/195-phase4-qualification-preflight/tasks.md`
- Modify: `docs/evidence/phase4-preflight-latest.md`

- [x] Step 1: Run focused tests, full pytest, Ruff, mypy and contract docs.
- [x] Step 2: Run Graphify and `project-composition-check "$(cat .ai/project-name)"`.
- [x] Step 3: Verify no physical evidence is marked passed and no secrets occur in artifacts.
- [x] Step 4: Record all errors in the Phase 4 bug ledger and mark completed tasks.

## Verification result — 2026-09-06

- Full repository regression after the lab-runner fix: 1906 passed, 18 skipped
  in 650.41s.
- Docker multi-host lab: 8/8 scenarios, 20/20 ownership races, exit 0.
- Composition gate: 530 passed, 18 skipped; 4 contracts kept, 0 broken.
- Ruff, mypy, runtime contract docs, shell syntax and `git diff --check` pass.
- Graphify structural refresh: 8279 nodes, 24629 links; semantic document
  extraction was unavailable without an API key.
