from __future__ import annotations

from domoai.adapters.knx.recipe_adapter import KnxRecipeTransport
from domoai.adapters.knx.recipe_discovery import KnxRecipeDiscovery
from domoai.adapters.knx.transport import InMemoryKnxTransport


def test_knx_recipe_discovery_is_bounded_to_explicit_endpoint() -> None:
    discovery = KnxRecipeDiscovery()
    candidates = discovery.manual_candidates(
        connector_id="fixture-knx-recipe", host="gateway.local", port=3671
    )
    assert len(candidates) == 1
    assert candidates[0].endpoint == {"host": "gateway.local", "port": 3671}
    assert isinstance(
        KnxRecipeTransport(lambda _host, _port, _secure: InMemoryKnxTransport()),
        KnxRecipeTransport,
    )
