"""Internal audit events and secret-safe payload handling."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol
from uuid import uuid4

from domoai.domain.models import AuditEvent

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
}


class AuditEventSink(Protocol):
    def append_event(self, event: AuditEvent) -> None: ...


def redact_payload(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SECRET_KEYS:
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    return deepcopy(value)


class AuditLog:
    """Small append-only in-memory sink used by the foundation and tests."""

    def __init__(self, sink: AuditEventSink | None = None) -> None:
        self._events: list[AuditEvent] = []
        self._sink = sink

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        *,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: Mapping[str, Any],
    ) -> AuditEvent:
        event = AuditEvent(
            id=str(uuid4()),
            event_type=event_type,
            actor=actor,
            subject_id=subject_id,
            payload=redact_payload(payload),
        )
        self._events.append(event)
        if self._sink is not None:
            self._sink.append_event(event)
        return event
