from __future__ import annotations

import pytest

from domoai.mcp.remote_metrics import MetricsRenderError, render_prometheus_metrics


def _snapshot() -> dict[str, object]:
    return {
        "event_consumer_alive": True,
        "scheduler_alive": False,
        "event_queue_depth": {"bulk": 2, "priority": 1},
        "dropped_events_total": 3,
        "stale_state_count": 4,
        "db_operation_count": 5,
        "adapter_health": {
            "connected": True,
            "components": [
                {"adapter_id": "home_assistant", "connected": True},
                {"adapter_id": "bad\"adapter", "connected": False},
            ],
        },
        "storage": {"operation_count": 6, "overloaded_count": 1},
        "audit": {"sink_failure_count": 2},
        "operational": {
            "command_outcomes": {"confirmed_success": 7, "failed": 1},
            "command_latency_ms": [
                {
                    "adapter_id": "home_assistant",
                    "capability": "light.set_power",
                    "count": 8,
                    "total": 12.5,
                    "last": 1.5,
                    "max": 4.0,
                }
            ],
            "readback_mismatch_total": 1,
            "series_overflow_total": 2,
            "telemetry_failure_total": 3,
            "source_cursor": {"gap_total": 4},
            "leases": {"acquire_total": 5},
            "approvals": {"reserved_total": 6},
            "bundles": {"completed_total": 7},
            "token_hash": "must-not-be-exported",
        },
        "client_id": "codex",
        "secret": "must-not-be-exported",
    }


def test_renderer_is_deterministic_bounded_and_secret_free() -> None:
    snapshot = _snapshot()

    rendered = render_prometheus_metrics(snapshot)

    assert rendered == render_prometheus_metrics(snapshot)
    assert 'domoai_up 1' in rendered
    assert 'domoai_command_outcome_total{outcome="confirmed_success"} 7' in rendered
    assert (
        'domoai_command_latency_count{adapter_id="home_assistant",'
        'capability="light.set_power"} 8'
    ) in rendered
    assert 'domoai_adapter_connected{adapter_id="bad_adapter"} 0' in rendered
    assert "must-not-be-exported" not in rendered
    assert "codex" not in rendered
    assert "token_hash" not in rendered
    assert len(rendered.encode("utf-8")) <= 262144


def test_renderer_rejects_an_exposition_that_exceeds_the_hard_bound() -> None:
    with pytest.raises(MetricsRenderError, match="size limit"):
        render_prometheus_metrics(_snapshot(), max_bytes=32)
