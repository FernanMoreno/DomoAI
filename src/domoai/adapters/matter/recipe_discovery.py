"""Bounded candidate handling for commissioned Matter recipe connections."""

from __future__ import annotations

from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe, MatterRecipeProfile
from domoai.runtime.connection_ports import ConnectionSetup


class MatterRecipeDiscovery:
    """Accept only an operator-supplied Matter Server URL."""

    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        profile = recipe.protocol_profile
        if not isinstance(profile, MatterRecipeProfile):
            return []
        endpoint = setup.draft.endpoint.get("url")
        if not isinstance(endpoint, str):
            return []
        return self.manual_candidates(
            connector_id=recipe.connector_id, endpoint=endpoint, profile=profile
        )

    def manual_candidates(
        self, *, connector_id: str, endpoint: str, profile: MatterRecipeProfile
    ) -> list[ConnectionCandidate]:
        if not endpoint.startswith(("ws://", "wss://")):
            return []
        try:
            candidate = ConnectionCandidate(
                connector_id=connector_id,
                candidate_id=f"{connector_id}:{endpoint}",
                display_name="Commissioned Matter Server",
                endpoint={"url": endpoint},
                metadata={
                    "source": "manual_recipe_discovery",
                    "transport": "matter",
                    "commissioning_required": profile.commissioning_required,
                },
            )
        except ValueError:
            return []
        return [candidate]


__all__ = ["MatterRecipeDiscovery"]
