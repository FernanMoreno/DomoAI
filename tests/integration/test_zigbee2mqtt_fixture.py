import pytest

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import InMemoryMqttTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, PlanStatus
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.zigbee2mqtt import retained_messages, state_message


@pytest.mark.asyncio
async def test_twenty_device_fixture_discovers_stable_registry_entries() -> None:
    transport = InMemoryMqttTransport(retained_messages(20))
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    await adapter.connect()
    registry = DeviceRegistry()
    state_store = StateStore()
    discovery = DiscoveryService(adapter, registry, state_store, AuditLog())

    first = await discovery.refresh()
    second = await discovery.refresh()

    assert len(first.devices) == 20
    assert [device.id for device in first.devices] == [device.id for device in second.devices]
    assert all(device.protocol == "zigbee2mqtt" for device in first.devices)
    assert first.devices[0].source_refs[0].adapter_id == "zigbee2mqtt"
    health = await adapter.health()
    assert health.adapter_id == "zigbee2mqtt"
    assert health.connected is True


@pytest.mark.asyncio
async def test_plan_executor_reaches_zigbee2mqtt_adapter() -> None:
    transport = InMemoryMqttTransport(retained_messages())
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()

    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    plan = plan_service.create_plan(
        "plan-zigbee-fixture",
        [
            Command(
                id="command-zigbee-fixture",
                device_id="unassigned.living-room-main-light",
                command="turn_on",
                idempotency_key="intent-zigbee-fixture",
            )
        ],
    )

    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert validated.status is PlanStatus.READY
    assert summary.outcomes[0].status.value == "confirmed_success"
    assert len(transport.published) == 1
    assert transport.published[0].topic == "zigbee2mqtt/living_room/main_light/set"
    assert transport.published[0].payload == b'{"state":"ON"}'


@pytest.mark.asyncio
async def test_runtime_event_consumer_can_refresh_after_zigbee_state_event() -> None:
    transport = InMemoryMqttTransport(retained_messages())
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await adapter.connect()
    await discovery.refresh()
    transport.incoming.append(state_message("zigbee2mqtt/living_room/main_light", {"state": "OFF"}))

    event = await RuntimeEventConsumer(
        adapter,
        discovery,
        state_store,
        audit,
    ).consume_once()

    assert event is not None
    assert event.kind == "state_changed"
    assert event.payload["friendly_name"] == "living_room/main_light"
    assert registry.get("unassigned.living-room-main-light") is not None
