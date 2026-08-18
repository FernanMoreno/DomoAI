from collections.abc import AsyncIterator

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import AdapterHealth, AdapterSnapshot, SourceEvent, StateStatus
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.matter_server import event_message, node_snapshot, server_info


class _StopTest(Exception):
    """Sentinel used to deterministically end a `run()` loop under test."""


class DiscoveryCountingAdapter(SimulatedHomeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.discover_calls = 0

    async def discover(self) -> AdapterSnapshot:
        self.discover_calls += 1
        return await super().discover()


@pytest.mark.asyncio
async def test_source_event_refreshes_canonical_state_and_revision() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    adapter.set_available("cover.bedroom_blind", False)
    event = await consumer.consume_once()

    cover_id = registry.canonical_id_for_source("fixture", "cover.bedroom_blind")
    assert event is not None
    assert event.kind == "availability_changed"
    assert cover_id is not None
    state = await state_store.get(cover_id, "position")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE
    assert state_store.runtime_revision == "rev-2"
    assert audit.events[-1].event_type == "source_event_applied"


class DisconnectedAdapter(SimulatedHomeAdapter):
    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise ConnectionError("Home Assistant event stream disconnected")
        yield SourceEvent(kind="unreachable")


@pytest.mark.asyncio
async def test_disconnected_source_marks_cached_state_stale_and_audits() -> None:
    adapter = DisconnectedAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    event = await consumer.consume_once()

    assert event is None
    snapshots = await state_store.all()
    assert snapshots
    assert all(snapshot.status is StateStatus.STALE for snapshot in snapshots)
    assert audit.events[-1].event_type == "source_event_stream_unavailable"


@pytest.mark.asyncio
async def test_matter_event_refreshes_canonical_runtime_state() -> None:
    nodes = [node_snapshot(1001)]
    transport = InMemoryMatterTransport(nodes=nodes, server_info=server_info())
    adapter = MatterServerAdapter(transport)
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

    device_id = registry.canonical_id_for_source("matter", "node:1001/endpoint:1")
    assert event is not None
    assert event.kind == "availability_changed"
    assert device_id is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_burst_of_state_only_events_does_not_repeat_full_discovery() -> None:
    adapter = DiscoveryCountingAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    assert adapter.discover_calls == 1
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    light_id = next(
        device.id for device in registry.devices if device.type.value == "light"
    )
    for index in range(1000):
        adapter._find_for_device(light_id)["state"]["brightness"] = index % 100
        await consumer._apply_event(SourceEvent(kind="state_changed"))

    assert adapter.discover_calls == 1
    state = await state_store.get(light_id, "brightness")
    assert state is not None
    assert state.value == 999 % 100


@pytest.mark.asyncio
async def test_unrecognized_event_kind_falls_back_to_full_discovery() -> None:
    adapter = DiscoveryCountingAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    assert adapter.discover_calls == 1
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    await consumer._apply_event(SourceEvent(kind="some_future_event_kind"))

    assert adapter.discover_calls == 2


@pytest.mark.asyncio
async def test_repeated_identical_state_only_value_does_not_advance_version() -> None:
    adapter = DiscoveryCountingAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    light_id = next(
        device.id for device in registry.devices if device.type.value == "light"
    )
    await consumer._apply_event(SourceEvent(kind="state_changed"))
    version_after_first = state_store.state_version(light_id, "brightness")

    await consumer._apply_event(SourceEvent(kind="state_changed"))

    assert state_store.state_version(light_id, "brightness") == version_after_first


class _ReconnectOnceAdapter(SimulatedHomeAdapter):
    """Starts disconnected; one successful `connect()` then subscribe ends run()."""

    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0
        self.refresh_happened = False
        self._connected_flag = False

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected_flag = True

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self._connected_flag)

    async def discover(self) -> AdapterSnapshot:
        self.refresh_happened = True
        return await super().discover()

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise _StopTest
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_run_reconnects_and_refreshes_before_consuming_events() -> None:
    adapter = _ReconnectOnceAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=0.001)

    assert adapter.connect_calls == 1
    assert adapter.refresh_happened is True


class _AlreadyConnectedAdapter(SimulatedHomeAdapter):
    """Reports connected from the start; proves `run()` never calls `connect()`."""

    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=True)

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise _StopTest
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_run_skips_connect_when_already_healthy() -> None:
    adapter = _AlreadyConnectedAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=0.001)

    assert adapter.connect_calls == 0


class _AlwaysFailsToConnectAdapter(SimulatedHomeAdapter):
    def __init__(self, *, fail_until_attempt: int) -> None:
        super().__init__()
        self.connect_attempts = 0
        self._fail_until_attempt = fail_until_attempt

    async def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_attempts > self._fail_until_attempt:
            raise _StopTest
        raise ConnectionError("still down")

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=False)

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise _StopTest
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_backoff_grows_on_repeated_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    monkeypatch.setattr("domoai.runtime.event_consumer.asyncio.sleep", fake_sleep)
    adapter = _AlwaysFailsToConnectAdapter(fail_until_attempt=5)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=1.0, max_reconnect_delay=8.0)

    assert recorded_delays == [1.0, 2.0, 4.0, 8.0, 8.0]


class _RecoversThenDropsAdapter(SimulatedHomeAdapter):
    def __init__(self, *, fail_connects: int) -> None:
        super().__init__()
        self.connect_attempts = 0
        self._subscribe_calls = 0
        self._fail_connects = fail_connects
        self._connected_flag = False

    async def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_attempts <= self._fail_connects:
            raise ConnectionError("still down")
        self._connected_flag = True

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self._connected_flag)

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._subscribe_calls += 1
        if self._subscribe_calls == 1:
            raise ConnectionError("stream dropped")
        raise _StopTest
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_backoff_resets_to_initial_delay_after_successful_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_delays.append(delay)

    monkeypatch.setattr("domoai.runtime.event_consumer.asyncio.sleep", fake_sleep)
    adapter = _RecoversThenDropsAdapter(fail_connects=2)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=1.0, max_reconnect_delay=8.0)

    # Two failed connect attempts grow the delay (1.0, 2.0); the third attempt
    # succeeds and resets it, so the post-reconnect stream drop's delay is
    # back at the initial value instead of continuing to grow.
    assert recorded_delays == [1.0, 2.0, 1.0]


class _PartiallyDegradedCompositeAdapter(SimulatedHomeAdapter):
    """Reports connected=True overall but one component down (Spec 026's gap)."""

    def __init__(self, *, component_down: bool) -> None:
        super().__init__()
        self.connect_calls = 0
        self._component_down = component_down

    async def connect(self) -> None:
        self.connect_calls += 1
        self._component_down = False
        raise _StopTest

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=True,
            components=[
                AdapterHealth(adapter_id="home_assistant", connected=True),
                AdapterHealth(
                    adapter_id="modbus", connected=not self._component_down
                ),
            ],
        )

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise _StopTest
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_run_reconnects_when_one_composite_component_is_down() -> None:
    adapter = _PartiallyDegradedCompositeAdapter(component_down=True)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=0.001)

    assert adapter.connect_calls == 1


@pytest.mark.asyncio
async def test_run_does_not_reconnect_when_every_composite_component_is_healthy() -> None:
    adapter = _PartiallyDegradedCompositeAdapter(component_down=False)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    with pytest.raises(_StopTest):
        await consumer.run(reconnect_delay=0.001)

    assert adapter.connect_calls == 0
