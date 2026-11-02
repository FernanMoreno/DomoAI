from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_connection_metric_recording_is_bounded_for_many_events() -> None:
    metrics = RuntimeOperationalMetrics()
    for _ in range(1000):
        metrics.record_connection_state("connected", connector_id="mqtt:home")

    snapshot = metrics.snapshot()

    assert snapshot["connections"]["connection_connected"] == 1000
