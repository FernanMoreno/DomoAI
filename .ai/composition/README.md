# System Composition Quality

This project uses a composition-aware completion gate.

A significant feature/refactor must be reviewed beyond its isolated feature tests.

Required review surface:

1. Changed subsystem(s).
2. Immediate upstream/downstream neighbors.
3. Shared state and persistence invariants.
4. Transaction/rollback/compensation behavior.
5. Events/messages and ordering.
6. Retries/idempotency/duplicates.
7. Timeouts/cancellation/error propagation.
8. Cache invalidation and stale state.
9. Concurrency and resource lifecycle.
10. API/schema/data contracts.
11. Configuration/environment propagation.
12. Migrations and backward compatibility.

Use:

- Graphify for structural impact and dependency/call-path discovery.
- Architecture-contract tools for forbidden/layer/cycle rules.
- Testcontainers for real disposable infrastructure when mocks could hide behavior.
- Pact for consumer/provider contracts when independently evolving APIs/services/events exist.
- Superpowers `systematic-debugging` when a failure appears.
- Superpowers `verification-before-completion` before declaring success.
- `system-composition-review` for the complete cross-subsystem review.

Suggested test directories:

- `tests/composition/`
- `tests/integration/`
- `tests/contracts/`

Do not create tests merely to satisfy directory names. Test actual boundaries and business invariants.
