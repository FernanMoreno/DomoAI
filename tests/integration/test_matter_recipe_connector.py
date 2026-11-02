from __future__ import annotations

from domoai.adapters.matter.recipe_discovery import MatterRecipeDiscovery
from domoai.domain.connector_recipes import MatterRecipeProfile


def test_matter_recipe_discovery_requires_commissioned_opaque_node_reference() -> None:
    profile = MatterRecipeProfile.model_validate(
        {
            "endpoints": [
                {
                    "endpoint_id": 1,
                    "capabilities": [
                        {
                            "name": "power",
                            "cluster_id": 6,
                            "attribute_id": 0,
                            "command_names": [],
                        }
                    ],
                }
            ],
            "node_ids": [42],
        }
    )
    candidates = MatterRecipeDiscovery().manual_candidates(
        connector_id="fixture-matter-recipe",
        endpoint="ws://matter-server.local/ws",
        profile=profile,
    )
    assert len(candidates) == 1
    assert candidates[0].metadata["commissioning_required"] is True
    assert "fabric" not in candidates[0].metadata
