"""Bounded manual discovery for declarative Modbus recipes."""

from __future__ import annotations

from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_ports import ConnectionSetup


class ModbusRecipeDiscovery:
    """Turn one operator-supplied endpoint into one safe candidate."""

    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        endpoint = dict(setup.draft.endpoint)
        host = endpoint.get("host")
        port = endpoint.get("port", 502)
        if not isinstance(host, str) or not host.strip() or not isinstance(port, int):
            return []
        return self.manual_candidates(recipe.connector_id, host.strip(), port)

    @staticmethod
    def manual_candidates(
        connector_id: str, host: str, port: int = 502
    ) -> list[ConnectionCandidate]:
        if (
            not host.strip()
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            return []
        return [
            ConnectionCandidate(
                connector_id=connector_id,
                candidate_id=f"{connector_id}:{host}:{port}",
                display_name=f"Modbus TCP at {host}:{port}",
                endpoint={"host": host, "port": port},
                metadata={"source": "manual_recipe_discovery", "transport": "modbus"},
            )
        ]


__all__ = ["ModbusRecipeDiscovery"]
