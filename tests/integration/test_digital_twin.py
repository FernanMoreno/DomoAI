from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import (
    Command,
    ExecutionStatus,
    Plan,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from domoai.runtime.twin import DigitalTwin


async def _live_context() -> tuple[SimulatedHomeAdapter, DeviceRegistry, StateStore]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    return adapter, registry, state_store


@pytest.mark.asyncio
async def test_twin_sync_mirrors_representable_devices_under_real_ids() -> None:
    _, registry, state_store = await _live_context()
    live_device_ids = {device.id for device in registry.devices}

    twin = DigitalTwin()
    report = await twin.sync(registry, state_store)

    representable = {
        device.id
        for device in registry.devices
        if device.type.value in {"light", "switch", "cover"}
    }
    assert representable
    assert set(report.mirrored_device_ids) == representable
    assert representable.issubset(live_device_ids)


@pytest.mark.asyncio
async def test_twin_preview_reflects_mirrored_current_state() -> None:
    adapter, registry, state_store = await _live_context()
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")

    await state_store.save(
        StateSnapshot(
            device_id=switch_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=switch_id),
        )
    )

    twin = DigitalTwin()
    await twin.sync(registry, state_store)

    plan = Plan(
        id="twin-preview-1",
        commands=[
            Command(
                id="twin-preview-1:command",
                device_id=switch_id,
                command="turn_off",
                idempotency_key="twin-preview-1:intent",
            )
        ],
    )
    summary = await twin.validate_and_execute(plan)

    assert len(summary.outcomes) == 1
    outcome = summary.outcomes[0]
    assert outcome.status is ExecutionStatus.CONFIRMED_SUCCESS
    assert outcome.before_state is not None
    assert outcome.before_state.value is True
    assert outcome.after_state is not None
    assert outcome.after_state.value is False


@pytest.mark.asyncio
async def test_twin_preview_uses_real_device_id_unmodified() -> None:
    _, registry, state_store = await _live_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")

    twin = DigitalTwin()
    await twin.sync(registry, state_store)

    plan = Plan(
        id="twin-real-id-1",
        commands=[
            Command(
                id="twin-real-id-1:command",
                device_id=light_id,
                command="turn_on",
                idempotency_key="twin-real-id-1:intent",
            )
        ],
    )
    summary = await twin.validate_and_execute(plan)

    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS


@pytest.mark.asyncio
async def test_twin_reports_non_representable_device_types() -> None:
    _, registry, state_store = await _live_context()
    climate_id = next(
        device.id for device in registry.devices if device.type.value == "climate"
    )

    twin = DigitalTwin()
    report = await twin.sync(registry, state_store)

    not_mirrored_ids = {entry["device_id"] for entry in report.not_mirrored}
    assert climate_id in not_mirrored_ids
    representable = {
        device.id
        for device in registry.devices
        if device.type.value in {"light", "switch", "cover"}
    }
    assert representable.issubset(set(report.mirrored_device_ids))


@pytest.mark.asyncio
async def test_twin_preview_reports_missing_device_when_targeting_non_representable_device() -> (
    None
):
    _, registry, state_store = await _live_context()
    climate_id = next(
        device.id for device in registry.devices if device.type.value == "climate"
    )

    twin = DigitalTwin()
    await twin.sync(registry, state_store)

    plan = Plan(
        id="twin-missing-1",
        commands=[
            Command(
                id="twin-missing-1:command",
                device_id=climate_id,
                command="turn_on",
                idempotency_key="twin-missing-1:intent",
            )
        ],
    )
    validated = twin.plan_service.validate(plan)  # type: ignore[union-attr]

    assert validated.validation is not None
    assert validated.validation.status.value == "invalid"


@pytest.mark.asyncio
async def test_twin_sync_and_preview_never_touch_live_registry_or_state() -> None:
    _, registry, state_store = await _live_context()
    live_devices_before = list(registry.devices)
    live_snapshots_before = await state_store.all()

    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    twin = DigitalTwin()
    await twin.sync(registry, state_store)
    plan = Plan(
        id="twin-isolation-1",
        commands=[
            Command(
                id="twin-isolation-1:command",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="twin-isolation-1:intent",
            )
        ],
    )
    await twin.validate_and_execute(plan)

    assert list(registry.devices) == live_devices_before
    assert await state_store.all() == live_snapshots_before


@pytest.mark.asyncio
async def test_twin_preview_issues_zero_commands_to_live_adapter() -> None:
    live_adapter, registry, state_store = await _live_context()
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")

    twin = DigitalTwin()
    await twin.sync(registry, state_store)
    plan = Plan(
        id="twin-zero-commands-1",
        commands=[
            Command(
                id="twin-zero-commands-1:command",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="twin-zero-commands-1:intent",
            )
        ],
    )
    await twin.validate_and_execute(plan)

    assert live_adapter.calls == []


@pytest.mark.asyncio
async def test_twin_preview_before_any_sync_reports_device_not_found() -> None:
    twin = DigitalTwin()
    plan = Plan(
        id="twin-never-synced-1",
        commands=[
            Command(
                id="twin-never-synced-1:command",
                device_id="switch.anything",
                command="turn_on",
                idempotency_key="twin-never-synced-1:intent",
            )
        ],
    )

    summary = await twin.validate_and_execute(plan)

    assert summary.outcomes == []
