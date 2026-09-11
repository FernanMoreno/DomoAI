"""Coverage evidence observes the existing household and device ACL boundary."""

import json

import pytest
from tests.composition.test_scoped_area_inventory_composition import request_authority
from tests.contract.test_effective_route_coverage import build_route_context, read_resource
from tests.contract.test_scoped_area_inventory_contract import scoped_authority
from tests.fixtures.device_coverage import RESOURCE, family_snapshot

from domoai.domain.errors import DomainError, ErrorCode

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("denial", ["household", "tenant", "operation"])
async def test_denial_precedes_membership_sampling(denial: str) -> None:
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
        result = await read_resource(server, RESOURCE)
    assert result["error"]["code"] == "insufficient_scope"
    assert calls == 0
    assert not {"rows", "families"}.intersection(result)


@pytest.mark.parametrize("scope", ["device", "area", "empty", "intersection"])
async def test_families_and_routes_only_reference_visible_devices(scope: str) -> None:
    _, context, server = build_route_context((), ("a", "unrelated-provider"))
    snapshot = family_snapshot()
    for entity in snapshot.source_entities:
        if entity["canonical_id"] != "home.light":
            entity["area_id"] = "hidden.room"
    context.registry.apply_snapshot(snapshot, "a")
    authority = {
        "device": scoped_authority(devices=("home.light",)),
        "area": scoped_authority(areas=("home.room",)),
        "empty": scoped_authority(devices=("absent",)),
        "intersection": scoped_authority(devices=("home.light",), areas=("hidden.room",)),
    }[scope]
    with request_authority(authority):
        result = await read_resource(server, RESOURCE)
    visible = {"home.light"} if scope in {"device", "area"} else set()
    assert {row["device_id"] for row in result["rows"]} == visible
    for group in result["families"]:
        assert set(group["candidate_device_ids"]) <= visible
        assert {ref["device_id"] for ref in group["row_refs"]} <= visible
    assert "home.switch" not in json.dumps(result)
    assert "unrelated-provider" not in json.dumps(result)


async def test_supplier_failure_cannot_fall_back_to_positive_membership() -> None:
    _, context, server = build_route_context((("a", True),), ("a",))

    def fail() -> tuple[str, ...]:
        raise DomainError(ErrorCode.ADAPTER_UNAVAILABLE, "Provider evidence unavailable")

    context.active_provider_ids_supplier = fail
    result = await read_resource(server, RESOURCE)
    assert result["error"]["code"] == "source_unavailable"
    assert "rows" not in result
