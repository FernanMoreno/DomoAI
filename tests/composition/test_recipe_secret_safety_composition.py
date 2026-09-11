from __future__ import annotations

import pytest

from domoai.domain.connector_recipes import ConnectorRecipe


@pytest.mark.composition
def test_public_recipe_validation_blocks_nested_credentials_before_consumers() -> None:
    secret = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"

    with pytest.raises(ValueError, match="secret material") as error:
        ConnectorRecipe.model_validate(
            {
                "connector_id": "composition_recipe",
                "display_name": "Composition recipe",
                "transport_family": "mqtt",
                "discovery_options": {
                    "headers": '{"Authorization": "' + secret + '"}'
                },
                "capabilities": [
                    {
                        "capability_id": "power",
                        "kind": "boolean",
                        "state_topic": "safe/{device_id}/state",
                    }
                ],
            }
        )

    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in str(error.value)
