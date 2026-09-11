from __future__ import annotations

import httpx
import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import SourceRef


@pytest.mark.composition
@pytest.mark.asyncio
async def test_rest_streaming_preserves_adapter_and_client_ownership() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"power": True}))
    )
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_composition",
            "display_name": "REST composition",
            "transport_family": "rest",
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "read_path": "/devices/{device_id}",
                    "read_json_path": "$.power",
                }
            ],
        }
    )
    profile = OpenApiRecipeProfile.from_document(
        {
            "openapi": "3.1.0",
            "paths": {
                "/devices/{device_id}": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"type": "object"}}
                                }
                            }
                        }
                    }
                }
            },
        },
        endpoint="https://api.example.test",
    )
    adapter = RestRecipeAdapter(
        client,
        recipe,
        ConnectionCandidate(
            connector_id="rest_composition",
            candidate_id="lamp-1",
            display_name="Lamp",
            endpoint={"url": "https://api.example.test"},
        ),
        profile,
        adapter_id="rest-composition",
    )
    await adapter.connect()

    states = await adapter.read_state(
        [SourceRef(adapter_id="rest-composition", external_id="lamp-1")]
    )

    assert states[0].value is True
    assert client.is_closed is False
    await client.aclose()
