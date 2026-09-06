Use the `system-composition-review` skill for the current repository.

Review the current significant change as a composed system, not as isolated features.

Use Graphify for the impact surface where useful, inspect source code, run the available architecture checks, contract tests, integration/composition tests and normal regression tests, and use real dependencies via Testcontainers when mocks may hide behavior.

Pay special attention to shared state, transactions, events, ordering, retries, idempotency, timeouts, error propagation, caches, concurrency, resource lifecycle, API/data contracts, configuration propagation, database invariants, migrations and backward compatibility.

If anything fails, use `systematic-debugging` and find the root cause before proposing a fix.

Finish with the Composition Review Report required by the skill.
