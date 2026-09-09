from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


def test_connection_metrics_do_not_export_credentials_or_raw_serial_labels() -> None:
    metrics = RuntimeOperationalMetrics()

    metrics.record_connection_state("connected", connector_id="mqtt:home")
    metrics.record_connection_state("connected", connector_id="token=super-secret")
    metrics.record_connection_state("connected", connector_id="serial-1234567890")

    rendered = repr(metrics.snapshot())
    assert "super-secret" not in rendered
    assert "1234567890" not in rendered
