# Exclusive Physical Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the durable physical-intent ledger grant at most one dispatch owner for each household/idempotency-key pair, so a competing plan cannot invoke the physical adapter.

**Architecture:** Add a small domain claim result and an in-flight state. The repository performs the insert/inspection atomically, writes the owner and execution attempt beside the existing fencing epoch, and settles only through an ownership-checked update. The executor becomes the sole consumer of the claim decision and calls the adapter only for the acquired decision; storage is released before adapter I/O.

**Tech Stack:** Python 3.12, Pydantic v2, SQLite, PostgreSQL via psycopg, pytest/pytest-asyncio, Ruff, mypy, Import Linter and the repository composition checks.

**Spec:** `specs/207-exclusive-physical-dispatch/spec.md` and `docs/superpowers/specs/2026-09-10-exclusive-physical-dispatch-design.md`

## Global Constraints

- Preserve the uniqueness boundary `(household_id, idempotency_key)`.
- A different `plan_id` or `command_id` for an existing key is a non-retryable conflict.
- The adapter is called only after a successful durable dispatch claim.
- An interrupted physical operation remains ambiguous and must not be automatically retried.
- Owner, execution attempt and fencing epoch are non-secret identifiers; credentials and authorization material never enter records, diagnostics or audit payloads.
- SQLite and PostgreSQL must expose equivalent claim and settlement behavior.
- Runtime behavior without a configured physical-intent ledger remains unchanged.
- Preserve unrelated working-tree changes; do not commit, push, reset, merge or rewrite history.

---

### Task 1: Define the exclusive dispatch contract

**Files:**
- Modify: `src/domoai/domain/coordination.py:70-105`
- Modify: `src/domoai/runtime/ports.py:93-106`
- Modify: `tests/contract/test_phase3_multihost_contract.py`
- Create: `tests/unit/domain/test_physical_intent_claim.py`

**Interfaces:**
- Produces `PhysicalIntentStatus.DISPATCHING` with the serialized optional fields `owner_id` and `execution_attempt_id`.
- Produces `PhysicalIntentClaimDecision` with values `ACQUIRED`, `REPLAY`, `IN_FLIGHT` and `CONFLICT`.
- Produces immutable `PhysicalIntentClaim(decision: PhysicalIntentClaimDecision, intent: PhysicalIntent)`.
- Changes `PhysicalIntentPort.claim_for_dispatch(intent: PhysicalIntent) -> PhysicalIntentClaim` and `settle(*, intent: PhysicalIntent, status: PhysicalIntentStatus) -> PhysicalIntent`.

- [ ] **Step 1: Write the failing contract and model tests**

Add tests that assert the new status serializes as `dispatching`, a dispatching
intent requires non-empty `owner_id` and `execution_attempt_id`, terminal and
legacy intents may omit those fields, and the port annotations expose the new
claim/settle signatures. Keep the existing secret-safety assertions and extend
them to the new identifiers.

```python
def test_dispatching_intent_requires_owner_and_attempt() -> None:
    with pytest.raises(ValueError):
        PhysicalIntent(
            tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a",
            idempotency_key="intent-a", plan_id="plan-a", command_id="command-a",
            fencing_epoch=1, status=PhysicalIntentStatus.DISPATCHING,
            created_at=NOW, updated_at=NOW,
        )


def test_dispatching_intent_round_trips_ownership() -> None:
    intent = PhysicalIntent(
        tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a",
        idempotency_key="intent-a", plan_id="plan-a", command_id="command-a",
        fencing_epoch=1, status=PhysicalIntentStatus.DISPATCHING,
        owner_id="owner-a", execution_attempt_id="attempt-a",
        created_at=NOW, updated_at=NOW,
    )
    assert PhysicalIntent.model_validate_json(intent.model_dump_json()) == intent
```

- [ ] **Step 2: Run the contract tests and verify the expected RED failure**

Run:

```bash
uv run pytest -q tests/unit/domain/test_physical_intent_claim.py tests/contract/test_phase3_multihost_contract.py
```

Expected: FAIL because the dispatching status, ownership fields and new claim
contract do not yet exist.

- [ ] **Step 3: Implement the minimal domain and port contract**

Add the enum and frozen claim result in `domain/coordination.py`. Add optional
bounded ownership fields to `PhysicalIntent`; validate that both are present
for `DISPATCHING` and allow them to remain absent on legacy `PREPARED` or
`UNKNOWN` records. Update `PhysicalIntentPort` to expose only
`claim_for_dispatch()` and the intent-bound `settle()` signature.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/unit/domain/test_physical_intent_claim.py tests/contract/test_phase3_multihost_contract.py
```

Expected: PASS with no secret-safety regression.

### Task 2: Implement the atomic repository claim and schema migration

**Files:**
- Modify: `src/domoai/persistence/coordination.py:14-115`
- Modify: `src/domoai/persistence/postgres_schema.sql:211-245`
- Modify: `src/domoai/persistence/postgres.py:16-120`
- Create: `src/domoai/persistence/migrations/020_exclusive_physical_dispatch.sql`
- Modify: `tests/integration/test_phase3_multihost_ledger.py`
- Modify: `tests/composition/test_phase3_external_postgres.py`

**Interfaces:**
- `PhysicalIntentRepository.claim_for_dispatch(intent)` opens one write transaction, inserts a `DISPATCHING` owner when the key is absent, and returns `PhysicalIntentClaim`.
- Existing identity comparison uses tenant, household, deployment, key, plan and command; owner, attempt and fencing epoch do not redefine logical command identity.
- `PhysicalIntentRepository.settle(intent=claimed_intent, status=...)` updates only a matching `household_id`, key, owner, execution attempt, fencing epoch and `DISPATCHING` status.

- [ ] **Step 1: Write failing SQLite race, decision and settlement tests**

Extend `tests/integration/test_phase3_multihost_ledger.py` with two independent
SQLite connections over the same database file. Add cases for one acquired and
one conflict result across different plans, one acquired and one in-flight
result for the same logical command, terminal replay without mutation, stale
settlement not overwriting the winner, and `DISPATCHING` recovery to `UNKNOWN`.

```python
async def test_distinct_plan_claims_have_one_owner(tmp_path: Path) -> None:
    first_db = await _database(tmp_path / "ledger.sqlite3")
    second_db = await _database(tmp_path / "ledger.sqlite3")
    try:
        first, second = await asyncio.gather(
            PhysicalIntentRepository(first_db).claim_for_dispatch(_intent(plan="plan-a")),
            PhysicalIntentRepository(second_db).claim_for_dispatch(_intent(plan="plan-b")),
        )
        assert sorted((first.decision.value, second.decision.value)) == [
            "acquired", "conflict"
        ]
        assert await PhysicalIntentRepository(first_db).count() == 1
    finally:
        await first_db.close()
        await second_db.close()
```

- [ ] **Step 2: Run the repository tests and verify the expected RED failure**

Run:

```bash
uv run pytest -q tests/integration/test_phase3_multihost_ledger.py
```

Expected: FAIL because the repository has no explicit decision, ownership
columns or `dispatching` schema state.

- [ ] **Step 3: Add the additive SQLite and PostgreSQL schema support**

Create migration `020_exclusive_physical_dispatch.sql` that rebuilds the
SQLite `physical_intents` table with the `dispatching` check value and nullable
`owner_id`/`execution_attempt_id` columns, copies existing payload/status/
fencing/timestamps, preserves the primary key and recreates the status index.
Update `postgres_schema.sql` with the new columns/status and idempotent ALTER
steps for an existing PostgreSQL table. Advance the PostgreSQL schema marker
only after the complete bootstrap succeeds.

- [ ] **Step 4: Implement the atomic claim decision**

In `PhysicalIntentRepository.claim_for_dispatch()`:

1. Begin the existing write transaction (`BEGIN IMMEDIATE`, adapted to
   PostgreSQL).
2. Read the row by household/key inside that transaction.
3. If absent, write the candidate as `DISPATCHING` with owner, attempt and
   epoch, commit and return `ACQUIRED`.
4. If logical identity differs, commit without writing and return `CONFLICT`.
5. If status is `ACKNOWLEDGED`, `CONFIRMED` or `REJECTED`, return `REPLAY`.
6. If status is `DISPATCHING`, `PREPARED` or `UNKNOWN`, return `IN_FLIGHT`
   without granting dispatch ownership.

Ensure every branch leaves a clean transaction boundary and returns the stored
intent, never the losing caller's candidate.

- [ ] **Step 5: Implement ownership-checked settlement**

Change `settle()` to accept the claimed intent and execute one conditional
`UPDATE` requiring the stored owner, execution attempt, fencing epoch and
`DISPATCHING` status. If the update affects zero rows, read and return the
current durable intent without overwriting it. Preserve idempotent terminal
settlement behavior and keep owner/attempt fields for audit/reconciliation.

- [ ] **Step 6: Run repository tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/integration/test_phase3_multihost_ledger.py tests/contract/test_phase3_multihost_contract.py
```

Expected: PASS for SQLite. Run the PostgreSQL composition test separately; it
must pass when Docker is available and be skipped only for the repository's
existing Docker-unavailable condition.

### Task 3: Gate executor dispatch on the winning claim

**Files:**
- Modify: `src/domoai/application/executor.py:86-90,449-532,625-723`
- Modify: `tests/unit/runtime/test_executor_correlation.py`
- Modify: `tests/integration/test_plan_lifecycle.py`
- Create: `tests/integration/test_exclusive_physical_dispatch.py`

**Interfaces:**
- The executor creates a `PhysicalIntent` with `owner_id=fencing_token.owner_id` and the current `execution_attempt_id`.
- `ACQUIRED` is the only claim decision that sets `physical_intent` as a settlement owner or calls `adapter.execute()`.
- `REPLAY` creates an acknowledgement from the stored status without an adapter call.
- `IN_FLIGHT` becomes a non-dispatching `UNKNOWN` outcome; `CONFLICT` becomes a non-retryable rejected outcome using `ErrorCode.DUPLICATE_COMMAND`.

- [ ] **Step 1: Write failing executor/composition tests**

Use a recording adapter and two executor invocations sharing a durable
physical-intent ledger. Pause the first adapter call with an `asyncio.Event` so
the second invocation reaches the claim while the first is in flight. Add a
different-plan case that asserts one adapter call and a duplicate-plan case
that asserts no second call. Add replay-after-ACK and stale-settlement cases.

```python
async def test_two_distinct_plans_share_one_physical_dispatch() -> None:
    first, second = await asyncio.gather(executor_a.execute(plan_a), executor_b.execute(plan_b))
    assert adapter.execute_calls == [plan_a.commands[0]]
    assert {result.status for result in (*first.outcomes, *second.outcomes)} == {
        ExecutionStatus.CONFIRMED_SUCCESS, ExecutionStatus.REJECTED
    }
    assert any(
        outcome.error is not None
        and outcome.error.code is ErrorCode.DUPLICATE_COMMAND
        for outcome in (*first.outcomes, *second.outcomes)
    )
```

- [ ] **Step 2: Run the executor tests and verify the expected RED failure**

Run:

```bash
uv run pytest -q tests/unit/runtime/test_executor_correlation.py tests/integration/test_exclusive_physical_dispatch.py
```

Expected: FAIL because the executor still treats every returned `PREPARED`
intent as dispatchable.

- [ ] **Step 3: Implement decision handling at the repository boundary**

Construct the owned intent after the fencing token is available. Consume the
claim decision before setting `dispatch_attempted` or entering adapter I/O.
Keep the stored intent only for `ACQUIRED`; use a separate local replay value
so an in-flight or conflicting caller cannot attempt settlement. Map conflict
and in-flight cases to explicit sanitized outcomes and do not change the
existing no-ledger path.

- [ ] **Step 4: Preserve the existing ACK/readback lifecycle for the winner**

On `ACQUIRED`, retain the current adapter call, immediate ACK/REJECT settlement
and final CONFIRMED/REJECTED/UNKNOWN settlement, but pass the claimed intent to
the new ownership-checked `settle()` method. On `REPLAY`, synthesize the same
accepted/rejected acknowledgement from the stored terminal status and avoid
all adapter calls. Ensure metrics count only real adapter dispatches.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/unit/runtime/test_executor_correlation.py tests/integration/test_plan_lifecycle.py tests/integration/test_exclusive_physical_dispatch.py
```

Expected: PASS, including existing fencing, plan lifecycle, replay and
no-ledger regressions.

### Task 4: Update lab harness and cross-backend repository callers

**Files:**
- Modify: `src/domoai/lab/multihost_host.py:193-235`
- Modify: `tests/unit/lab/test_multihost_host.py`
- Modify: `tests/composition/test_phase3_external_postgres.py`
- Modify: `tests/integration/test_phase3_multihost_ledger.py`
- Modify: `tests/unit/persistence/test_repository_clock.py`
- Modify: `tests/unit/persistence/test_postgres_backend.py`

**Interfaces:**
- The lab `physical_intent` action uses `claim_for_dispatch()` and reports the decision value in its safe response.
- The lab only probes/settles after `ACQUIRED`; `crash_after_claim` exits after the durable `DISPATCHING` claim.
- Existing PostgreSQL and clock-injection tests use the new intent-bound settlement contract.

- [ ] **Step 1: Write/update failing caller contract tests**

Replace direct `claim()` assumptions with assertions for `ACQUIRED`, `REPLAY`,
`IN_FLIGHT` and `CONFLICT`. Add a lab test proving a crash-after-claim leaves
`DISPATCHING` evidence, and assert the PostgreSQL schema SQL contains the new
status and owner columns.

- [ ] **Step 2: Run caller tests and verify the expected RED failure**

Run:

```bash
uv run pytest -q tests/unit/lab/test_multihost_host.py tests/unit/persistence/test_postgres_backend.py tests/composition/test_phase3_external_postgres.py
```

Expected: FAIL because these callers still invoke the removed ambiguous API or
expect `PREPARED` to be claimable.

- [ ] **Step 3: Migrate the lab and all remaining callers**

Generate a fresh execution attempt identifier for each lab request, set the
token owner on the intent, branch on the claim decision, and pass the acquired
intent to `settle()`. Update fixtures and clock tests to assert the new
dispatching state while keeping terminal replay and recovery expectations.

- [ ] **Step 4: Run caller tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/unit/lab/test_multihost_host.py tests/unit/persistence/test_postgres_backend.py tests/integration/test_phase3_multihost_ledger.py tests/composition/test_phase3_external_postgres.py
```

Expected: PASS, with PostgreSQL skipped only when Docker is unavailable.

### Task 5: Close recovery, composition and project gates

**Files:**
- Modify: `src/domoai/persistence/coordination.py:89-112`
- Modify: `specs/207-exclusive-physical-dispatch/quickstart.md`
- Create: `specs/207-exclusive-physical-dispatch/data-model.md`
- Create: `specs/207-exclusive-physical-dispatch/contracts/physical-dispatch.md`
- Create: `specs/207-exclusive-physical-dispatch/research.md`
- Create: `specs/207-exclusive-physical-dispatch/composition-review.md`
- Modify: `docs/auditoria-codigo-2026-09-09/02-concurrencia-idempotencia-recuperacion.md`

**Interfaces:**
- `recover_inflight()` includes `DISPATCHING` and changes it to `UNKNOWN` without clearing owner, attempt or epoch evidence.
- The quickstart proves the P1 acceptance scenarios using deterministic SQLite and optional PostgreSQL.
- The composition review records ordering, ownership, retry/replay, failure propagation, schema compatibility and residual `REC-001` risk.

- [ ] **Step 1: Add failing recovery and documentation checks**

Add the `DISPATCHING` recovery assertion to the integration suite and validate
that the contract/quickstart mention the four claim decisions, the no-adapter
condition for conflict/in-flight/replay and the ambiguity of crash recovery.

- [ ] **Step 2: Run the checks and verify the expected RED failure**

Run:

```bash
uv run pytest -q tests/integration/test_phase3_multihost_ledger.py
```

Expected: FAIL until recovery recognizes `DISPATCHING` and the new Spec Kit
artifacts document the final contract.

- [ ] **Step 3: Update recovery and completion artifacts**

Include `dispatching` in the recovery query and CAS update. Write the research,
data model, repository contract and quickstart from the already approved
design, then update the audit report status only after executable tests prove
that two different plans cannot both dispatch.

- [ ] **Step 4: Run the focused acceptance suite**

Run:

```bash
uv run pytest -q tests/unit/domain/test_physical_intent_claim.py tests/unit/runtime/test_executor_correlation.py tests/integration/test_phase3_multihost_ledger.py tests/integration/test_plan_lifecycle.py tests/integration/test_exclusive_physical_dispatch.py
```

Expected: PASS.

- [ ] **Step 5: Run project architecture, composition and verification gates**

Run:

```bash
uv run ruff check .
uv run mypy src
uv run lint-imports
project-composition-check "$(cat .ai/project-name)"
```

Then run `system-composition-review` against the final diff, refresh Graphify
for the changed executor/repository path, review `git diff` and `git diff
--check`, and use `verification-before-completion` before claiming the finding
closed. Record any unavailable Docker/PostgreSQL check accurately.

## Execution Order

Task 1 is the contract foundation. Task 2 depends on it and blocks executor
changes. Task 3 depends on the repository decision and is the safety-critical
MVP. Task 4 updates all non-executor callers. Task 5 is the completion gate.

The test files in Task 2 and Task 3 can be drafted independently, but execution
is intentionally sequential because each RED test must fail against the prior
boundary and each GREEN cycle must remain clean before moving downstream.

## Stop Condition

Stop after `EXEC-001` is proven closed by the focused suite, composition checks,
final diff review and verification gate. Do not implement `AUTH-001`, `REC-001`,
`EVT-001` or later audit findings in this plan.
