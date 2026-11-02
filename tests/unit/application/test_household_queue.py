import asyncio

import pytest

from domoai.application.household_queue import HouseholdWorkQueues, QueueOverloaded


@pytest.mark.asyncio
async def test_overload_is_isolated_per_household() -> None:
    queues = HouseholdWorkQueues(max_per_household=1, max_total=2)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked() -> str:
        started.set()
        await release.wait()
        return "home-a"

    first = asyncio.create_task(queues.submit("home-a", blocked))
    await started.wait()
    with pytest.raises(QueueOverloaded):
        await queues.submit("home-a", lambda: "rejected")

    other = await queues.submit("home-b", lambda: "home-b")
    assert other == "home-b"
    release.set()
    assert await first == "home-a"
    await queues.close()


@pytest.mark.asyncio
async def test_household_queue_preserves_fifo_and_closes_cleanly() -> None:
    queues = HouseholdWorkQueues(max_per_household=3, max_total=3)
    order: list[int] = []

    async def work(value: int) -> int:
        order.append(value)
        return value

    results = await asyncio.gather(
        queues.submit("home-a", lambda: work(1)),
        queues.submit("home-a", lambda: work(2)),
        queues.submit("home-a", lambda: work(3)),
    )

    assert results == [1, 2, 3]
    assert order == [1, 2, 3]
    assert queues.depths() == {}
    await queues.close()


@pytest.mark.asyncio
async def test_cancel_pending_recovers_capacity_without_running_work() -> None:
    queues = HouseholdWorkQueues(max_per_household=2, max_total=2)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked():
        entered.set()
        await release.wait()

    active = asyncio.create_task(queues.submit("a", blocked))
    await entered.wait()
    pending = asyncio.create_task(queues.submit("a", lambda: calls.append("cancelled")))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    try:
        assert queues.depths() == {"a": 1}
        assert await queues.submit("b", lambda: "other") == "other"
    finally:
        release.set()
        await active
        await queues.close()
    assert calls == []


@pytest.mark.asyncio
async def test_queued_work_uses_each_request_principal() -> None:
    from domoai.runtime.execution_context import current_execution_principal, execution_principal

    queues = HouseholdWorkQueues()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        await release.wait()
        return current_execution_principal()

    with execution_principal("alice"):
        first = asyncio.create_task(queues.submit("a", blocked))
    await entered.wait()
    with execution_principal("bob"):
        second = asyncio.create_task(queues.submit("a", current_execution_principal))
    await asyncio.sleep(0)
    release.set()
    assert await first == "alice"
    assert await second == "bob"
    await queues.close()


@pytest.mark.asyncio
async def test_cancelling_close_still_drains_running_work() -> None:
    queues = HouseholdWorkQueues()
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def work():
        entered.set()
        await release.wait()
        effects.append("settled")

    submit = asyncio.create_task(queues.submit("a", work))
    await entered.wait()
    close = asyncio.create_task(queues.close())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(submit, close, return_exceptions=True)
    assert effects == ["settled"]
    assert queues.depths() == {}
