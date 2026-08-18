"""Reusable, per-method configurable failure injection for AdapterPort."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)


class FailureInjectingAdapter:
    """AdapterPort test double: raises a configured exception per method.

    Any method not present in ``fail`` succeeds trivially, so a test only
    needs to specify the one operation it wants to fail.
    """

    def __init__(
        self,
        adapter_id: str = "failure-injection",
        *,
        fail: dict[str, BaseException] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self._fail = dict(fail or {})
        self.execute_calls: list[Command] = []

    def _maybe_raise(self, method: str) -> None:
        error = self._fail.get(method)
        if error is not None:
            raise error

    async def connect(self) -> None:
        self._maybe_raise("connect")

    async def disconnect(self) -> None:
        self._maybe_raise("disconnect")

    async def discover(self) -> AdapterSnapshot:
        self._maybe_raise("discover")
        return AdapterSnapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._maybe_raise("read_state")
        return []

    async def execute(self, command: Command) -> AdapterExecutionAck:
        self._maybe_raise("execute")
        self.execute_calls.append(command)
        return AdapterExecutionAck(accepted=True)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async def stream() -> AsyncIterator[SourceEvent]:
            self._maybe_raise("subscribe_events")
            return
            yield  # pragma: no cover

        return stream()

    async def health(self) -> AdapterHealth:
        self._maybe_raise("health")
        return AdapterHealth(adapter_id=self.adapter_id, connected=True)
