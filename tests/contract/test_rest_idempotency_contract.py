from __future__ import annotations

import httpx
import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter, RestRecipeTransport
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_ports import ConnectionSetup


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_contract",
            "display_name": "REST contract",
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


def _profile() -> OpenApiRecipeProfile:
    return OpenApiRecipeProfile.from_document(
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


@pytest.mark.asyncio
async def test_rest_transport_contract_propagates_bounded_cache_options() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"power": True}))
    )
    transport = RestRecipeTransport(
        client,
        _profile(),
        idempotency_cache_max_entries=7,
        idempotency_cache_ttl_seconds=11,
    )
    adapter = transport.build_adapter(
        _recipe(),
        setup=ConnectionSetup(
            candidate=ConnectionCandidate(
                connector_id="rest_contract",
                candidate_id="lamp-1",
                display_name="Lamp",
                endpoint={"url": "https://api.example.test"},
            ),
            draft=ConnectionDraft(
                connector_id="rest_contract",
                unique_id="lamp-1",
                endpoint={"url": "https://api.example.test"},
                options={},
            ),
        ),
        adapter_id="rest-contract",
    )

    assert isinstance(adapter, RestRecipeAdapter)
    assert adapter.idempotency_cache_max_entries == 7
    assert adapter.idempotency_cache_ttl_seconds == 11
    await client.aclose()
