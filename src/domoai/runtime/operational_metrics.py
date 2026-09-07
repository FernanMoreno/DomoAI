"""Bounded, process-local operational metrics for the live runtime."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from threading import RLock
from typing import Any

from domoai.runtime.instance import InstanceIdentity

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_MAX_LABEL_LENGTH = 128

_COMMAND_OUTCOMES = (
    "confirmed_success",
    "rejected",
    "unavailable",
    "unknown",
    "failed",
)
_SOURCE_CURSOR_EVENTS = {
    "gap": "gap_total",
    "replay": "replay_total",
    "resync": "resync_total",
}
_LEASE_EVENTS = {
    "acquire": "acquire_total",
    "expired": "expired_total",
    "released": "released_total",
    "release_failed": "release_failed_total",
    "unknown": "unknown_total",
}
_APPROVAL_EVENTS = {
    "reserved": "reserved_total",
    "consumed": "consumed_total",
    "released": "released_total",
    "rejected": "rejected_total",
}
_BUNDLE_EVENTS = {
    "completed": "completed_total",
    "partial": "partial_total",
    "unknown": "unknown_total",
    "recovered": "recovered_total",
}
_FENCING_EVENTS = {
    "acquire": "acquire_total",
    "renewed": "renewed_total",
    "renewal_failed": "renewal_failed_total",
    "takeover": "takeover_total",
    "lease_lost": "lease_lost_total",
    "stale_rejected": "stale_rejected_total",
}


@dataclass
class _CommandLatency:
    count: int = 0
    total: float = 0.0
    last: float = 0.0
    maximum: float = 0.0


class RuntimeOperationalMetrics:
    """Thread-safe and bounded counters for current-process diagnostics.

    This object is deliberately not a domain authority or durable history.
    Producers may call it from synchronous or asynchronous paths; recording
    a malformed value is best-effort and never raises into the observed path.
    """

    DEFAULT_MAX_COMMAND_SERIES = 256

    def __init__(
        self,
        *,
        max_command_series: int = DEFAULT_MAX_COMMAND_SERIES,
        instance_identity: InstanceIdentity | None = None,
    ) -> None:
        if max_command_series <= 0:
            raise ValueError("max_command_series must be positive")
        self._max_command_series = max_command_series
        self.instance_identity = instance_identity or InstanceIdentity.create()
        self._lock = RLock()
        self._commands: dict[tuple[str, str], _CommandLatency] = {}
        self._command_outcomes = {key: 0 for key in _COMMAND_OUTCOMES}
        self._readback_mismatch_total = 0
        self._source_cursor = {
            "gap_total": 0,
            "replay_total": 0,
            "resync_total": 0,
        }
        self._leases = {
            "acquire_total": 0,
            "expired_total": 0,
            "released_total": 0,
            "release_failed_total": 0,
            "unknown_total": 0,
        }
        self._approvals = {
            "reserved_total": 0,
            "consumed_total": 0,
            "released_total": 0,
            "rejected_total": 0,
        }
        self._bundles = {
            "completed_total": 0,
            "partial_total": 0,
            "unknown_total": 0,
            "recovered_total": 0,
        }
        self._fencing = {value: 0 for value in _FENCING_EVENTS.values()}
        self._series_overflow_total = 0
        self._telemetry_failure_total = 0

    def record_command(
        self,
        adapter_id: str,
        capability: str,
        elapsed_ms: float,
        outcome: str,
    ) -> None:
        """Record one adapter dispatch without allowing telemetry to escape."""

        try:
            normalized_adapter = self._label(adapter_id)
            normalized_capability = self._label(capability)
            if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
                raise ValueError("command latency must be finite and non-negative")
            normalized_outcome = self._outcome(outcome)
        except (TypeError, ValueError):
            self._record_failure()
            return

        with self._lock:
            self._command_outcomes[normalized_outcome] += 1
            key = (normalized_adapter, normalized_capability)
            series = self._commands.get(key)
            if series is None:
                if len(self._commands) >= self._max_command_series:
                    self._series_overflow_total += 1
                    return
                series = _CommandLatency()
                self._commands[key] = series
            series.count += 1
            series.total += float(elapsed_ms)
            series.last = float(elapsed_ms)
            series.maximum = max(series.maximum, float(elapsed_ms))

    def record_readback_mismatch(self) -> None:
        self._increment("_readback_mismatch_total")

    def record_source_cursor(self, event: str) -> None:
        self._record_named(event, _SOURCE_CURSOR_EVENTS, self._source_cursor)

    def record_lease(self, event: str) -> None:
        self._record_named(event, _LEASE_EVENTS, self._leases)

    def record_approval(self, event: str) -> None:
        self._record_named(event, _APPROVAL_EVENTS, self._approvals)

    def record_bundle(self, event: str) -> None:
        self._record_named(event, _BUNDLE_EVENTS, self._bundles)

    def record_fencing(self, event: str) -> None:
        self._record_named(event, _FENCING_EVENTS, self._fencing)

    def record_telemetry_failure(self) -> None:
        self._record_failure()

    def snapshot(self) -> dict[str, Any]:
        """Return a detached JSON-ready view of all operational counters."""

        with self._lock:
            return {
                "instance_id": self.instance_identity.instance_id,
                "process_start_time": self.instance_identity.process_start_time.isoformat(),
                "process_start_time_seconds": self.instance_identity.process_start_time.timestamp(),
                "command_latency_ms": [
                    {
                        "adapter_id": adapter_id,
                        "capability": capability,
                        "count": series.count,
                        "total": series.total,
                        "last": series.last,
                        "max": series.maximum,
                    }
                    for (adapter_id, capability), series in self._commands.items()
                ],
                "command_outcomes": dict(self._command_outcomes),
                "readback_mismatch_total": self._readback_mismatch_total,
                "source_cursor": dict(self._source_cursor),
                "leases": dict(self._leases),
                "approvals": dict(self._approvals),
                "bundles": dict(self._bundles),
                "fencing": dict(self._fencing),
                "series_overflow_total": self._series_overflow_total,
                "telemetry_failure_total": self._telemetry_failure_total,
            }

    @staticmethod
    def _label(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("metric label must be a string")
        normalized = value.strip()
        if not normalized or not _SAFE_LABEL.fullmatch(normalized):
            raise ValueError("metric label is not safe")
        return normalized[:_MAX_LABEL_LENGTH]

    @staticmethod
    def _outcome(value: str) -> str:
        if not isinstance(value, str) or value not in _COMMAND_OUTCOMES:
            raise ValueError("metric command outcome is not supported")
        return value

    def _record_named(
        self,
        event: str,
        mapping: dict[str, str],
        counters: dict[str, int],
    ) -> None:
        try:
            key = mapping[event]
        except (KeyError, TypeError):
            self._record_failure()
            return
        with self._lock:
            counters[key] += 1

    def _increment(self, attribute: str) -> None:
        with self._lock:
            setattr(self, attribute, getattr(self, attribute) + 1)

    def _record_failure(self) -> None:
        with self._lock:
            self._telemetry_failure_total += 1
