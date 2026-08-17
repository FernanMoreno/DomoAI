"""Ports that keep the runtime independent of external protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Protocol

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    ExecutionOutcome,
    Plan,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)


class AdapterPort(Protocol):
    adapter_id: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def discover(self) -> AdapterSnapshot: ...

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]: ...

    async def execute(self, command: Command) -> AdapterExecutionAck: ...

    def subscribe_events(self) -> AsyncIterator[SourceEvent]: ...

    async def health(self) -> AdapterHealth: ...


class StateStorePort(Protocol):
    async def save(self, snapshot: StateSnapshot) -> None: ...

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None: ...


class AuditSinkPort(Protocol):
    def append(
        self,
        *,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: dict[str, object],
    ) -> object: ...


class DatabasePort(Protocol):
    path: Path

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...


class PlanRecordPort(Protocol):
    async def save(self, plan: Plan) -> None: ...


class ExecutionOutcomePort(Protocol):
    async def save(self, outcome: ExecutionOutcome) -> None: ...
