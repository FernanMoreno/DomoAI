from __future__ import annotations

from datetime import UTC, datetime

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.domain.models import StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.contract.test_home_assistant_partial_states import entity, event
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient


async def test_partial_ha_events_update_one_household_without_reread_or_global_degradation() -> (
    None
):
    clock = FixedClock(datetime(2026, 9, 10, 12, tzinfo=UTC))
    households = []
    for household in ("a", "b"):
        client = FakeHomeAssistantProviderClient(
            [
                entity(brightness=True) if household == "a" else entity(),
                {"entity_id": "switch.neighbor", "state": "on", "attributes": {}},
            ],
            events=[
                event(entity(brightness="token=never-publish")),
                event({"state": "on", "attributes": []}),
                event(entity("unavailable")),
                event(entity("on", brightness=255)),
            ],
        )
        adapter = HomeAssistantProviderAdapter(
            HomeAssistantProvider(client, clock=clock), adapter_id=f"ha:{household}", clock=clock
        )
        registry, store, audit = DeviceRegistry(), StateStore(clock=clock), AuditLog()
        discovery = DiscoveryService(adapter, registry, store, audit, clock=clock)
        initial = await discovery.refresh()
        brightness = next(s for s in initial.states if s.capability == "brightness")
        expected = StateStatus.INVALID if household == "a" else StateStatus.CURRENT
        assert brightness.status is expected
        assert brightness.value == (None if household == "a" else 50)
        consumer = RuntimeEventConsumer(adapter, discovery, store, audit, clock=clock)
        households.append((client, adapter, registry, store, consumer, initial))
    client_a, adapter_a, registry_a, store_a, consumer_a, initial_a = households[0]
    client_b, _, registry_b, store_b, _, initial_b = households[1]
    before_b = await store_b.all()
    calls_a, calls_b = client_a.fetch_states_calls, client_b.fetch_states_calls
    light_a = registry_a.canonical_id_for_source("ha:a", "light.shared")
    neighbor_a = registry_a.canonical_id_for_source("ha:a", "switch.neighbor")
    assert light_a and neighbor_a
    assert registry_b.canonical_id_for_source("ha:a", "light.shared") is None
    events = adapter_a.subscribe_events()
    first = await anext(events)
    await consumer_a._apply_event(first)
    power = await store_a.get(light_a, "power")
    brightness = await store_a.get(light_a, "brightness")
    assert power and power.value is True and power.status is StateStatus.CURRENT
    assert brightness and brightness.value is None and brightness.status is StateStatus.INVALID
    async for source_event in events:
        assert "token=" not in source_event.model_dump_json()
        assert getattr(source_event, "code", None) != "source_unavailable"
        await consumer_a._apply_event(source_event)
        neighbor = await store_a.get(neighbor_a, "power")
        assert neighbor and neighbor.value is True and neighbor.status is StateStatus.CURRENT
        assert await store_b.all() == before_b
    recovered = await store_a.get(light_a, "brightness")
    assert recovered and recovered.value == 100 and recovered.status is StateStatus.CURRENT
    assert client_a.fetch_states_calls == calls_a
    assert client_b.fetch_states_calls == calls_b
    assert len(initial_a.devices) == len(initial_b.devices) == 2
