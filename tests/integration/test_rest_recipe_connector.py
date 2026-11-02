from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import Command, SourceRef


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_fixture",
            "display_name": "REST Fixture",
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


def _document() -> dict[str, object]:
    schema = {"type": "object"}
    response = {
        "description": "JSON",
        "content": {"application/json": {"schema": schema}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1.0"},
        "paths": {
            "/devices/{device_id}": {"get": {"responses": {"200": response}}},
            "/devices/{device_id}/power": {"put": {"responses": {"200": response}}},
        },
    }


class _StreamingResponse:
    is_redirect = False
    status_code = 200

    def __init__(self, chunks: list[bytes], *, content_length: str | None = None) -> None:
        self.chunks = chunks
        self.headers = (
            {"content-length": content_length} if content_length is not None else {}
        )
        self.iterations = 0
        self.chunks_yielded = 0

    async def aiter_bytes(self, *, chunk_size: int | None = None):
        del chunk_size
        self.iterations += 1
        for chunk in self.chunks:
            self.chunks_yielded += 1
            yield chunk


class _StreamingClient:
    def __init__(self, response: _StreamingResponse) -> None:
        self.response = response
        self.stream_calls = 0
        self.request_calls = 0

    async def request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.request_calls += 1
        raise AssertionError("REST adapter must use streaming")

    @asynccontextmanager
    async def stream(self, *args: object, **kwargs: object):
        del args, kwargs
        self.stream_calls += 1
        yield self.response


@pytest.mark.asyncio
async def test_rest_recipe_reads_executes_and_proves_readback() -> None:
    state = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state
        if request.method == "PUT":
            state = json.loads(request.content)
            return httpx.Response(200, json={"power": state})
        return httpx.Response(200, json={"power": state})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    profile = OpenApiRecipeProfile.from_document(_document(), endpoint="https://api.example.test")
    candidate = ConnectionCandidate(
        connector_id="rest_fixture",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )
    adapter = RestRecipeAdapter(client, _recipe(), candidate, profile, adapter_id="rest-lamp-1")

    await adapter.connect()
    snapshot = await adapter.discover()
    assert snapshot.source_entities[0]["entity_id"] == "lamp-1"

    states = await adapter.read_state([SourceRef(adapter_id="rest-lamp-1", external_id="lamp-1")])
    assert states[0].value is True

    ack = await adapter.execute(
        Command(
            id="command-1",
            device_id="lamp-1",
            command="set_power",
            value=False,
            idempotency_key="rest-command-1",
        )
    )
    assert ack.accepted is True
    assert state is False
    await client.aclose()


@pytest.mark.asyncio
async def test_rest_recipe_rejects_redirects_and_oversized_responses() -> None:
    candidate = ConnectionCandidate(
        connector_id="rest_fixture",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )
    source_ref = SourceRef(adapter_id="rest-lamp-1", external_id="lamp-1")

    redirect_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "/elsewhere"})
        ),
        follow_redirects=False,
    )
    redirect_adapter = RestRecipeAdapter(
        redirect_client,
        _recipe(),
        candidate,
        OpenApiRecipeProfile.from_document(_document(), endpoint="https://api.example.test"),
        adapter_id="rest-lamp-1",
    )
    await redirect_adapter.connect()
    with pytest.raises(ConnectionError, match="redirects"):
        await redirect_adapter.read_state([source_ref])
    await redirect_client.aclose()

    large_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"power": True})),
        follow_redirects=False,
    )
    large_adapter = RestRecipeAdapter(
        large_client,
        _recipe(),
        candidate,
        OpenApiRecipeProfile.from_document(
            _document(), endpoint="https://api.example.test", max_response_bytes=4
        ),
        adapter_id="rest-lamp-1",
    )
    await large_adapter.connect()
    with pytest.raises(ValueError, match="too large"):
        await large_adapter.read_state([source_ref])
    await large_client.aclose()


@pytest.mark.asyncio
async def test_rest_recipe_reads_bounded_body_through_streaming_client() -> None:
    stream_client = _StreamingClient(_StreamingResponse([b'{"power":', b" true}"]))
    candidate = ConnectionCandidate(
        connector_id="rest_fixture",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )
    adapter = RestRecipeAdapter(
        stream_client,  # type: ignore[arg-type]
        _recipe(),
        candidate,
        OpenApiRecipeProfile.from_document(_document(), endpoint="https://api.example.test"),
        adapter_id="rest-lamp-1",
    )
    await adapter.connect()

    states = await adapter.read_state([SourceRef(adapter_id="rest-lamp-1", external_id="lamp-1")])

    assert states[0].value is True
    assert stream_client.stream_calls == 1
    assert stream_client.request_calls == 0


@pytest.mark.asyncio
async def test_rest_recipe_rejects_declared_oversized_body_before_streaming() -> None:
    response = _StreamingResponse([b'{"power": true}'], content_length="100")
    stream_client = _StreamingClient(response)
    candidate = ConnectionCandidate(
        connector_id="rest_fixture",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )
    adapter = RestRecipeAdapter(
        stream_client,  # type: ignore[arg-type]
        _recipe(),
        candidate,
        OpenApiRecipeProfile.from_document(
            _document(), endpoint="https://api.example.test", max_response_bytes=10
        ),
        adapter_id="rest-lamp-1",
    )
    await adapter.connect()

    with pytest.raises(ValueError, match="too large"):
        await adapter.read_state([SourceRef(adapter_id="rest-lamp-1", external_id="lamp-1")])

    assert response.iterations == 0


@pytest.mark.asyncio
async def test_rest_recipe_stops_stream_on_first_over_limit_chunk() -> None:
    response = _StreamingResponse([b'{"power":', b" true}", b" ignored"], content_length=None)
    stream_client = _StreamingClient(response)
    candidate = ConnectionCandidate(
        connector_id="rest_fixture",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )
    adapter = RestRecipeAdapter(
        stream_client,  # type: ignore[arg-type]
        _recipe(),
        candidate,
        OpenApiRecipeProfile.from_document(
            _document(), endpoint="https://api.example.test", max_response_bytes=10
        ),
        adapter_id="rest-lamp-1",
    )
    await adapter.connect()

    with pytest.raises(ValueError, match="too large"):
        await adapter.read_state([SourceRef(adapter_id="rest-lamp-1", external_id="lamp-1")])

    assert response.chunks_yielded == 2
