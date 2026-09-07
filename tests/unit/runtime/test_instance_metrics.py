from datetime import UTC, datetime

from domoai.runtime.instance import InstanceIdentity
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_instance_identity_is_bounded_and_process_local() -> None:
    identity = InstanceIdentity(
        instance_id="host-a",
        process_start_time=datetime(2026, 9, 5, 12, tzinfo=UTC),
    )
    metrics = RuntimeOperationalMetrics(instance_identity=identity)
    metrics.record_fencing("stale_rejected")

    snapshot = metrics.snapshot()

    assert snapshot["instance_id"] == "host-a"
    assert snapshot["process_start_time"] == "2026-09-05T12:00:00+00:00"
    assert snapshot["fencing"] == {
        "acquire_total": 0,
        "renewed_total": 0,
        "renewal_failed_total": 0,
        "takeover_total": 0,
        "lease_lost_total": 0,
        "stale_rejected_total": 1,
    }


def test_invalid_fencing_event_is_counted_without_new_series() -> None:
    metrics = RuntimeOperationalMetrics()

    metrics.record_fencing("not-allowlisted")

    snapshot = metrics.snapshot()
    assert snapshot["telemetry_failure_total"] == 1
