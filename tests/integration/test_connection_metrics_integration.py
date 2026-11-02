from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_connection_lifecycle_metric_snapshot_is_detached() -> None:
    metrics = RuntimeOperationalMetrics()
    metrics.record_connection_total("matter:home")
    first = metrics.snapshot()
    first["connections"]["connection_total"] = 0

    assert metrics.snapshot()["connections"]["connection_total"] == 1
