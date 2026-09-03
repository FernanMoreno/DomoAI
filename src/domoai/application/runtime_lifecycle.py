"""Shared ownership of runtime background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field

Runner = Callable[[], Coroutine[object, object, None]]


@dataclass
class RuntimeLifecycle:
    """Start and stop each long-lived runtime runner exactly once."""

    event_runner: Runner
    scheduler_runner: Runner
    state_refresh_runner: Runner | None = None
    supervisor_runner: Runner | None = None
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def running_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    @property
    def closed(self) -> bool:
        return self._closed

    def task(self, name: str) -> asyncio.Task[None] | None:
        for task in self._tasks:
            if task.get_name() == name:
                return task
        return None

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("runtime lifecycle is already closed")
        if self._started:
            return
        runners: list[tuple[str, Runner]] = [
            ("domoai-event-consumer", self.event_runner),
            ("domoai-scheduler", self.scheduler_runner),
        ]
        if self.supervisor_runner is not None:
            runners.append(("domoai-control-supervisor", self.supervisor_runner))
        if self.state_refresh_runner is not None:
            runners.append(("domoai-state-refresher", self.state_refresh_runner))
        self._tasks = [asyncio.create_task(runner(), name=name) for name, runner in runners]
        self._started = True

    async def close(self) -> None:
        if self._closed:
            return
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._closed = True
