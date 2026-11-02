"""ProcessOptimizationWorker: the CP-SAT solve path is process-isolated
(spec 150 / P2.5 from the 2026-08-24 re-audit of commit 61439f3).

The critical property this whole feature exists to prove (SC-001): a
timed-out solve's OS process is actually terminated, not merely abandoned.
That property was structurally impossible to test against the thread-backed
`OptimizationWorker` -- Python threads cannot be forcibly stopped, so there
is no "prove it stopped" available for a thread. Here there is: we record
the worker process's PID from inside the (deliberately slow) solve, then
confirm that PID no longer exists shortly after the configured timeout.

`_slow_solve` and `_pid_recording_solve` must be module-level functions
(not closures/lambdas): `ProcessOptimizationWorker`'s `solve_fn` is
scheduled into a `spawn`-context subprocess, which re-imports the callable
by its module path rather than pickling it by value -- a lambda or a
function nested in a test body is not importable that way.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.application.optimization_worker import WorkerBudget
from domoai.application.process_optimization_worker import ProcessOptimizationWorker
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario
from domoai.runtime.registry import DeviceRegistry


def _horizon() -> Horizon:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return Horizon(
        start=start,
        end=start + timedelta(minutes=15),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )


def _slow_solve(scenario: OptimizationScenario) -> OptimizationResult:
    """Sleeps far past any timeout this test suite configures. Must never
    actually return -- if it does, the timeout/kill didn't happen."""
    time.sleep(30.0)
    return OptimizationResult(
        scenario_id=scenario.id, status=OptimizationStatus.OPTIMAL, solver="should-never-return"
    )


def _slow_only_for_scenarios_named_slow(scenario: OptimizationScenario) -> OptimizationResult:
    """Like `_slow_solve`, but only for scenarios whose id contains "slow" --
    lets a single worker (one fixed `solve_fn` for its whole lifetime, same
    as production) both trigger a timeout/kill and then prove the respawned
    worker still does real work for an ordinary follow-up call."""
    if "slow" in scenario.id:
        time.sleep(30.0)
    return OptimizationResult(
        scenario_id=scenario.id, status=OptimizationStatus.OPTIMAL, solver="fast"
    )


def _pid_recording_slow_solve(scenario: OptimizationScenario) -> OptimizationResult:
    """Writes its own PID to a fixed, well-known path derived from the
    scenario id (the only channel available to a spawned subprocess
    without extra IPC plumbing), then sleeps past the timeout."""
    import os

    if scenario.id == "warm-up":
        return OptimizationResult(
            scenario_id=scenario.id, status=OptimizationStatus.OPTIMAL, solver="warm-up"
        )
    Path(f"/tmp/domoai-test-pid-{scenario.id}.txt").write_text(str(os.getpid()))
    time.sleep(30.0)
    return OptimizationResult(
        scenario_id=scenario.id, status=OptimizationStatus.OPTIMAL, solver="should-never-return"
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_normal_solve_round_trips_through_the_process_pool() -> None:
    registry = DeviceRegistry()
    worker = ProcessOptimizationWorker(registry, WorkerBudget(max_solver_time_seconds=10.0))
    try:
        scenario = OptimizationScenario(id="normal-solve", horizon=_horizon())
        result = await worker.optimize(scenario)
        assert result.status == OptimizationStatus.NO_ACTION_REQUIRED
        assert result.solver == "cp-sat"
    finally:
        worker.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_invalid_scenario_never_reaches_the_subprocess() -> None:
    registry = DeviceRegistry()  # empty: no devices at all
    worker = ProcessOptimizationWorker(registry, WorkerBudget(max_solver_time_seconds=10.0))
    try:
        from domoai.optimizer.scenario import Load

        scenario = OptimizationScenario(
            id="invalid-scenario",
            horizon=_horizon(),
            loads=[
                Load(
                    id="load-1",
                    device_id="unknown.device",
                    capability="power",
                    command="turn_on",
                )
            ],
        )
        before = set(multiprocessing.active_children())
        result = await worker.optimize(scenario)
        after = set(multiprocessing.active_children())

        assert result.status == OptimizationStatus.INVALID
        assert any(d.code == "missing_device" for d in result.diagnostics)
        # No pool worker was ever spawned for a validation-only reject.
        assert before == after
    finally:
        worker.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_horizon_limit_rejects_before_subprocess_admission() -> None:
    registry = DeviceRegistry()
    worker = ProcessOptimizationWorker(registry, WorkerBudget(max_solver_time_seconds=10.0))
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    oversized = Horizon(
        start=start,
        end=start + timedelta(minutes=(10080 + 1) * 15),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )
    try:
        before = set(multiprocessing.active_children())
        result = await worker.optimize(
            OptimizationScenario(id="oversized-horizon", horizon=oversized)
        )
        after = set(multiprocessing.active_children())

        assert result.status == OptimizationStatus.INVALID
        assert any(d.code == "horizon_too_large" for d in result.diagnostics)
        assert before == after
    finally:
        worker.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_queue_full_raises_without_touching_the_pool(tmp_path) -> None:
    registry = DeviceRegistry()
    worker = ProcessOptimizationWorker(
        registry,
        WorkerBudget(
            max_solver_time_seconds=10.0,
            max_concurrency=1,
            queue_capacity=0,
            queue_wait_seconds=0.05,
        ),
        solve_fn=_slow_solve,
    )
    try:
        scenario_a = OptimizationScenario(id="queue-full-a", horizon=_horizon())
        scenario_b = OptimizationScenario(id="queue-full-b", horizon=_horizon())
        first = asyncio.ensure_future(worker.optimize(scenario_a))
        await asyncio.sleep(0.3)  # let the first solve actually be admitted
        second = await worker.optimize(scenario_b)

        assert second.status == OptimizationStatus.UNKNOWN
        assert any(d.code == "worker_queue_full" for d in second.diagnostics)
    finally:
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        worker.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_timed_out_solve_process_is_actually_terminated() -> None:
    """The SC-001 test: proves a timed-out solve's OS process really stops,
    not just that the caller received an error -- the property no test
    could express against the thread-backed OptimizationWorker."""
    registry = DeviceRegistry()
    worker = ProcessOptimizationWorker(
        registry,
        WorkerBudget(max_solver_time_seconds=1.0, max_concurrency=1, queue_capacity=0),
        solve_fn=_pid_recording_slow_solve,
    )
    scenario_id = "kill-proof"
    pid_file = Path(f"/tmp/domoai-test-pid-{scenario_id}.txt")
    pid_file.unlink(missing_ok=True)
    try:
        # The timeout property must measure the running worker, not Pebble's
        # one-time spawn/OR-Tools import cost. A cold process can legitimately
        # take several seconds before it acknowledges its first task.
        warmup = await worker.optimize(OptimizationScenario(id="warm-up", horizon=_horizon()))
        assert warmup.status == OptimizationStatus.OPTIMAL
        scenario = OptimizationScenario(id=scenario_id, horizon=_horizon())

        started = time.monotonic()
        result = await worker.optimize(scenario)
        elapsed = time.monotonic() - started

        assert result.status == OptimizationStatus.TIMEOUT
        assert any(d.code == "worker_timeout" for d in result.diagnostics)
        # Killed at (approximately) the deadline, not after the 30s sleep.
        assert elapsed < 10.0

        # Give the pool a moment to finish killing + reaping the worker.
        deadline = time.monotonic() + 5.0
        child_pid: int | None = None
        while time.monotonic() < deadline:
            if pid_file.exists():
                child_pid = int(pid_file.read_text().strip())
                break
            await asyncio.sleep(0.1)
        assert child_pid is not None, "solve never recorded its PID -- test setup broken"

        # The actual proof: the process that was running the slow solve no
        # longer exists.
        deadline = time.monotonic() + 5.0
        pid_gone = False
        while time.monotonic() < deadline:
            try:
                os_kill_check(child_pid)
            except ProcessLookupError:
                pid_gone = True
                break
            await asyncio.sleep(0.1)
        assert pid_gone, f"worker process {child_pid} is still running after its timeout"

        # And the pool is still usable for the next call: a fresh worker
        # was transparently respawned.
        assert worker._pool.active
    finally:
        pid_file.unlink(missing_ok=True)
        worker.close()


def os_kill_check(pid: int) -> None:
    """`os.kill(pid, 0)` raises ProcessLookupError if the process is gone,
    without actually sending a signal -- the standard liveness probe."""
    import os

    os.kill(pid, 0)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_pool_serves_a_second_call_after_a_timeout_kill() -> None:
    registry = DeviceRegistry()
    worker = ProcessOptimizationWorker(
        registry,
        WorkerBudget(max_solver_time_seconds=1.0, max_concurrency=1, queue_capacity=0),
        solve_fn=_slow_only_for_scenarios_named_slow,
    )
    try:
        timed_out = await worker.optimize(
            OptimizationScenario(id="respawn-trigger-slow", horizon=_horizon())
        )
        assert timed_out.status == OptimizationStatus.TIMEOUT

        # A fresh worker process (paying the ortools import cost again,
        # once) must transparently serve this next, ordinary call.
        normal = await worker.optimize(
            OptimizationScenario(id="respawn-followup-fast", horizon=_horizon())
        )
        assert normal.status == OptimizationStatus.OPTIMAL
        assert normal.solver == "fast"
    finally:
        worker.close()


def _versioned_result_solve(scenario: OptimizationScenario) -> OptimizationResult:
    from tests.fixtures.runtime_schema_compatibility import plan

    current = plan(deployment_id="worker-deployment")
    result = OptimizationResult(
        scenario_id=scenario.id,
        status="feasible",
        solver="fixture",
        plan=current,
        plans=[plan(), current],
    )
    if scenario.id == "mutated-contract":
        return result.model_copy(update={"schema_version": "v1"})
    return result


@pytest.mark.composition
@pytest.mark.asyncio
async def test_spawned_worker_preserves_mixed_versions_and_rejects_mutated_contract() -> None:
    worker = ProcessOptimizationWorker(
        DeviceRegistry(), WorkerBudget(max_solver_time_seconds=10), solve_fn=_versioned_result_solve
    )
    try:
        result = await worker.optimize(
            OptimizationScenario(id="valid-contract", horizon=_horizon())
        )
        assert result.schema_version == "v2"
        assert result.plan.schema_version == "v2"
        assert [plan.schema_version for plan in result.plans] == ["v1", "v2"]
        invalid = await worker.optimize(
            OptimizationScenario(id="mutated-contract", horizon=_horizon())
        )
        assert invalid.status == OptimizationStatus.UNKNOWN
        assert invalid.diagnostics[0].code == "worker_invalid_result"
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_a12_accounting_survives_actual_process_pickle_and_independent_json():
    import hashlib

    from domoai.mcp.ortools_server import explain_result
    from domoai.optimizer.energy import (
        BaseLoadPoint,
        EnergyContext,
        SolarForecastPoint,
        TariffPoint,
    )
    from domoai.optimizer.product import build_product_summary

    horizon = _horizon()
    context = EnergyContext(
        horizon=horizon,
        tariffs=[TariffPoint(slot=0, price_per_kwh=0.2, currency="USD")],
        solar_forecast=[SolarForecastPoint(slot=0, power=0)],
        base_load_forecast=[BaseLoadPoint(slot=0, power=1.0)],
        source_revision="rev-é-" + "x" * 400,
        observed_at=horizon.start,
    )
    scenario = OptimizationScenario(id="a12-process", horizon=horizon, energy_context=context)
    worker = ProcessOptimizationWorker(DeviceRegistry(), WorkerBudget(max_solver_time_seconds=10.0))
    try:
        result = await worker.optimize(scenario)
        assert result.status is OptimizationStatus.NO_ACTION_REQUIRED
        assert result.objective_values["energy_cost"] == pytest.approx(0.05)
        block = result.constraint_summary["accounting"]
        assert (
            block["source_revision_digest"]
            == hashlib.sha256(context.source_revision.encode("utf-8")).hexdigest()
        )
        restored = OptimizationResult.model_validate_json(result.model_dump_json())
        assert restored.constraint_summary["accounting"] == block
        assert explain_result(restored).constraint_summary["accounting"] == block
        assert build_product_summary(restored).constraint_summary["accounting"] == block
        assert "currency" not in context.model_dump()
    finally:
        worker.close()


@pytest.mark.parametrize("value", [0.0, 0.2])
async def test_a12_terminal_currency_rejection_does_not_spawn(value):
    from tests.fixtures.energy import energy_context_for

    context = energy_context_for()
    for point in [*context.tariffs, *(context.export_tariffs or [])]:
        point.currency = "USD"
    scenario = OptimizationScenario(
        id="a12-terminal-reject",
        horizon=context.horizon,
        energy_context=context,
        terminal_soc_policy={"value_eur_per_kwh": value},
    )
    worker = ProcessOptimizationWorker(DeviceRegistry(), WorkerBudget(max_solver_time_seconds=10))
    try:
        before = set(multiprocessing.active_children())
        result = await worker.optimize(scenario)
        assert result.status is OptimizationStatus.INVALID
        assert "terminal_soc_currency_mismatch" in {error.code for error in result.diagnostics}
        assert set(multiprocessing.active_children()) == before
    finally:
        worker.close()
