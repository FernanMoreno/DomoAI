"""Ports that keep the runtime independent of external protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    ExecutionOutcome,
    Plan,
    PlanStatus,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)
from domoai.runtime.execution_context import ExecutionContext


class AdapterPort(Protocol):
    adapter_id: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def discover(self) -> AdapterSnapshot: ...

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]: ...

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck: ...

    def subscribe_events(self) -> AsyncIterator[SourceEvent]: ...

    async def health(self) -> AdapterHealth: ...


class StateStorePort(Protocol):
    stale_after: timedelta

    async def save(self, snapshot: StateSnapshot) -> None: ...

    def peek(self, device_id: str, capability: str) -> StateSnapshot | None: ...

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None: ...


class StateSnapshotSinkPort(Protocol):
    """Durable sink for normalized runtime state observations."""

    async def save(self, snapshot: StateSnapshot) -> None: ...


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
    async def save_approval(self, plan: Plan) -> None: ...
    async def settle_execution(self, plan: Plan) -> None: ...
    async def get(self, plan_id: str) -> Plan | None: ...
    async def mark_unknown_if_executing(self, plan_id: str) -> bool: ...
    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool: ...
    async def list_by_status(self, statuses: frozenset[PlanStatus]) -> list[Plan]: ...


class ExecutionOutcomePort(Protocol):
    async def save(self, outcome: ExecutionOutcome) -> None: ...
