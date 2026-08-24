"""RuntimeComposition owns every OptimizationWorker it creates (P2.1).

Closes P2.1 from the 2026-08-24 re-audit of commit 61439f3: `OptimizationWorker`
wraps a `ThreadPoolExecutor`. Before this fix, `mcp/stdio.py`'s
`build_configured_server` created one worker for the optimizer boundary but
never registered it with `RuntimeComposition`, so `runtime.close()` never
called `worker.close()` -- the executor's threads outlived the runtime. A
second worker for the energy-context boundary was created *lazily inside the
get_energy_context tool*, on every context that didn't already carry one
whose `.service` happened to match `energy_context_provider` -- which the
production-wired worker never did, so a second, wholly unowned worker was
silently created on first use and never closed either.

Since spec 150, the optimizer-boundary worker is `ProcessOptimizationWorker`
(process-pool-backed) while the energy-context worker stays
`OptimizationWorker` (thread-backed) -- see spec 150 FR-006 -- so this test
checks each backend's own liveness signal instead of assuming both share
`ThreadPoolExecutor` internals.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.optimization_worker import OptimizationWorker
from domoai.application.process_optimization_worker import ProcessOptimizationWorker
from domoai.config.settings import Settings
from domoai.mcp.stdio import build_configured_server


class _BlockingCloseWorker:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        time.sleep(0.15)
        self.closed = True


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_close_shuts_down_both_optimizer_and_energy_workers(tmp_path) -> None:
    runtime, server = await build_configured_server(
        Settings(
            database_path=tmp_path / "worker-ownership.sqlite3",
            energy_live=True,
            tariff_provider="omie",
            solar_provider="open_meteo",
            solar_latitude=40.4168,
            solar_longitude=-3.7038,
            solar_installed_kwp=6.0,
            solar_tilt=30.0,
            solar_azimuth=0.0,
            solar_performance_ratio=0.82,
        ),
    )
    try:
        # Two distinct workers on two distinct backends: the optimizer
        # boundary is process-backed (spec 150), the energy-context
        # boundary stays thread-backed (spec 150 FR-006).
        assert len(runtime.blocking_workers) == 2
        assert {type(worker) for worker in runtime.blocking_workers} == {
            ProcessOptimizationWorker,
            OptimizationWorker,
        }
        process_worker = next(
            w for w in runtime.blocking_workers if isinstance(w, ProcessOptimizationWorker)
        )
        thread_worker = next(
            w for w in runtime.blocking_workers if isinstance(w, OptimizationWorker)
        )
        assert not thread_worker._executor._shutdown
        assert process_worker._pool.active
    finally:
        await runtime.close()

    assert thread_worker._executor._shutdown
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        thread_worker._executor.submit(lambda: None)

    assert not process_worker._pool.active
    with pytest.raises(RuntimeError, match="not active"):
        process_worker._pool.schedule(lambda: None)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_registering_a_worker_after_the_fact_still_gets_closed(tmp_path) -> None:
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "worker-registration.sqlite3"),
    )
    extra_worker = runtime.register_blocking_worker(
        OptimizationWorker(SimulatedHomeAdapter())  # any object stands in as `service` here
    )
    assert extra_worker in runtime.blocking_workers

    await runtime.close()

    assert extra_worker._executor._shutdown


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_close_does_not_block_the_event_loop_on_worker_shutdown(tmp_path) -> None:
    runtime, _server = await build_configured_server(
        Settings(database_path=tmp_path / "worker-close-loop.sqlite3"),
    )
    blocking_worker = runtime.register_blocking_worker(_BlockingCloseWorker())
    ticks = 0
    stop = asyncio.Event()

    async def tick() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        await runtime.close()
    finally:
        stop.set()
        await ticker

    assert blocking_worker.closed
    assert ticks >= 5
