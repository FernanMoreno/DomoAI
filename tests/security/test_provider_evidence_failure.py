"""Supplier faults are private across the real MCP ClientSession boundary."""

import asyncio
import json
from collections.abc import Iterator
from typing import cast

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextResourceContents
from tests.composition.test_scoped_area_inventory_composition import request_authority
from tests.contract.test_effective_route_coverage import build_route_context, read_resource
from tests.contract.test_scoped_area_inventory_contract import scoped_authority

from domoai.domain.errors import DomainError, ErrorCode

pytestmark = pytest.mark.asyncio
RESOURCES = ["runtime", "coverage", "coverage-evidence"]
PRIVATE_MARKER = "supplier-private-sentinel"
PRIVATE_PATH = "/private/provider-configuration.json"
SAFE_MESSAGE = "Provider availability evidence is unavailable"


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize("failure", ["runtime", "domain", "tuple"])
async def test_sdk_resource_never_discloses_supplier_failures(resource: str, failure: str) -> None:
    _, context, server = build_route_context((("a", True),), ("a",))
    calls = 0

    def broken_iteration() -> Iterator[str]:
        yield "a"
        raise RuntimeError(f"{PRIVATE_MARKER}: {PRIVATE_PATH}")

    def supplier() -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if failure == "tuple":
            return cast(tuple[str, ...], broken_iteration())
        if failure == "domain":
            raise DomainError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                PRIVATE_MARKER,
                field=PRIVATE_PATH,
                device_id="foreign-device",
                capability="private-capability",
                details={"private": PRIVATE_PATH},
            )
        raise RuntimeError(f"{PRIVATE_MARKER}: {PRIVATE_PATH}")

    context.active_provider_ids_supplier = supplier
    async with create_connected_server_and_client_session(server) as session:
        # Also prove that a failed response never becomes cached positive/empty
        # membership on a later request with the same inventory revision.
        for expected_calls in (1, 2):
            result = None
            try:
                result = await session.read_resource(f"domotics://{resource}")
                wire = result.model_dump_json()
            except McpError as error:
                wire = str(error)
            assert calls == expected_calls
            assert PRIVATE_MARKER not in wire
            assert PRIVATE_PATH not in wire
            assert "foreign-device" not in wire
            assert "private-capability" not in wire
            assert result is not None, "Supplier failure must return a safe domain envelope"
            assert len(result.contents) == 1
            content = result.contents[0]
            assert isinstance(content, TextResourceContents)
            payload = json.loads(content.text)
            assert payload["error"]["code"] == "source_unavailable"
            assert payload["error"]["message"] == SAFE_MESSAGE
            assert payload["error"]["details"] == {}
            assert not {
                "rows",
                "families",
                "routes",
                "providers",
                "writable_capabilities",
                "authority",
            }.intersection(payload)


@pytest.mark.parametrize("resource", RESOURCES)
async def test_supplier_cancellation_propagates_without_a_response(resource: str) -> None:
    _, context, server = build_route_context((("a", True),), ("a",))
    calls = 0

    def supplier() -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    context.active_provider_ids_supplier = supplier
    with pytest.raises(asyncio.CancelledError):
        await read_resource(server, resource)
    assert calls == 1


@pytest.mark.parametrize("resource", RESOURCES)
async def test_unauthorized_request_never_invokes_failing_supplier(resource: str) -> None:
    _, context, server = build_route_context((("a", True),), ("a",))
    calls = 0

    def supplier() -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        raise RuntimeError(PRIVATE_MARKER)

    context.active_provider_ids_supplier = supplier
    with request_authority(scoped_authority(household="foreign-home")):
        response = await read_resource(server, resource)
    assert response["error"]["code"] == "insufficient_scope"
    assert calls == 0
    assert PRIVATE_MARKER not in json.dumps(response)
