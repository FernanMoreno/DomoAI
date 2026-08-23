# Plan: Physical Precondition Freshness

1. Establish red unit and composition tests for all snapshot statuses.
2. Introduce a single typed freshness evaluator and explicit stale opt-in.
3. Route preflight, JIT, scheduler and MCP through the evaluator.
4. Preserve provenance through projected state and record stale exceptions.
5. Verify with full tests, architecture gate and system-composition review.

Stop condition: no physical write may occur when required evidence is not current or when an explicit exception lacks server policy authorization.
