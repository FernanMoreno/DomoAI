"""Shared ownership of runtime background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field

Runner = Callable[[], Coroutine[object, object, None]]


@dataclass
class RuntimeLifecycle:
    """Start and stop each long-lived runtime runner exactly once."""

    event_runner: Runner
    scheduler_runner: Runner
    state_refresh_runner: Runner | None = None
    supervisor_runner: Runner | None = None
    coordination_runner: Runner | None = None
    outbox_runner: Runner | None = None
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False, repr=False)
    _required_task_names: tuple[str, ...] = field(default=(), init=False, repr=False)
    _closed_task_states: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
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

    @property
    def closing(self) -> bool:
        """Whether this lifecycle's own close operation has started."""

        return self._closing and not self._closed

    @property
    def required_task_states(self) -> Mapping[str, str]:
        """Sanitized state of every runner configured at startup.

        The configured names are captured once so later attribute changes do
        not make a required worker disappear from readiness.
        """

        if self._closed_task_states:
            return dict(self._closed_task_states)
        tasks = {task.get_name(): task for task in self._tasks}
        return {
            name: self._task_state(tasks.get(name))
            for name in self._required_task_names
        }

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
        if self.coordination_runner is not None:
            runners.append(("domoai-coordination-supervisor", self.coordination_runner))
        if self.state_refresh_runner is not None:
            runners.append(("domoai-state-refresher", self.state_refresh_runner))
        if self.outbox_runner is not None:
            runners.append(("domoai-audit-outbox-dispatcher", self.outbox_runner))
        self._required_task_names = tuple(name for name, _ in runners)
        try:
            for name, runner in runners:
                task = asyncio.create_task(runner(), name=name)
                task.add_done_callback(self._consume_task_exception)
                self._tasks.append(task)
        except BaseException:
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            raise
        self._started = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._closed_task_states = dict(self.required_task_states)
        self._tasks.clear()
        self._closed = True

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[None]) -> None:
        """Retrieve failures so an observed readiness failure emits no warning."""

        if not task.cancelled():
            task.exception()

    @staticmethod
    def _task_state(task: asyncio.Task[None] | None) -> str:
        if task is None:
            return "missing"
        if not task.done():
            return "running"
        if task.cancelled():
            return "cancelled"
        return "failed" if task.exception() is not None else "stopped"
