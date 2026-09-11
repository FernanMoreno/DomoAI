"""Live provider evidence preserves existing MCP authorization and visibility."""

import json

import pytest
from tests.composition.test_scoped_area_inventory_composition import request_authority
from tests.contract.test_effective_route_coverage import (
    build_route_context,
    read_resource,
    route_snapshot,
)
from tests.contract.test_scoped_area_inventory_contract import scoped_authority

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AdapterSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["runtime", "coverage"])
@pytest.mark.parametrize("denial", ["household", "tenant", "operation"])
async def test_denied_resource_never_samples_live_provider_ids(resource: str, denial: str) -> None:
    _, context, server = build_route_context((("a", True),), ("a",))
    calls = 0

    def supplier() -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ("a",)

    context.active_provider_ids_supplier = supplier
    authority = scoped_authority()
    if denial == "household":
        authority = scoped_authority(household="foreign-home")
    elif denial == "tenant":
        authority = scoped_authority(tenant="foreign-tenant")
    else:
        authority = authority.model_copy(update={"operations": ["get_state"]})

    with request_authority(authority):
        result = await read_resource(server, resource)

    assert result["error"]["code"] == "insufficient_scope"
    assert calls == 0
    assert not {"routes", "providers", "writable_capabilities"}.intersection(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["runtime", "coverage"])
@pytest.mark.parametrize("scope", ["device", "area", "empty", "intersection"])
async def test_live_evidence_never_includes_hidden_devices(resource: str, scope: str) -> None:
    _, context, server = build_route_context((("a", True),), ("stale-fallback",))
    visible = route_snapshot()
    hidden = route_snapshot(device_id="hidden.light", area_id="hidden.room")
    context.registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=visible.source_entities + hidden.source_entities,
            areas=visible.areas + hidden.areas,
        ),
        "a",
    )
    context.active_provider_ids_supplier = lambda: ("a",)
    authority = {
        "device": scoped_authority(devices=("home.light",)),
        "area": scoped_authority(areas=("home.room",)),
        "empty": scoped_authority(devices=("missing-device",)),
        "intersection": scoped_authority(devices=("home.light",), areas=("hidden.room",)),
    }[scope]

    with request_authority(authority):
        result = await read_resource(server, resource)

    entries = result["routes"] if resource == "coverage" else result["writable_capabilities"]
    assert {item["device_id"] for item in entries} == (
        {"home.light"} if scope in {"device", "area"} else set()
    )
    assert "hidden" not in json.dumps(result)
    assert "stale-fallback" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["runtime", "coverage"])
async def test_provider_sample_failure_never_reuses_positive_fallback(resource: str) -> None:
    _, context, server = build_route_context((("a", True),), ("a",))

    def failed_sample() -> tuple[str, ...]:
        raise DomainError(ErrorCode.ADAPTER_UNAVAILABLE, "Provider evidence unavailable")

    context.active_provider_ids_supplier = failed_sample
    result = await read_resource(server, resource)

    assert result["error"]["code"] == "source_unavailable"
    assert not {"routes", "providers", "writable_capabilities"}.intersection(result)
