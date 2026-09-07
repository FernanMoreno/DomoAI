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
