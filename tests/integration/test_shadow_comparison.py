import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.domain.models import AdapterSnapshot
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.shadow import ShadowComparator
from domoai.runtime.state_store import StateStore


def _entity(
    entity_id: str,
    *,
    domain: str,
    name: str,
    area_id: str = "living_room",
) -> dict:
    return {
        "entity_id": entity_id,
        "domain": domain,
        "name": name,
        "area_id": area_id,
        "device_id": f"{entity_id}-device",
        "manufacturer": "Fixture",
        "model": "F1",
        "supported_features": [],
        "attributes": {},
        "state": {"power": False},
    }


@pytest.mark.asyncio
async def test_shadow_comparison_classifies_matches_and_only_one_side() -> None:
    shared_entity = _entity("switch.shared", domain="switch", name="Shared switch")
    production_only = _entity(
        "switch.production_only", domain="switch", name="Production only switch"
    )
    candidate_only = _entity("switch.candidate_only", domain="switch", name="Candidate only switch")
    production = SimulatedHomeAdapter(entities=[shared_entity, production_only])
    candidate = SimulatedHomeAdapter(entities=[shared_entity, candidate_only])

    result = await ShadowComparator().compare(production, candidate)

    classifications = {entity.entity_id: entity.classification for entity in result.entities}
    assert classifications["switch.shared"] == "matches"
    assert classifications["switch.production_only"] == "only_production"
    assert classifications["switch.candidate_only"] == "only_candidate"
    assert result.production_unavailable is False
    assert result.candidate_unavailable is False


@pytest.mark.asyncio
async def test_shadow_comparison_flags_semantic_type_disagreement() -> None:
    production_entity = _entity(
        "shared.entity", domain="light", name="Shared Device", area_id="living_room"
    )
    candidate_entity = _entity(
        "shared.entity", domain="switch", name="Shared Device", area_id="living_room"
    )
    production = SimulatedHomeAdapter(entities=[production_entity])
    candidate = SimulatedHomeAdapter(entities=[candidate_entity])

    result = await ShadowComparator().compare(production, candidate)

    assert len(result.entities) == 1
    assert result.entities[0].classification == "disagrees"


@pytest.mark.asyncio
async def test_shadow_comparison_issues_zero_commands() -> None:
    shared_entity = _entity("switch.shared", domain="switch", name="Shared switch")
    production = SimulatedHomeAdapter(entities=[shared_entity])
    candidate = SimulatedHomeAdapter(entities=[shared_entity])

    await ShadowComparator().compare(production, candidate)

    assert production.calls == []
    assert candidate.calls == []


@pytest.mark.asyncio
async def test_shadow_comparison_never_touches_live_registry_or_state() -> None:
    live_registry = DeviceRegistry()
    live_state_store = StateStore()
    live_snapshot_before = await live_state_store.all()
    live_devices_before = list(live_registry.devices)

    shared_entity = _entity("switch.shared", domain="switch", name="Shared switch")
    production = SimulatedHomeAdapter(entities=[shared_entity])
    candidate = SimulatedHomeAdapter(entities=[shared_entity])
    await ShadowComparator().compare(production, candidate)

    assert list(live_registry.devices) == live_devices_before
    assert await live_state_store.all() == live_snapshot_before


class _UnavailableAdapter:
    adapter_id = "unavailable"

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def discover(self) -> AdapterSnapshot:
        raise ConnectionError("simulated unavailable")

    async def execute(self, command):  # pragma: no cover - never called
        raise AssertionError("execute must never be called by shadow comparison")

    async def health(self):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.mark.asyncio
async def test_shadow_comparison_reports_production_unavailable_distinctly() -> None:
    shared_entity = _entity("switch.shared", domain="switch", name="Shared switch")
    candidate = SimulatedHomeAdapter(entities=[shared_entity])

    result = await ShadowComparator().compare(_UnavailableAdapter(), candidate)

    assert result.production_unavailable is True
    assert result.candidate_unavailable is False
    assert result.entities == []


@pytest.mark.asyncio
async def test_shadow_comparison_reports_candidate_unavailable_distinctly() -> None:
    shared_entity = _entity("switch.shared", domain="switch", name="Shared switch")
    production = SimulatedHomeAdapter(entities=[shared_entity])

    result = await ShadowComparator().compare(production, _UnavailableAdapter())

    assert result.candidate_unavailable is True
    assert result.production_unavailable is False
    assert result.entities == []
