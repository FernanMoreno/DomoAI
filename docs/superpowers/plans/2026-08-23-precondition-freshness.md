# Plan: Physical Precondition Freshness

1. Establish red unit and composition tests for all snapshot statuses.
2. Introduce a single typed freshness evaluator and explicit stale opt-in.
3. Route preflight, JIT, scheduler and MCP through the evaluator.
4. Preserve provenance through projected state and record stale exceptions.
5. Verify with full tests, architecture gate and system-composition review.

Stop condition: no physical write may occur when required evidence is not current or when an explicit exception lacks server policy authorization.

## Closure notes (2026-08-29)

Superseded, not implemented from this outline. Already executed end-to-end
via `specs/118-precondition-freshness/` (21/21 tasks). `FreshnessEvaluator`
exists at `src/domoai/runtime/freshness.py`, wired into
`src/domoai/application/executor.py`. This outline is a duplicate planning
artifact left behind uncommitted; no new code written against it.
