from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.connector_recipes import MatterRecipeBinding, MatterRecipeEndpoint


def test_matter_recipe_rejects_vendor_attributes_and_duplicate_capabilities() -> None:
    with pytest.raises(ValidationError):
        MatterRecipeBinding(name="power", cluster_id=6, attribute_id=1)
    with pytest.raises(ValidationError):
        MatterRecipeEndpoint(
            endpoint_id=1,
            capabilities=(
                MatterRecipeBinding(name="power", cluster_id=6, attribute_id=0),
                MatterRecipeBinding(name="power", cluster_id=6, attribute_id=0),
            ),
        )

