"""Public evidence distinguishes runtime availability from hardware proof."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.domain.coverage import CoverageEvidenceSnapshot
from domoai.domain.models import AdapterSnapshot, AvailabilityMode
from domoai.mcp.resources import coverage_evidence_snapshot
from tests.contract.test_effective_route_coverage import (
    build_route_context,
    read_resource,
    route_snapshot,
)
from tests.fixtures.device_coverage import RESOURCE, assert_unknown, family_snapshot

pytestmark = pytest.mark.asyncio


async def test_evidence_is_a_separate_public_resource() -> None:
    _, _, server = build_route_context((("a", True),), ("a",))
    uris = {str(resource.uri) for resource in await server.list_resources()}
    assert f"domotics://{RESOURCE}" in uris
    assert "domotics://coverage" in uris


@pytest.mark.parametrize(
    ("routes", "providers", "active"),
    [
        ((), ("a",), False),
        ((("a", False),), ("a",), False),
        ((("a", True),), (), False),
        ((("a", True),), ("a",), True),
        ((("a", True), ("b", False)), ("b",), False),
        ((("a", False), ("b", True)), ("b",), True),
        ((("a", True), ("b", True)), ("a", "b"), True),
    ],
)
async def test_public_route_availability_and_unknown_evidence(
    routes: tuple[tuple[str, bool], ...],
    providers: tuple[str, ...],
    active: bool,
) -> None:
    _, context, server = build_route_context(routes, providers)
    result = await read_resource(server, RESOURCE)
    assert result["schema_version"] == "v1"
    assert result["runtime_revision"] == context.discovery.state_store.runtime_revision
    assert len(result["rows"]) == 2
    assert [row["capability"] for row in result["rows"]] == ["brightness", "temperature"]
    for row in result["rows"]:
        assert_unknown(row)
        assert row["status"] == ("active" if active else "unavailable")
        assert row["reasons"] == ([] if routes else ["route_missing"])
        assert len(row["routes"]) == len(routes)
        for route in row["routes"]:
            source_up = dict(routes)[route["provider_id"]]
            provider_up = route["provider_id"] in providers
            assert route["source_available"] is source_up
            assert route["provider_active"] is provider_up
            assert route["effective_available"] is (source_up and provider_up)
            assert route["availability_reasons"] == (
                ([] if source_up else ["source_unavailable"])
                + ([] if provider_up else ["provider_inactive"])
            )
    assert result["rows"][0]["declared_guarantees"]["readback_required"] is True
    assert result["rows"][1]["declared_operations"] == {
        "read": True,
        "write": False,
        "commands": [],
    }


async def test_membership_is_sampled_once_and_is_not_cached_by_revision() -> None:
    _, context, server = build_route_context((("a", True),), ("stale-fallback",))
    calls = 0

    def supplier() -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ("a",) if calls == 1 else ()

    context.active_provider_ids_supplier = supplier
    before = await read_resource(server, RESOURCE)
    assert calls == 1
    after = await read_resource(server, RESOURCE)
    assert calls == 2
    assert before["runtime_revision"] == after["runtime_revision"]
    assert all(row["status"] == "active" for row in before["rows"])
    assert all(row["status"] == "unavailable" for row in after["rows"])


async def test_route_keys_do_not_merge_same_provider_routes_or_invent_identity() -> None:
    _, context, server = build_route_context((("zigbee", True),), ("zigbee",))
    snapshot = route_snapshot()
    alias = deepcopy(snapshot.source_entities[0])
    alias["entity_id"] = "private-external-entity"
    alias["capabilities"] = [
        dict(alias["capabilities"][0], writable=False, commands=[], guarantees={})
    ]
    separate = deepcopy(snapshot.source_entities[0])
    separate["entity_id"] = "separate-entity"
    separate["canonical_id"] = "home.separate"
    context.registry.apply_snapshot(
        AdapterSnapshot(source_entities=snapshot.source_entities + [alias, separate]), "zigbee"
    )
    first = await read_resource(server, RESOURCE)
    second = await read_resource(server, RESOURCE)
    assert first == second
    rows = {(row["device_id"], row["capability"]): row for row in first["rows"]}
    assert len(rows) == 4
    routes = rows[("home.light", "brightness")]["routes"]
    assert len(routes) == 2
    assert len({route["route_key"] for route in routes}) == 2
    assert {route["declared_operations"]["write"] for route in routes} == {True, False}
    assert "private-external-entity" not in json.dumps(first)
    for row in rows.values():
        assert_unknown(row)


async def test_families_show_missing_requirements_without_claiming_hardware() -> None:
    _, context, server = build_route_context((), ("provider",))
    context.registry.apply_snapshot(family_snapshot(), "provider")
    result = await read_resource(server, RESOURCE)
    families = {item["family"]: item for item in result["families"]}
    assert list(families) == ["lighting", "metered_plugs", "covers", "hvac", "schedulable_loads"]
    assert [item["priority"] for item in families.values()] == [1, 2, 3, 4, 5]
    for group in families.values():
        assert group["status"] == "not_qualified"
        assert group["hardware_verified"] is False
        assert "hardware_evidence_missing" in group["missing_requirements"]
    assert "requested_functions_unspecified" in families["lighting"]["missing_requirements"]
    plugs = families["metered_plugs"]
    assert plugs["candidate_device_ids"] == ["home.metered", "home.switch"]
    assert "metering_missing" in plugs["missing_requirements"]
    assert "plug_identity_unverified" in plugs["missing_requirements"]
    assert "setpoint_control_missing" in families["hvac"]["missing_requirements"]
    assert "position_feedback_missing" not in families["covers"]["missing_requirements"]
    assert families["schedulable_loads"]["candidate_device_ids"] == []
    assert "programmability_unverified" in families["schedulable_loads"]["missing_requirements"]


async def test_legacy_coverage_is_unchanged_by_evidence_reads() -> None:
    _, _, server = build_route_context((("a", True), ("b", False)), ("a",))
    before = await read_resource(server, "coverage")
    baseline_path = Path(__file__).parents[1] / "fixtures/contracts/coverage-before-evidence.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["runtime_revision"] = before["runtime_revision"]
    assert before == baseline
    assert set(before) == {"schema_version", "runtime_revision", "routes"}
    for row in before["routes"]:
        assert set(row) == {
            "device_id",
            "capability",
            "protocols",
            "operations",
            "guarantees",
            "limits",
            "status",
            "writable",
        }
        assert row["protocols"] == ["a", "b"]
    await read_resource(server, RESOURCE)
    assert await read_resource(server, "coverage") == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "qualified"),
        ("level", "hil"),
        ("firmware", "claimed-v1"),
        ("tested_at", "2026-09-11T00:00:00Z"),
        ("verified_capabilities", ["brightness"]),
        ("authority_created", True),
    ],
)
async def test_minimum_schema_rejects_unverified_positive_claims(field: str, value: object) -> None:
    _, _, server = build_route_context((("a", True),), ("a",))
    payload = await read_resource(server, RESOURCE)
    assert CoverageEvidenceSnapshot.model_validate(payload).model_dump(mode="json") == payload
    payload["rows"][0]["routes"][0]["evidence"][field] = value
    with pytest.raises(ValidationError):
        CoverageEvidenceSnapshot.model_validate(payload)


async def test_source_metadata_claims_do_not_become_tested_hardware() -> None:
    _, context, server = build_route_context((), ("mqtt",))
    snapshot = route_snapshot()
    for cap in snapshot.source_entities[0]["capabilities"]:
        cap["constraints"] = {
            "evidence_class": "hardware",
            "status": "qualified",
            "firmware": "v9",
            "profile": "claims-only",
            "tested_at": "2020-01-01T00:00:00Z",
            "expires_at": "2020-01-02T00:00:00Z",
        }
    context.registry.apply_snapshot(snapshot, "mqtt")
    result = await read_resource(server, RESOURCE)
    for row in result["rows"]:
        assert_unknown(row)
    assert "claims-only" not in json.dumps(result)


async def test_strict_snapshot_rejects_duplicate_rows_and_hidden_family_refs() -> None:
    _, _, server = build_route_context((("a", True),), ("a",))
    payload = await read_resource(server, RESOURCE)
    duplicate = deepcopy(payload)
    duplicate["rows"].append(deepcopy(duplicate["rows"][0]))
    with pytest.raises(ValidationError, match="unique per canonical"):
        CoverageEvidenceSnapshot.model_validate(duplicate)
    payload["families"][0]["row_refs"].append({"device_id": "hidden", "capability": "power"})
    with pytest.raises(ValidationError, match="visible candidate rows"):
        CoverageEvidenceSnapshot.model_validate(payload)


async def test_typed_projection_does_not_alias_mutable_canonical_guarantees() -> None:
    _, context, _ = build_route_context((("a", True),), ("a",))
    devices = context.registry.devices
    snapshot = coverage_evidence_snapshot(
        context.registry, runtime_revision="inventory-1", active_provider_ids=("a",),
        devices=devices,
    )
    canonical = next(cap for cap in devices[0].capabilities if cap.name == "brightness")
    assert canonical.guarantees.readback_required is True
    snapshot.rows[0].declared_guarantees.readback_required = False
    assert canonical.guarantees.readback_required is True
    canonical.guarantees.availability_modes.append(AvailabilityMode.REMOTE)
    assert snapshot.rows[0].declared_guarantees.availability_modes == [AvailabilityMode.LOCAL]
