"""Internal audit events and secret-safe payload handling."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol
from uuid import uuid4

from domoai.domain.models import AuditEvent, AuthorityContext, PrincipalRole
from domoai.runtime.clock import Clock, SystemClock

DEFAULT_MAX_EVENTS = 1000

CRITICAL_AUDIT_EVENT_TYPES = frozenset(
    {
        "plan_approved",
        "plan_execution_started",
        "plan_execution_completed",
        "command_execution_outcome",
        "unknown_authority",
        "control_lease_released",
        "control_lease_release_failed",
        "control_supervisor_emergency_stop",
        "control_supervisor_lease_stop",
        "control_supervisor_startup_reconciliation",
        "plan_execution_recovered",
        "bundle_commit_started",
        "bundle_commit_completed",
        "bundle_commit_recovered",
        "recurring_schedule_created",
        "recurring_authority_expired",
        "recurring_authority_rejected",
        "policy_default_applied",
    }
)

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "password",
    "private_key",
    "secret",
    "token",
}
_SAFE_KEY_NAMES = {"key_id", "public_key", "source_key", "binding_key"}
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)([?&](?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|password|secret|token)=)[^&\s]+"
    ),
    re.compile(
        r"(?i)\b(?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|password|private[_-]?key|secret|token)\s*[:=]\s*[^\s,;]+"
    ),
)


class AuditEventSink(Protocol):
    def append_event(self, event: AuditEvent) -> Any: ...


def redact_payload(value: Any, *, key: str | None = None) -> Any:
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") if key else None
    if normalized_key is not None and (
        normalized_key in _SECRET_KEYS
        or (
            normalized_key not in _SAFE_KEY_NAMES
            and normalized_key.endswith(
                (
                    "_token",
                    "_password",
                    "_secret",
                    "_authorization",
                    "_private_key",
                    "_client_key",
                    "_api_key",
                    "_access_key",
                    "_credential",
                    "_credentials",
                )
            )
        )
    ):
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
    if isinstance(value, BaseException):
        return _redact_text(str(value))
    if isinstance(value, str):
        return _redact_text(value)
    return deepcopy(value)


def _redact_text(value: str) -> str:
    """Mask credential-shaped text without attempting to identify secrets."""

    redacted = value
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                match.group(1) + _REDACTED
                if match.lastindex and match.group(1).endswith("=")
                else _REDACTED
            ),
            redacted,
        )
    return redacted


class AuditLog:
    """Append-only audit emitter with a bounded in-memory retention window.

    The in-memory window is a process-local convenience copy, not the
    durable record -- a configured `sink` still receives every event
    regardless of in-memory eviction, so bounding this window does not
    lose data for callers reading through the sink (Spec 080).
    """

    def __init__(
        self,
        sink: AuditEventSink | None = None,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        clock: Clock | None = None,
        critical_event_types: set[str] | frozenset[str] | None = None,
        critical_delivery_timeout_seconds: float = 5.0,
    ) -> None:
        if critical_delivery_timeout_seconds <= 0:
            raise ValueError("critical audit delivery timeout must be positive")
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._sink = sink
        self.clock = clock or SystemClock()
        self.critical_event_types = frozenset(
            critical_event_types if critical_event_types is not None else CRITICAL_AUDIT_EVENT_TYPES
        )
        self.critical_delivery_timeout_seconds = critical_delivery_timeout_seconds
        self.sink_failure_count = 0
        self.critical_delivery_failure_count = 0
        self.last_sink_error: str | None = None

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
        authority: AuthorityContext | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=str(uuid4()),
            event_type=event_type,
            actor=actor,
            subject_id=subject_id,
            payload=redact_payload(payload),
            created_at=self.clock.now(),
            authority=authority
            or AuthorityContext(
                principal_id=actor,
                roles=[PrincipalRole.SERVICE],
            ),
        )
        self._events.append(event)
        if self._sink is not None:
            # A durable-sink failure (e.g. the storage boundary is
            # momentarily overloaded) must never propagate into the caller:
            # observability degrading to memory-only retention is an
            # acceptable loss, but an in-flight plan/execution/schedule
            # operation aborting because its *audit* write failed is not.
            # The event is never dropped silently -- it stays in the bounded
            # in-memory window above, and the failure is countable via
            # sink_failure_count/last_sink_error for metrics/alerting.
            try:
                acknowledgement = self._sink.append_event(event)
                if event.event_type in self.critical_event_types:
                    wait_for_ack = getattr(acknowledgement, "result", None)
                    if callable(wait_for_ack):
                        wait_for_ack(timeout=self.critical_delivery_timeout_seconds)
            except Exception as error:
                self.sink_failure_count += 1
                if event.event_type in self.critical_event_types:
                    self.critical_delivery_failure_count += 1
                self.last_sink_error = f"{type(error).__name__}: {error}"
        return event
