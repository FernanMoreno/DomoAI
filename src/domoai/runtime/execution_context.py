"""Immutable metadata carried with one physical command attempt."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Non-secret correlation lineage for one adapter request."""

    plan_id: str
    execution_attempt_id: str
    adapter_request_id: str
    agent_request_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("plan_id", "execution_attempt_id", "adapter_request_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.agent_request_id is not None and (
            not isinstance(self.agent_request_id, str) or not self.agent_request_id.strip()
        ):
            raise ValueError("agent_request_id must be non-empty when provided")
