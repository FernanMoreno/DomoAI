import math

from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_connection_metrics_expose_exact_p1_names_and_lifecycle_counts() -> None:
    metrics = RuntimeOperationalMetrics()

    metrics.record_connection_total("mqtt:home")
    metrics.record_connection_state("connected", connector_id="mqtt:home")
    metrics.record_connection_state("degraded", connector_id="mqtt:home")
    metrics.record_connection_state("unavailable", connector_id="mqtt:home")
    metrics.record_connection_reconnect_attempt("mqtt:home")
    metrics.record_connection_reconnect_success("mqtt:home")
    metrics.record_connection_auth_failure("mqtt:home")
    metrics.record_connection_discovery_duration(0.25, connector_id="mqtt:home")
    metrics.record_connection_validation_duration(0.5, connector_id="mqtt:home")

    snapshot = metrics.snapshot()

    connections = snapshot["connections"]
    assert connections["connection_total"] == 1
    assert connections["connection_connected"] == 1
    assert connections["connection_degraded"] == 1
    assert connections["connection_unavailable"] == 1
    assert connections["connection_reconnect_attempt_total"] == 1
    assert connections["connection_reconnect_success_total"] == 1
    assert connections["connection_auth_failure_total"] == 1
    assert connections["connection_discovery_duration_seconds"]["count"] == 1
    assert connections["connection_validation_duration_seconds"]["total"] == 0.5


def test_connection_metric_labels_reject_sensitive_endpoints_and_invalid_durations() -> None:
    metrics = RuntimeOperationalMetrics()

    metrics.record_connection_state("connected", connector_id="192.168.1.10")
    metrics.record_connection_state("connected", connector_id="mqtt:home")
    metrics.record_connection_discovery_duration(math.nan, connector_id="mqtt:home")
    metrics.record_connection_validation_duration(-1, connector_id="mqtt:home")

    snapshot = metrics.snapshot()

    assert "192.168.1.10" not in str(snapshot)
    assert snapshot["connections"]["connection_connected"] == 1
    assert snapshot["telemetry_failure_total"] == 3
