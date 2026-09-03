import pytest

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, PlanStatus, StateStatus
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.matter_server import event_message, node_snapshot, node_snapshots, server_info


@pytest.mark.asyncio
async def test_twenty_matter_endpoints_discover_stable_registry_entries() -> None:
    nodes = node_snapshots(20)
    transport = InMemoryMatterTransport(nodes=nodes, server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    discovery = DiscoveryService(adapter, registry, state_store, audit)

    first = await discovery.refresh()
    second = await discovery.refresh()
    health = await adapter.health()

    assert len(first.devices) == 20
    assert [device.id for device in first.devices] == [device.id for device in second.devices]
    assert all(device.protocol == "matter" for device in first.devices)
    assert first.devices[0].source_refs[0].external_id == "node:1001/endpoint:1"
    assert health.connected is True


@pytest.mark.asyncio
async def test_matter_events_refresh_canonical_state_and_availability() -> None:
    nodes = node_snapshots(1)
    transport = InMemoryMatterTransport(nodes=nodes, server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    nodes[0] = node_snapshot(1001, available=False, on=False)
    transport.enqueue(event_message("node_updated", nodes[0]))
    event = await consumer.consume_once()

    assert event is not None
    assert event.kind in {"availability_changed", "state_changed"}
    device_id = registry.canonical_id_for_source("matter", "node:1001/endpoint:1")
    assert device_id is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_removed_matter_node_remains_traceable_and_unavailable() -> None:
    nodes = node_snapshots(1)
    transport = InMemoryMatterTransport(nodes=nodes, server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    transport.nodes.clear()
    transport.enqueue(event_message("node_removed", 1001))
    event = await consumer.consume_once()

    assert event is not None
    assert event.kind == "device_membership_changed"
    device_id = registry.canonical_id_for_source("matter", "node:1001/endpoint:1")
    assert device_id is not None
    assert registry.get(device_id) is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_plan_executor_reaches_matter_adapter_with_readback() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    plan = plan_service.create_plan(
        "matter-plan-1",
        [
            Command(
                id="matter-command-1",
                device_id="unassigned.matter-fixture-1001",
                command="turn_on",
                idempotency_key="matter-intent-plan-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert validated.status is PlanStatus.READY
    assert summary.outcomes[0].status.value == "confirmed_success"
    command_requests = [
        request for request in transport.requests if request.command == "device_command"
    ]
    read_requests = [
        request for request in transport.requests if request.command == "read_attribute"
    ]
    assert command_requests[-1].args == {
        "node_id": 1001,
        "endpoint_id": 1,
        "cluster_id": 6,
        "command_name": "On",
        "payload": {},
    }
    assert any(
        request.args == {"node_id": 1001, "attribute_path": "1/6/0"}
        for request in read_requests
    )
