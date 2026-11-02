from __future__ import annotations

import httpx
import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import SourceRef


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_stream_contract",
            "display_name": "REST stream contract",
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


def _document() -> dict[str, object]:
    return {
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
    }


@pytest.mark.asyncio
async def test_streaming_response_closes_response_but_not_caller_client() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"power": True}))
    )
    adapter = RestRecipeAdapter(
        client,
        _recipe(),
        ConnectionCandidate(
            connector_id="rest_stream_contract",
            candidate_id="lamp-1",
            display_name="Lamp",
            endpoint={"url": "https://api.example.test"},
        ),
        OpenApiRecipeProfile.from_document(
            _document(), endpoint="https://api.example.test", max_response_bytes=1024
        ),
        adapter_id="rest-stream-contract",
    )
    await adapter.connect()

    states = await adapter.read_state(
        [SourceRef(adapter_id="rest-stream-contract", external_id="lamp-1")]
    )

    assert states[0].value is True
    assert client.is_closed is False
    await client.aclose()
