from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.domain.models import AdapterHealth, SourceRef, StateSnapshot, StateStatus
from domoai.mcp.gateway import GatewayApplication, create_gateway_server
from domoai.mcp.health import readyz
from domoai.mcp.unified_server import UnifiedMcpContext
from tests.unit.mcp.test_gateway import _build_context


class _Lifecycle:
    started = True
    closed = False
    running_task_count = 2


class _Ownership:
    released = False


class _Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        battery_qualification: str = "unsupported",
        knx_connected: bool = False,
        knx_message: str | None = "secret=redact-me",
    ):
        self.settings = settings
        self.lifecycle = _Lifecycle()
        self.ownership = _Ownership()
        self.battery_qualification = battery_qualification
        self.dispatchable_battery_binding = None
        self.knx_connected = knx_connected
        self.knx_message = knx_message
        self.state_store = None

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            adapter_id="composite",
            connected=True,
            message="provider secret must never be returned",
            components=[
                AdapterHealth(adapter_id="home_assistant", connected=True),
                AdapterHealth(
                    adapter_id="knx",
                    connected=self.knx_connected,
                    message=self.knx_message,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_health_contract_separates_liveness_readiness_and_physical_status(
    tmp_path: Path,
) -> None:
    context: UnifiedMcpContext = await _build_context()
    settings = Settings(database_path=tmp_path / "gateway.sqlite3")
    runtime = _Runtime(settings)
    gateway = GatewayApplication(
        runtime=runtime,
        server=create_gateway_server(context, settings, runtime=runtime),
    )

    async with gateway.http_client() as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"service": "domoai", "status": "ok"}
    assert ready.status_code == 503
    payload = ready.json()
    assert payload["status"] == "not_ready"
    assert payload["runtime"]["status"] == "ready"
    assert payload["adapter"]["components"][1]["status"] == "unavailable"
    assert payload["freshness"]["status"] == "unknown"
    assert payload["physical"]["status"] == "ready"
    assert "secret" not in ready.text.lower()


@pytest.mark.asyncio
async def test_readiness_blocks_unqualified_battery_without_losing_liveness(tmp_path: Path) -> None:
    context: UnifiedMcpContext = await _build_context()
    settings = Settings(database_path=tmp_path / "gateway.sqlite3")
    runtime = _Runtime(settings, battery_qualification="software-qualified")
    gateway = GatewayApplication(
        runtime=runtime,
        server=create_gateway_server(context, settings, runtime=runtime),
    )

    async with gateway.http_client() as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["physical"] == {
        "status": "not_ready",
        "battery_qualification": "software-qualified",
    }


@pytest.mark.asyncio
async def test_readyz_reports_knx_unavailable_without_exposing_adapter_message(
    tmp_path: Path,
) -> None:
    context: UnifiedMcpContext = await _build_context()
    settings = Settings(database_path=tmp_path / "gateway.sqlite3")
    runtime = _Runtime(settings, knx_connected=False, knx_message="secret=knx-details")

    gateway = GatewayApplication(
        runtime=runtime,
        server=create_gateway_server(context, settings, runtime=runtime),
    )

    async with gateway.http_client() as client:
        ready = await client.get("/readyz")

    assert ready.status_code == 503
    assert "knx_unavailable" in ready.json()["reason_codes"]
    assert "knx-details" not in ready.text


@pytest.mark.asyncio
async def test_readyz_direct_contract_contains_no_raw_runtime_object() -> None:
    runtime = _Runtime(Settings())
    response = await readyz(runtime, object())  # type: ignore[arg-type]

    assert response.status_code == 503
    assert "_Runtime" not in str(response.body)
    assert "secret" not in response.body.decode().lower()


@pytest.mark.asyncio
async def test_readyz_and_state_reads_share_jit_freshness_classification(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "gateway.sqlite3")
    runtime = _Runtime(settings, knx_connected=True)
    from domoai.runtime.clock import FixedClock
    from domoai.runtime.state_store import StateStore

    clock = FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC))
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="fixture-device",
        capability="temperature",
        value=20,
        observed_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        received_at=datetime(2026, 8, 23, 11, 59, tzinfo=UTC),
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="temperature"),
    )
    await store.save(snapshot)
    runtime.state_store = store

    ready = await readyz(runtime, object())  # type: ignore[arg-type]
    states = await StateService(store).get(["fixture-device"])

    assert ready.status_code == 200
    assert ready.body is not None
    assert '"freshness":{"status":"current"' in ready.body.decode()
    assert states[0].status is StateStatus.CURRENT


@pytest.mark.asyncio
async def test_optional_unavailable_matter_node_does_not_mask_required_state(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        matter_optional_node_ids=(5,),
    )
    runtime = _Runtime(settings, knx_connected=True)
    from domoai.runtime.clock import FixedClock
    from domoai.runtime.state_store import StateStore

    clock = FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC))
    store = StateStore(timedelta(minutes=5), clock=clock)
    common = {
        "observed_at": clock.now(),
        "received_at": clock.now(),
    }
    await store.save(
        StateSnapshot(
            device_id="fixture-device",
            capability="temperature",
            value=20,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="temperature"),
            **common,
        )
    )
    await store.save(
        StateSnapshot(
            device_id="matter-device",
            capability="power",
            value=None,
            status=StateStatus.UNAVAILABLE,
            source_ref=SourceRef(adapter_id="matter", external_id="node:5/endpoint:1"),
            **common,
        )
    )
    runtime.state_store = store

    ready = await readyz(runtime, object())  # type: ignore[arg-type]
    payload = json.loads(ready.body)

    assert ready.status_code == 200
    assert payload["freshness"]["status"] == "current"
    assert payload["freshness"]["optional_snapshot_count"] == 1
    assert "optional_state_unavailable" in payload["freshness"]["reason_codes"]
