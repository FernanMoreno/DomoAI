from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_snapshot_has_stable_operational_counters() -> None:
    metrics = RuntimeOperationalMetrics()

    snapshot = metrics.snapshot()

    assert snapshot == {
        "instance_id": snapshot["instance_id"],
        "process_start_time": snapshot["process_start_time"],
        "process_start_time_seconds": snapshot["process_start_time_seconds"],
        "command_latency_ms": [],
        "command_outcomes": {
            "confirmed_success": 0,
            "rejected": 0,
            "unavailable": 0,
            "unknown": 0,
            "failed": 0,
        },
        "readback_mismatch_total": 0,
        "source_cursor": {
            "gap_total": 0,
            "replay_total": 0,
            "resync_total": 0,
        },
        "leases": {
            "acquire_total": 0,
            "expired_total": 0,
            "released_total": 0,
            "release_failed_total": 0,
            "unknown_total": 0,
        },
        "approvals": {
            "reserved_total": 0,
            "consumed_total": 0,
            "released_total": 0,
            "rejected_total": 0,
        },
        "bundles": {
            "completed_total": 0,
            "partial_total": 0,
            "unknown_total": 0,
            "recovered_total": 0,
        },
        "fencing": {
            "acquire_total": 0,
            "renewed_total": 0,
            "renewal_failed_total": 0,
            "takeover_total": 0,
            "lease_lost_total": 0,
            "stale_rejected_total": 0,
        },
        "series_overflow_total": 0,
        "telemetry_failure_total": 0,
    }


def test_command_series_and_fixed_outcomes_are_recorded() -> None:
    metrics = RuntimeOperationalMetrics(max_command_series=2)

    metrics.record_command("ha", "power", 12.5, "confirmed_success")
    metrics.record_command("ha", "power", 7.5, "unavailable")
    metrics.record_command("knx", "power", 2, "rejected")

    snapshot = metrics.snapshot()
    assert snapshot["command_latency_ms"] == [
        {
            "adapter_id": "ha",
            "capability": "power",
            "count": 2,
            "total": 20.0,
            "last": 7.5,
            "max": 12.5,
        },
        {
            "adapter_id": "knx",
            "capability": "power",
            "count": 1,
            "total": 2.0,
            "last": 2.0,
            "max": 2.0,
        },
    ]
    assert snapshot["command_outcomes"] == {
        "confirmed_success": 1,
        "rejected": 1,
        "unavailable": 1,
        "unknown": 0,
        "failed": 0,
    }


def test_invalid_or_overflow_data_is_visible_but_bounded() -> None:
    metrics = RuntimeOperationalMetrics(max_command_series=1)

    metrics.record_command("ha", "power", math.nan, "confirmed_success")
    metrics.record_command("ha", "power", 3, "confirmed_success")
    metrics.record_command("knx", "power", 4, "confirmed_success")
    metrics.record_command("", "power", 4, "confirmed_success")

    snapshot = metrics.snapshot()
    assert len(snapshot["command_latency_ms"]) == 1
    assert snapshot["series_overflow_total"] == 1
    assert snapshot["telemetry_failure_total"] == 2
    assert snapshot["command_outcomes"]["confirmed_success"] == 2


def test_transition_counters_and_snapshot_are_isolated() -> None:
    metrics = RuntimeOperationalMetrics()

    metrics.record_readback_mismatch()
    metrics.record_source_cursor("gap")
    metrics.record_source_cursor("replay")
    metrics.record_source_cursor("resync")
    metrics.record_lease("acquire")
    metrics.record_lease("expired")
    metrics.record_lease("released")
    metrics.record_lease("release_failed")
    metrics.record_lease("unknown")
    metrics.record_approval("reserved")
    metrics.record_approval("consumed")
    metrics.record_approval("released")
    metrics.record_approval("rejected")
    metrics.record_bundle("completed")
    metrics.record_bundle("partial")
    metrics.record_bundle("unknown")
    metrics.record_bundle("recovered")

    first = metrics.snapshot()
    first["source_cursor"]["gap_total"] = 99
    second = metrics.snapshot()

    assert second["readback_mismatch_total"] == 1
    assert second["source_cursor"] == {
        "gap_total": 1,
        "replay_total": 1,
        "resync_total": 1,
    }
    assert second["leases"] == {
        "acquire_total": 1,
        "expired_total": 1,
        "released_total": 1,
        "release_failed_total": 1,
        "unknown_total": 1,
    }
    assert second["approvals"] == {
        "reserved_total": 1,
        "consumed_total": 1,
        "released_total": 1,
        "rejected_total": 1,
    }
    assert second["bundles"] == {
        "completed_total": 1,
        "partial_total": 1,
        "unknown_total": 1,
        "recovered_total": 1,
    }


def test_recording_is_thread_safe() -> None:
    metrics = RuntimeOperationalMetrics()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _: metrics.record_command("ha", "power", 1.0, "confirmed_success"),
                range(100),
            )
        )

    snapshot = metrics.snapshot()
    assert snapshot["command_outcomes"]["confirmed_success"] == 100
    assert snapshot["command_latency_ms"][0]["count"] == 100
