"""Ports for bounded declarative connector engines."""

from __future__ import annotations

from typing import Protocol

from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import ScalarValue
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.ports import AdapterPort


class DiscoveryStrategyPort(Protocol):
    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]: ...


class TransportRecipePort(Protocol):
    def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> AdapterPort: ...


type RecipeEndpoint = dict[str, ScalarValue]


__all__ = ["DiscoveryStrategyPort", "RecipeEndpoint", "TransportRecipePort"]
