from __future__ import annotations

import json

import httpx
import pytest

import domoai.adapters.rest.recipe_adapter as recipe_adapter_module
from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import RestRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import Command


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_idempotency",
            "display_name": "REST idempotency",
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


def _profile() -> OpenApiRecipeProfile:
    response = {
        "description": "JSON",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    return OpenApiRecipeProfile.from_document(
        {
            "openapi": "3.1.0",
            "info": {"title": "Fixture", "version": "1.0"},
            "paths": {
                "/devices/{device_id}": {"get": {"responses": {"200": response}}},
                "/devices/{device_id}/power": {
                    "put": {"responses": {"200": response}}
                },
            },
        },
        endpoint="https://api.example.test",
    )


def _candidate() -> ConnectionCandidate:
    return ConnectionCandidate(
        connector_id="rest_idempotency",
        candidate_id="lamp-1",
        display_name="Fixture lamp",
        endpoint={"url": "https://api.example.test"},
    )


def _command(key: str, value: bool = False) -> Command:
    return Command(
        id=f"command-{key}",
        device_id="lamp-1",
        command="set_power",
        value=value,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_rest_write_forwards_key_and_provider_deduplicates_after_restart() -> None:
    state = True
    seen_keys: set[str] = set()
    write_count = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state, write_count
        requests.append(request)
        if request.method == "PUT":
            key = request.headers["Idempotency-Key"]
            if key not in seen_keys:
                seen_keys.add(key)
                write_count += 1
                state = json.loads(request.content)
        return httpx.Response(200, json={"power": state})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    adapter_one = RestRecipeAdapter(
        client, _recipe(), _candidate(), _profile(), adapter_id="rest-one"
    )
    adapter_two = RestRecipeAdapter(
        client, _recipe(), _candidate(), _profile(), adapter_id="rest-two"
    )
    await adapter_one.connect()
    await adapter_two.connect()

    first = await adapter_one.execute(_command("restart-safe-key"))
    second = await adapter_two.execute(_command("restart-safe-key"))

    assert first.accepted is True
    assert second.accepted is True
    assert write_count == 1
    write_keys = [
        request.headers.get("Idempotency-Key")
        for request in requests
        if request.method == "PUT"
    ]
    assert write_keys == ["restart-safe-key", "restart-safe-key"]
    assert all(
        "Idempotency-Key" not in request.headers
        for request in requests
        if request.method == "GET"
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_rest_local_idempotency_cache_is_bounded_and_lru() -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "PUT":
            writes += 1
            value = json.loads(request.content)
        else:
            value = False
        return httpx.Response(200, json={"power": value})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    adapter = RestRecipeAdapter(
        client,
        _recipe(),
        _candidate(),
        _profile(),
        adapter_id="rest-cache",
        idempotency_cache_max_entries=2,
    )
    await adapter.connect()

    await adapter.execute(_command("cache-one", False))
    await adapter.execute(_command("cache-two", False))
    await adapter.execute(_command("cache-three", False))
    await adapter.execute(_command("cache-two", False))

    assert writes == 3
    assert len(adapter._executed_idempotency_keys) == 2
    assert "cache-one" not in adapter._executed_idempotency_keys
    assert list(adapter._executed_idempotency_keys) == ["cache-three", "cache-two"]
    await client.aclose()


@pytest.mark.asyncio
async def test_rest_local_idempotency_cache_expires_and_does_not_cache_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    writes = 0

    def fake_monotonic() -> float:
        return now

    monkeypatch.setattr(recipe_adapter_module, "monotonic", fake_monotonic)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "PUT":
            writes += 1
            return httpx.Response(200, json={"power": True})
        return httpx.Response(200, json={"power": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    adapter = RestRecipeAdapter(
        client,
        _recipe(),
        _candidate(),
        _profile(),
        adapter_id="rest-ttl",
        idempotency_cache_ttl_seconds=5,
    )
    await adapter.connect()

    first = await adapter.execute(_command("expired-key", False))
    now += 6
    second = await adapter.execute(_command("expired-key", False))

    assert first.accepted is False
    assert second.accepted is False
    assert writes == 2
    assert "expired-key" not in adapter._executed_idempotency_keys
    await client.aclose()


def test_rest_idempotency_cache_policy_must_be_positive() -> None:
    with pytest.raises(ValueError, match="idempotency cache"):
        RestRecipeAdapter(
            object(),  # type: ignore[arg-type]
            _recipe(),
            _candidate(),
            _profile(),
            adapter_id="invalid",
            idempotency_cache_max_entries=0,
        )
    with pytest.raises(ValueError, match="idempotency cache"):
        RestRecipeAdapter(
            object(),  # type: ignore[arg-type]
            _recipe(),
            _candidate(),
            _profile(),
            adapter_id="invalid",
            idempotency_cache_ttl_seconds=0,
        )
