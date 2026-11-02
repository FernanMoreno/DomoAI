from __future__ import annotations

import json

import httpx
import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import Command


@pytest.mark.composition
@pytest.mark.asyncio
async def test_two_rest_adapters_share_provider_deduplication_boundary() -> None:
    state = True
    writes = 0
    seen: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state, writes
        if request.method == "PUT":
            key = request.headers["Idempotency-Key"]
            if key not in seen:
                seen.add(key)
                writes += 1
                state = json.loads(request.content)
        return httpx.Response(200, json={"power": state})

    document = {
        "openapi": "3.1.0",
        "paths": {
            "/devices/{device_id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {"application/json": {"schema": {"type": "object"}}}
                        }
                    }
                }
            },
            "/devices/{device_id}/power": {
                "put": {
                    "responses": {
                        "200": {
                            "content": {"application/json": {"schema": {"type": "object"}}}
                        }
                    }
                }
            },
        },
    }
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_composition_idempotency",
            "display_name": "REST composition idempotency",
            "transport_family": "rest",
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "read_path": "/devices/{device_id}",
                    "read_json_path": "$.power",
                    "command": {
                        "name": "set_power",
                        "method": "PUT",
                        "path": "/devices/{device_id}/power",
                        "payload_template": "{value}",
                        "readback_path": "/devices/{device_id}",
                        "readback_json_path": "$.power",
                    },
                }
            ],
        }
    )
    candidate = ConnectionCandidate(
        connector_id=recipe.connector_id,
        candidate_id="lamp-1",
        display_name="Lamp",
        endpoint={"url": "https://api.example.test"},
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter_one = RestRecipeAdapter(
        client,
        recipe,
        candidate,
        OpenApiRecipeProfile.from_document(document, endpoint="https://api.example.test"),
        adapter_id="one",
    )
    adapter_two = RestRecipeAdapter(
        client,
        recipe,
        candidate,
        OpenApiRecipeProfile.from_document(document, endpoint="https://api.example.test"),
        adapter_id="two",
    )
    await adapter_one.connect()
    await adapter_two.connect()
    command = Command(
        id="composition-command",
        device_id="lamp-1",
        command="set_power",
        value=False,
        idempotency_key="composition-rest-key",
    )

    first = await adapter_one.execute(command)
    second = await adapter_two.execute(command)

    assert first.accepted is True
    assert second.accepted is True
    assert writes == 1
    await client.aclose()
