"""Bounded Prometheus exposition for the live, process-local runtime snapshot."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


class MetricsRenderError(ValueError):
    """Raised when a metrics snapshot cannot be rendered safely."""


_DEFAULT_MAX_BYTES = 262144
_MAX_DYNAMIC_SERIES = 256
_MAX_LABEL_LENGTH = 128
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:/-]+")

_COMMAND_OUTCOMES = (
    "confirmed_success",
    "failed",
    "rejected",
    "unavailable",
    "unknown",
)
_SOURCE_CURSOR_EVENTS = ("gap_total", "replay_total", "resync_total")
_LEASE_EVENTS = (
    "acquire_total",
    "expired_total",
    "released_total",
    "release_failed_total",
    "unknown_total",
)
_APPROVAL_EVENTS = ("reserved_total", "consumed_total", "released_total", "rejected_total")
_BUNDLE_EVENTS = ("completed_total", "partial_total", "unknown_total", "recovered_total")
_FENCING_EVENTS = (
    "acquire_total",
    "renewed_total",
    "renewal_failed_total",
    "takeover_total",
    "lease_lost_total",
    "stale_rejected_total",
)


def render_prometheus_metrics(
    snapshot: Mapping[str, Any], *, max_bytes: int = _DEFAULT_MAX_BYTES
) -> str:
    """Render an allowlisted snapshot as deterministic Prometheus text.

    The collector is deliberately explicit rather than recursively flattening
    JSON. This prevents provider messages, credentials, client claims and
    future high-cardinality fields from becoming a monitoring contract by
    accident.
    """

    if max_bytes <= 0:
        raise MetricsRenderError("metrics size limit must be positive")

    lines: list[str] = []
    _emit(lines, "domoai_up", 1)
    instance_id = snapshot.get("instance_id")
    if isinstance(instance_id, str) and instance_id.strip():
        _emit(lines, "domoai_instance_info", 1, {"instance_id": instance_id})
    _emit_scalar(
        lines,
        snapshot,
        "process_start_time_seconds",
        "domoai_process_start_time_seconds",
    )
    _emit_scalar(lines, snapshot, "event_consumer_alive", "domoai_event_consumer_alive")
    _emit_scalar(lines, snapshot, "scheduler_alive", "domoai_scheduler_alive")
    _emit_scalar(lines, snapshot, "event_count", "domoai_events_applied_total")
    _emit_scalar(lines, snapshot, "event_lag_seconds", "domoai_event_lag_seconds")
    _emit_scalar(lines, snapshot, "stale_state_count", "domoai_stale_state_total")
    _emit_scalar(lines, snapshot, "max_state_age_seconds", "domoai_max_state_age_seconds")
    _emit_scalar(lines, snapshot, "scheduler_lateness_seconds", "domoai_scheduler_lateness_seconds")
    _emit_scalar(
        lines, snapshot, "scheduler_max_lateness_seconds", "domoai_scheduler_max_lateness_seconds"
    )
    _emit_scalar(lines, snapshot, "scheduler_missed_total", "domoai_scheduler_missed_total")
    for key in (
        "execution_unknown_total",
        "execution_unavailable_total",
        "execution_failed_total",
        "execution_partial_total",
        "db_operation_count",
        "dropped_events_total",
        "coalesced_events_total",
    ):
        _emit_scalar(lines, snapshot, key, f"domoai_{key}")

    _emit_mapping(
        lines,
        snapshot.get("event_queue_depth"),
        "domoai_event_queue_depth",
        "priority",
        allowed=("bulk", "priority"),
    )
    _emit_mapping(
        lines,
        snapshot.get("household_queue_depth"),
        "domoai_household_queue_depth",
        "household_id",
    )
    _emit_mapping(
        lines,
        snapshot.get("plans_by_status"),
        "domoai_plans_total",
        "status",
        allowed=("pending", "executing", "unknown"),
    )
    _emit_mapping(
        lines,
        _nested(snapshot, "adapter_reconnect"),
        "domoai_adapter_reconnect_total",
        "outcome",
        allowed=("attempts_total", "success_total", "failure_total"),
    )

    adapter_health = snapshot.get("adapter_health")
    if isinstance(adapter_health, Mapping):
        components = adapter_health.get("components")
        if isinstance(components, Sequence) and not isinstance(components, (str, bytes)):
            if len(components) > _MAX_DYNAMIC_SERIES:
                raise MetricsRenderError("metrics output exceeds size limit")
            for component in sorted(
                (item for item in components if isinstance(item, Mapping)),
                key=lambda item: _label(item.get("adapter_id", "unknown")),
            ):
                _emit(
                    lines,
                    "domoai_adapter_connected",
                    _bool_number(component.get("connected")),
                    {"adapter_id": component.get("adapter_id", "unknown")},
                )
        else:
            _emit(lines, "domoai_adapter_connected", _bool_number(adapter_health.get("connected")))

    for section, prefix in (("storage", "domoai_storage"), ("audit", "domoai_audit")):
        values = _nested(snapshot, section)
        if section == "audit":
            audit_values = _nested(snapshot, "audit")
            audit_storage = dict(_nested(audit_values, "storage"))
            audit_storage["sink_failure_count"] = audit_values.get("sink_failure_count")
            values = audit_storage
        for key in (
            "operation_count",
            "completed_count",
            "failed_count",
            "timeout_count",
            "overloaded_count",
            "queue_depth",
            "max_in_flight",
            "sink_failure_count",
        ):
            _emit_scalar(lines, values, key, f"{prefix}_{key}")

    operational = _nested(snapshot, "operational")
    _emit_allowlisted_map(
        lines,
        operational.get("command_outcomes"),
        "domoai_command_outcome_total",
        "outcome",
        _COMMAND_OUTCOMES,
    )
    _emit_allowlisted_map(
        lines,
        operational.get("source_cursor"),
        "domoai_source_cursor_total",
        "event",
        _SOURCE_CURSOR_EVENTS,
        label_transform=_without_total,
    )
    _emit_allowlisted_map(
        lines,
        operational.get("leases"),
        "domoai_lease_event_total",
        "event",
        _LEASE_EVENTS,
        label_transform=_without_total,
    )
    _emit_allowlisted_map(
        lines,
        operational.get("approvals"),
        "domoai_approval_event_total",
        "event",
        _APPROVAL_EVENTS,
        label_transform=_without_total,
    )
    _emit_allowlisted_map(
        lines,
        operational.get("bundles"),
        "domoai_bundle_event_total",
        "event",
        _BUNDLE_EVENTS,
        label_transform=_without_total,
    )
    _emit_allowlisted_map(
        lines,
        operational.get("fencing"),
        "domoai_fencing_event_total",
        "event",
        _FENCING_EVENTS,
        label_transform=_without_total,
    )
    for key in (
        "readback_mismatch_total",
        "series_overflow_total",
        "telemetry_failure_total",
    ):
        _emit_scalar(lines, operational, key, f"domoai_{key}")

    latency_series = operational.get("command_latency_ms")
    if isinstance(latency_series, Sequence) and not isinstance(latency_series, (str, bytes)):
        if len(latency_series) > _MAX_DYNAMIC_SERIES:
            raise MetricsRenderError("metrics output exceeds size limit")
        normalized_series = [
            item
            for item in latency_series
            if isinstance(item, Mapping)
            and isinstance(item.get("adapter_id"), str)
            and isinstance(item.get("capability"), str)
        ]
        for series in sorted(
            normalized_series,
            key=lambda item: (
                _label(item.get("adapter_id")),
                _label(item.get("capability")),
            ),
        ):
            labels = {
                "adapter_id": series["adapter_id"],
                "capability": series["capability"],
            }
            for key, suffix in (
                ("count", "count"),
                ("total", "sum_ms"),
                ("last", "last_ms"),
                ("max", "max_ms"),
            ):
                _emit(lines, f"domoai_command_latency_{suffix}", series.get(key), labels)

    output = "\n".join(lines) + "\n"
    if len(output.encode("utf-8")) > max_bytes:
        raise MetricsRenderError("metrics output exceeds size limit")
    return output


def _nested(value: Mapping[str, Any] | Any, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, Mapping) else {}


def _emit_scalar(lines: list[str], values: Mapping[str, Any] | Any, key: str, name: str) -> None:
    if isinstance(values, Mapping) and key in values:
        _emit(lines, name, values[key])


def _emit_mapping(
    lines: list[str],
    values: Mapping[str, Any] | Any,
    name: str,
    label_name: str,
    *,
    allowed: Sequence[str] | None = None,
) -> None:
    if not isinstance(values, Mapping):
        return
    keys = allowed if allowed is not None else tuple(sorted(str(key) for key in values))
    for key in keys:
        if key in values:
            _emit(lines, name, values[key], {label_name: key})


def _emit_allowlisted_map(
    lines: list[str],
    values: Mapping[str, Any] | Any,
    name: str,
    label_name: str,
    keys: Sequence[str],
    *,
    label_transform: Any = lambda value: value,
) -> None:
    if not isinstance(values, Mapping):
        values = {}
    for key in keys:
        _emit(lines, name, values.get(key, 0), {label_name: label_transform(key)})


def _emit(lines: list[str], name: str, value: Any, labels: Mapping[str, Any] | None = None) -> None:
    normalized = _number(value)
    if normalized is None:
        return
    suffix = ""
    if labels:
        suffix = "{" + ",".join(
            f'{key}="{_escape_label(labels[key])}"' for key in sorted(labels)
        ) + "}"
    lines.append(f"{name}{suffix} {normalized}")


def _number(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, (int, float)) or isinstance(value, complex):
        return None
    if not math.isfinite(float(value)):
        return None
    return str(value)


def _bool_number(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _label(value: Any) -> str:
    normalized = _SAFE_LABEL.sub("_", str(value).strip())[:_MAX_LABEL_LENGTH]
    return normalized or "unknown"


def _escape_label(value: Any) -> str:
    return _label(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _without_total(value: str) -> str:
    return value.removesuffix("_total")
