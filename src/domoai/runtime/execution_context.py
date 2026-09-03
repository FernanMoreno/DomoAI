"""Immutable metadata carried with one physical command attempt."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_execution_principal: ContextVar[str] = ContextVar(
    "domoai_execution_principal", default="local"
)


def current_execution_principal() -> str:
    """Return the non-secret client identity for the current async request."""

    return _execution_principal.get()


@contextmanager
def execution_principal(principal_id: str | None) -> Iterator[None]:
    """Scope an agent identity across MCP-to-adapter execution lineage."""

    normalized = principal_id.strip() if principal_id is not None else ""
    token = _execution_principal.set(normalized or "local")
    try:
        yield
    finally:
        _execution_principal.reset(token)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Non-secret correlation lineage for one adapter request."""

    plan_id: str
    execution_attempt_id: str
    adapter_request_id: str
    agent_request_id: str | None = None
    client_principal_id: str = "local"

    def __post_init__(self) -> None:
        for field_name in ("plan_id", "execution_attempt_id", "adapter_request_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.agent_request_id is not None and (
            not isinstance(self.agent_request_id, str) or not self.agent_request_id.strip()
        ):
            raise ValueError("agent_request_id must be non-empty when provided")
