import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.coordination import (
    PhysicalIntent,
    PhysicalIntentClaimDecision,
    PhysicalIntentStatus,
)
from domoai.persistence.coordination import PhysicalIntentRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _intent(key: str = "intent-1") -> PhysicalIntent:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return PhysicalIntent(
        tenant_id="tenant-a",
        household_id="home-a",
        deployment_id="gw-a",
        idempotency_key=key,
        plan_id="plan-1",
        command_id="command-1",
        fencing_epoch=3,
        status=PhysicalIntentStatus.PREPARED,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_duplicate_claim_returns_one_durable_intent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        first = await repository.claim_for_dispatch(
            _intent().model_copy(
                update={"owner_id": "owner-a", "execution_attempt_id": "attempt-a"}
            )
        )
        duplicate = await repository.claim_for_dispatch(
            _intent().model_copy(
                update={"owner_id": "owner-b", "execution_attempt_id": "attempt-b"}
            )
        )

        assert first.decision is PhysicalIntentClaimDecision.ACQUIRED
        assert duplicate.decision is PhysicalIntentClaimDecision.IN_FLIGHT
        assert first.intent.status is PhysicalIntentStatus.DISPATCHING
        assert await repository.count() == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_confirmed_replay_and_inflight_recovery_are_idempotent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        prepared = await repository.claim_for_dispatch(
            _intent("prepared").model_copy(
                update={"owner_id": "owner-a", "execution_attempt_id": "attempt-a"}
            )
        )
        confirmed = await repository.claim_for_dispatch(
            _intent("confirmed").model_copy(
                update={"owner_id": "owner-a", "execution_attempt_id": "attempt-b"}
            )
        )
        await repository.settle(
            intent=confirmed.intent,
            status=PhysicalIntentStatus.CONFIRMED,
        )

        replay = await repository.claim_for_dispatch(
            _intent("confirmed").model_copy(
                update={"owner_id": "owner-b", "execution_attempt_id": "attempt-c"}
            )
        )
        recovered = await repository.recover_inflight()
        recovered_again = await repository.recover_inflight()

        assert prepared.decision is PhysicalIntentClaimDecision.ACQUIRED
        assert replay.decision is PhysicalIntentClaimDecision.REPLAY
        assert replay.intent.status is PhysicalIntentStatus.CONFIRMED
        assert recovered == 1
        assert recovered_again == 0
        prepared = await repository.get(household_id="home-a", idempotency_key="prepared")
        assert prepared is not None
        assert prepared.status is PhysicalIntentStatus.UNKNOWN
        assert prepared.owner_id == "owner-a"
        assert prepared.execution_attempt_id == "attempt-a"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_inflight_recovery_is_scoped_to_deployment_and_prior_epoch(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        old = await repository.claim_for_dispatch(
            _intent("old").model_copy(
                update={
                    "deployment_id": "gw-a",
                    "fencing_epoch": 1,
                    "owner_id": "a",
                    "execution_attempt_id": "a",
                }
            )
        )
        current = await repository.claim_for_dispatch(
            _intent("current").model_copy(
                update={
                    "deployment_id": "gw-a",
                    "fencing_epoch": 2,
                    "owner_id": "a",
                    "execution_attempt_id": "b",
                }
            )
        )
        foreign = await repository.claim_for_dispatch(
            _intent("foreign").model_copy(
                update={
                    "deployment_id": "gw-b",
                    "fencing_epoch": 1,
                    "owner_id": "b",
                    "execution_attempt_id": "c",
                }
            )
        )

        recovered = await repository.recover_inflight(deployment_id="gw-a", fencing_epoch=2)

        assert recovered == 1
        old_persisted = await repository.get(household_id="home-a", idempotency_key="old")
        current_persisted = await repository.get(household_id="home-a", idempotency_key="current")
        foreign_persisted = await repository.get(household_id="home-a", idempotency_key="foreign")
        assert old_persisted is not None
        assert old_persisted.status is PhysicalIntentStatus.UNKNOWN
        assert current_persisted is not None
        assert current_persisted.status is PhysicalIntentStatus.DISPATCHING
        assert foreign_persisted is not None
        assert foreign_persisted.status is PhysicalIntentStatus.DISPATCHING
        assert old.intent.deployment_id == "gw-a"
        assert current.intent.fencing_epoch == 2
        assert foreign.intent.deployment_id == "gw-b"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_same_key_different_plan_is_conflict_and_preserves_owner(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        first = _intent().model_copy(
            update={
                "owner_id": "owner-a",
                "execution_attempt_id": "attempt-a",
            }
        )
        conflicting = first.model_copy(
            update={
                "plan_id": "plan-2",
                "owner_id": "owner-b",
                "execution_attempt_id": "attempt-b",
            }
        )

        acquired = await repository.claim_for_dispatch(first)
        conflict = await repository.claim_for_dispatch(conflicting)
        stored = await repository.get(household_id="home-a", idempotency_key="intent-1")

        assert acquired.decision is PhysicalIntentClaimDecision.ACQUIRED
        assert conflict.decision is PhysicalIntentClaimDecision.CONFLICT
        assert stored is not None
        assert stored.plan_id == "plan-1"
        assert stored.owner_id == "owner-a"
        assert stored.execution_attempt_id == "attempt-a"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stale_settlement_cannot_overwrite_dispatching_owner(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        acquired = await repository.claim_for_dispatch(
            _intent().model_copy(
                update={"owner_id": "owner-a", "execution_attempt_id": "attempt-a"}
            )
        )
        stale = acquired.intent.model_copy(
            update={"owner_id": "owner-b", "execution_attempt_id": "attempt-b"}
        )

        stale_result = await repository.settle(
            intent=stale,
            status=PhysicalIntentStatus.CONFIRMED,
        )
        stored = await repository.settle(
            intent=acquired.intent,
            status=PhysicalIntentStatus.ACKNOWLEDGED,
        )

        assert stale_result.status is PhysicalIntentStatus.DISPATCHING
        assert stored.status is PhysicalIntentStatus.ACKNOWLEDGED
        assert stored.owner_id == "owner-a"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_one_acquirer_per_key_across_100_repeated_sqlite_races(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first_database = SQLiteDatabase(path)
    second_database = SQLiteDatabase(path)
    await first_database.initialize()
    await second_database.initialize()
    try:
        first_repository = PhysicalIntentRepository(first_database)
        second_repository = PhysicalIntentRepository(second_database)
        for index in range(100):
            key = f"race-{index}"
            first_intent = _intent(key).model_copy(
                update={"owner_id": "owner-a", "execution_attempt_id": f"attempt-a-{index}"}
            )
            second_intent = _intent(key).model_copy(
                update={
                    "plan_id": "plan-race-b",
                    "owner_id": "owner-b",
                    "execution_attempt_id": f"attempt-b-{index}",
                }
            )
            results = await asyncio.gather(
                first_repository.claim_for_dispatch(first_intent),
                second_repository.claim_for_dispatch(second_intent),
            )
            decisions = {result.decision for result in results}
            assert decisions == {
                PhysicalIntentClaimDecision.ACQUIRED,
                PhysicalIntentClaimDecision.CONFLICT,
            }
    finally:
        await first_database.close()
        await second_database.close()


@pytest.fixture(
    scope="module", params=["sqlite", pytest.param("postgres", marks=pytest.mark.composition)]
)
def ledger_databases(request, tmp_path_factory):
    from contextlib import ExitStack

    from domoai.persistence.postgres import PostgresDatabase

    with ExitStack() as stack:
        if request.param == "postgres":
            import docker
            from testcontainers.community.postgres import PostgresContainer

            try:
                docker.from_env().ping()
            except Exception:
                pytest.skip("Docker unavailable for PostgreSQL ledger proof")
            container = stack.enter_context(
                PostgresContainer(
                    "postgres:16-alpine",
                    username="domoai",
                    password="domoai-test",
                    dbname="domoai",
                    driver=None,
                )
            )
            databases = [
                PostgresDatabase(container.get_connection_url(driver=None)) for _ in range(2)
            ]
        else:
            path = tmp_path_factory.mktemp("terminal-ledger") / "ledger.sqlite3"
            databases = [SQLiteDatabase(path) for _ in range(2)]
        for database in databases:
            asyncio.run(database.initialize())
        try:
            yield request.param, databases
        finally:
            for database in databases:
                asyncio.run(database.close())


@pytest.fixture
def ledger_pair(ledger_databases):
    backend, databases = ledger_databases
    for database in databases:
        database.connection.rollback()
    databases[0].connection.execute("DELETE FROM physical_intents")
    databases[0].connection.commit()
    try:
        yield backend, databases
    finally:
        for database in databases:
            database.connection.rollback()


def _owned_intent(key="terminal-intent"):
    return _intent(key).model_copy(
        update={
            "owner_id": "owner-a",
            "execution_attempt_id": "attempt-a",
            "lease_id": "lease-a",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [PhysicalIntentStatus.CONFIRMED, PhysicalIntentStatus.REJECTED, PhysicalIntentStatus.UNKNOWN],
)
async def test_acknowledged_intent_reaches_terminal_and_survives_recovery(ledger_pair, terminal):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    claim = await repo.claim_for_dispatch(_owned_intent())
    await repo.settle(intent=claim.intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    settled = await repo.settle(intent=claim.intent, status=terminal)
    assert settled.status is terminal
    assert await repo.recover_inflight(deployment_id="gw-a", fencing_epoch=4) == 0
    restored = await PhysicalIntentRepository(databases[1]).get(
        household_id="home-a", idempotency_key="terminal-intent"
    )
    assert restored == settled
    row = (
        databases[1]
        .connection.execute(
            "SELECT status, payload, updated_at FROM physical_intents WHERE household_id = ?",
            ("home-a",),
        )
        .fetchone()
    )
    import json

    assert row[0] == terminal.value
    assert json.loads(row[1])["status"] == terminal.value
    assert row[2] == settled.updated_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "foreign-tenant"),
        ("deployment_id", "foreign-deployment"),
        ("plan_id", "foreign-plan"),
        ("command_id", "foreign-command"),
        ("owner_id", "foreign-owner"),
        ("execution_attempt_id", "foreign-attempt"),
        ("fencing_epoch", 4),
        ("lease_id", "foreign-lease"),
    ],
)
async def test_settlement_rejects_every_mismatched_identity_field(ledger_pair, field, value):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    claim = await repo.claim_for_dispatch(_owned_intent())
    result = await repo.settle(
        intent=claim.intent.model_copy(update={field: value}), status=PhysicalIntentStatus.CONFIRMED
    )
    assert result == claim.intent
    assert (
        await repo.get(household_id="home-a", idempotency_key="terminal-intent")
    ) == claim.intent


@pytest.mark.asyncio
async def test_terminal_and_duplicate_ack_are_immutable_including_timestamp(ledger_pair):
    from datetime import timedelta

    from domoai.runtime.clock import FixedClock

    _, databases = ledger_pair
    clock = FixedClock(datetime(2026, 9, 10, tzinfo=UTC))
    repo = PhysicalIntentRepository(databases[0], clock=clock)
    for terminal in [
        PhysicalIntentStatus.CONFIRMED,
        PhysicalIntentStatus.REJECTED,
        PhysicalIntentStatus.UNKNOWN,
    ]:
        claim = await repo.claim_for_dispatch(_owned_intent(terminal.value))
        final = await repo.settle(intent=claim.intent, status=terminal)
        clock.set(clock.now() + timedelta(seconds=1))
        for requested in PhysicalIntentStatus:
            assert await repo.settle(intent=claim.intent, status=requested) == final
    claim = await repo.claim_for_dispatch(_owned_intent("duplicate-ack"))
    ack = await repo.settle(intent=claim.intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    clock.set(clock.now() + timedelta(seconds=1))
    assert await repo.settle(intent=claim.intent, status=PhysicalIntentStatus.ACKNOWLEDGED) == ack


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", [PhysicalIntentStatus.PREPARED, PhysicalIntentStatus.DISPATCHING]
)
async def test_settlement_cannot_reopen_inflight_or_prepared(ledger_pair, target):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    claim = await repo.claim_for_dispatch(_owned_intent())
    assert await repo.settle(intent=claim.intent, status=target) == claim.intent


@pytest.mark.asyncio
async def test_same_key_in_two_households_settles_only_its_owner(ledger_pair):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    first = await repo.claim_for_dispatch(_owned_intent())
    other = await repo.claim_for_dispatch(
        _owned_intent().model_copy(
            update={
                "household_id": "home-b",
                "owner_id": "owner-b",
                "execution_attempt_id": "attempt-b",
            }
        )
    )
    forged = first.intent.model_copy(update={"household_id": "home-b"})
    assert await repo.settle(intent=forged, status=PhysicalIntentStatus.CONFIRMED) == other.intent
    await repo.settle(intent=first.intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    result = await repo.settle(intent=first.intent, status=PhysicalIntentStatus.CONFIRMED)
    assert result.status is PhysicalIntentStatus.CONFIRMED
    assert await repo.get(household_id="home-b", idempotency_key="terminal-intent") == other.intent


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_ack", [False, True])
async def test_non_utc_timestamps_remain_compatible_when_acknowledged_intent_settles(
    ledger_pair, persisted_ack
):
    import json
    from datetime import timedelta, timezone

    from domoai.runtime.clock import FixedClock

    _, databases = ledger_pair
    clock = FixedClock(datetime(2026, 9, 10, 12, tzinfo=timezone(timedelta(hours=2))))
    repository = PhysicalIntentRepository(databases[0], clock=clock)
    intent = (await repository.claim_for_dispatch(_owned_intent())).intent
    if persisted_ack:
        # A legitimate older row may serialize the same instant using an offset.
        acknowledged = intent.model_copy(
            update={
                "status": PhysicalIntentStatus.ACKNOWLEDGED,
                "created_at": clock.now(),
                "updated_at": clock.now(),
            }
        )
        databases[0].connection.execute(
            """UPDATE physical_intents SET payload = ?, status = ?,
               created_at = ?, updated_at = ?""",
            (
                json.dumps(acknowledged.model_dump(mode="json")),
                "acknowledged",
                clock.now().isoformat(),
                clock.now().isoformat(),
            ),
        )
        databases[0].connection.commit()
    else:
        await repository.settle(intent=intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    confirmed = await repository.settle(intent=intent, status=PhysicalIntentStatus.CONFIRMED)
    assert confirmed.status is PhysicalIntentStatus.CONFIRMED
    assert confirmed.updated_at == clock.now()
    assert (
        await PhysicalIntentRepository(databases[1]).get(
            household_id=intent.household_id,
            idempotency_key=intent.idempotency_key,
        )
        == confirmed
    )


class _OrderedConnection:
    """Real SQL connection with barriers at reads/commits, never fabricated rows."""

    def __init__(self, connection, *, read_barrier, first_commit, wait_for_first):
        self.connection = connection
        self.read_barrier = read_barrier
        self.first_commit = first_commit
        self.wait_for_first = wait_for_first
        self.initial_read = True
        self.initial_write = True

    def execute(self, sql, *args):
        if sql.lstrip().startswith("UPDATE physical_intents") and self.initial_write:
            self.initial_write = False
            if self.wait_for_first:
                assert self.first_commit.wait(10), "first transaction did not commit"
        cursor = self.connection.execute(sql, *args)
        if sql.lstrip().startswith("SELECT") and "physical_intents" in sql and self.initial_read:
            self.initial_read = False
            barrier = self.read_barrier

            class ReadCursor:
                def fetchone(self):
                    row = cursor.fetchone()
                    barrier.wait(timeout=10)
                    return row

                def fetchall(self):
                    rows = cursor.fetchall()
                    barrier.wait(timeout=10)
                    return rows

            return ReadCursor()
        return cursor

    def commit(self):
        self.connection.commit()
        if not self.wait_for_first:
            self.first_commit.set()

    def rollback(self):
        self.connection.rollback()

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_first", [False, True])
async def test_real_recovery_and_confirmation_race_preserves_first_terminal(
    ledger_pair, recovery_first
):
    from threading import Barrier, Event
    from types import SimpleNamespace

    backend, databases = ledger_pair
    repository = PhysicalIntentRepository(databases[0])
    intent = (await repository.claim_for_dispatch(_owned_intent())).intent
    await repository.settle(intent=intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    start = Barrier(2)
    reads = Barrier(2)
    committed = Event()

    def run_in_thread(index):
        database = databases[index]
        if backend == "postgres":
            database = SimpleNamespace(
                connection=_OrderedConnection(
                    database.connection,
                    read_barrier=reads,
                    first_commit=committed,
                    wait_for_first=(bool(index) != recovery_first),
                )
            )
        repo = PhysicalIntentRepository(database)
        start.wait(timeout=10)
        if index == 1:
            return asyncio.run(repo.recover_inflight(deployment_id="gw-a", fencing_epoch=4))
        return asyncio.run(repo.settle(intent=intent, status=PhysicalIntentStatus.CONFIRMED))

    settled, recovered = await asyncio.gather(
        asyncio.to_thread(run_in_thread, 0),
        asyncio.to_thread(run_in_thread, 1),
    )
    stored = await repository.get(household_id="home-a", idempotency_key="terminal-intent")
    assert stored == settled
    assert stored.status is (
        PhysicalIntentStatus.UNKNOWN if recovered else PhysicalIntentStatus.CONFIRMED
    )
    if backend == "postgres":
        assert recovered == int(recovery_first)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_status,second_status",
    [
        (PhysicalIntentStatus.ACKNOWLEDGED, PhysicalIntentStatus.CONFIRMED),
        (PhysicalIntentStatus.CONFIRMED, PhysicalIntentStatus.ACKNOWLEDGED),
        (PhysicalIntentStatus.UNKNOWN, PhysicalIntentStatus.CONFIRMED),
        (PhysicalIntentStatus.CONFIRMED, PhysicalIntentStatus.UNKNOWN),
    ],
)
async def test_real_concurrent_settlers_preserve_monotonic_state_and_return_fresh_result(
    ledger_pair,
    first_status,
    second_status,
):
    from threading import Barrier, Event
    from types import SimpleNamespace

    backend, databases = ledger_pair
    intent = (
        await PhysicalIntentRepository(databases[0]).claim_for_dispatch(_owned_intent())
    ).intent
    start = Barrier(2)
    reads = Barrier(2)
    committed = Event()

    def settle_in_thread(index, status):
        database = databases[index]
        if backend == "postgres":
            # READ COMMITTED permits both snapshots to read DISPATCHING. Force
            # opposite commit orders to exercise lost-CAS reload, not scheduling luck.
            database = SimpleNamespace(
                connection=_OrderedConnection(
                    database.connection,
                    read_barrier=reads,
                    first_commit=committed,
                    wait_for_first=bool(index),
                )
            )
        start.wait(timeout=10)
        return asyncio.run(PhysicalIntentRepository(database).settle(intent=intent, status=status))

    first, second = await asyncio.gather(
        asyncio.to_thread(settle_in_thread, 0, first_status),
        asyncio.to_thread(settle_in_thread, 1, second_status),
    )
    stored = await PhysicalIntentRepository(databases[0]).get(
        household_id="home-a", idempotency_key="terminal-intent"
    )
    if PhysicalIntentStatus.ACKNOWLEDGED in {first_status, second_status}:
        assert stored.status is PhysicalIntentStatus.CONFIRMED
        confirming = first if first_status is PhysicalIntentStatus.CONFIRMED else second
        assert confirming.status is PhysicalIntentStatus.CONFIRMED
        assert first.status is not PhysicalIntentStatus.DISPATCHING
        assert second.status is not PhysicalIntentStatus.DISPATCHING
    else:
        assert stored.status in {PhysicalIntentStatus.CONFIRMED, PhysicalIntentStatus.UNKNOWN}
        assert first == second == stored
        if backend == "postgres":
            assert stored.status is first_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    [("status", "acknowledged"), ("owner_id", "inconsistent-owner"), ("fencing_epoch", 99)],
)
async def test_inconsistent_ledger_projection_fails_closed_without_retry_loop(
    ledger_pair, column, value
):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    claimed = await repo.claim_for_dispatch(_owned_intent())
    # Column names are literal cases, never user-controlled SQL.
    databases[0].connection.execute(f"UPDATE physical_intents SET {column} = ?", (value,))
    databases[0].connection.commit()
    with pytest.raises(ValueError, match="inconsistent"):
        await repo.settle(intent=claimed.intent, status=PhysicalIntentStatus.CONFIRMED)


@pytest.mark.asyncio
async def test_recovered_intent_cannot_be_reopened_by_late_confirmation(ledger_pair):
    _, databases = ledger_pair
    repo = PhysicalIntentRepository(databases[0])
    claimed = await repo.claim_for_dispatch(_owned_intent())
    await repo.settle(intent=claimed.intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
    assert await repo.recover_inflight(deployment_id="gw-a", fencing_epoch=4) == 1
    recovered = await repo.get(household_id="home-a", idempotency_key="terminal-intent")
    assert recovered.status is PhysicalIntentStatus.UNKNOWN
    assert (
        await repo.settle(intent=claimed.intent, status=PhysicalIntentStatus.CONFIRMED) == recovered
    )
