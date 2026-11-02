"""Spatial authority is preserved across the three public inventory views."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP

from domoai.domain.models import (
    Area,
    AuthorityContext,
    Device,
    DeviceType,
    PrincipalRole,
    SourceRef,
)
from domoai.mcp.unified_server import create_unified_server
from domoai.runtime.registry import DeviceRegistry
from tests.contract.test_unified_mcp_contract import build_context, structured

SURFACES = ("discover_devices", "domotics://devices", "domotics://areas")


def seed_inventory(registry: DeviceRegistry, prefix: str = "scope") -> None:
    """Seed canonical metadata that discovery currently does not import for areas."""
    areas = [
        Area(
            id=f"{prefix}.public",
            name=f"{prefix} public",
            parent_id=f"{prefix}.private-parent",
            source_refs=[SourceRef(adapter_id="fixture", external_id="PRIVATE-AREA-REF")],
        ),
        Area(id=f"{prefix}.private-parent", name="PRIVATE-PARENT-NAME"),
        Area(id=f"{prefix}.private", name="PRIVATE-ROOM-NAME"),
        Area(id=f"{prefix}.empty", name=f"{prefix} empty"),
        Area(
            id=f"{prefix}.child",
            name=f"{prefix} child",
            parent_id=f"{prefix}.missing-parent",
        ),
    ]
    # There is no public canonical-area loader. apply_snapshot intentionally
    # imports only ID/name, so setup must retain parent/source metadata here.
    registry._areas.update({area.id: area for area in areas})
    registry.load_persisted(
        [
            Device(
                id=f"{prefix}.{name}",
                type=device_type,
                name=f"{prefix} {name}",
                area_id=f"{prefix}.{area}" if area else None,
                protocol="fixture",
                source_refs=[SourceRef(adapter_id="fixture", external_id=f"device-{name}")],
            )
            for name, area, device_type in [
                ("light", "public", DeviceType.LIGHT),
                ("sensor", "private", DeviceType.SENSOR),
                ("orphan", None, DeviceType.LIGHT),
                ("child-sensor", "child", DeviceType.SENSOR),
            ]
        ]
    )


def scoped_authority(
    *,
    devices: tuple[str, ...] = (),
    areas: tuple[str, ...] = (),
    tenant: str = "default",
    household: str = "default",
    principal: str = "scoped-reader",
) -> AuthorityContext:
    return AuthorityContext(
        tenant_id=tenant,
        household_id=household,
        household_ids=[household],
        principal_id=principal,
        roles=[PrincipalRole.VIEWER],
        device_ids=list(devices),
        area_ids=list(areas),
    )


def access_token(authority: AuthorityContext) -> AccessToken:
    return AccessToken(
        token="opaque-test-token",
        client_id=authority.principal_id,
        scopes=["read"],
        claims={"authority": authority.model_dump(mode="json")},
    )


def authenticate(monkeypatch: pytest.MonkeyPatch, authority: AuthorityContext | None) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    token = access_token(authority) if authority is not None else None
    monkeypatch.setattr(auth_context, "get_access_token", lambda: token)


async def read_inventory(server: FastMCP, surface: str, **arguments: Any) -> dict[str, Any]:
    if surface == "discover_devices":
        return structured(await server.call_tool(surface, {"refresh": False, **arguments}))
    contents = await server.read_resource(surface)
    return json.loads(contents[0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    ("devices", "areas", "expected_areas", "expected_devices"),
    [
        (("scope.light",), (), {"scope.public"}, {"scope.light"}),
        ((), ("scope.public", "scope.empty"), {"scope.public", "scope.empty"}, {"scope.light"}),
        (("scope.light",), ("scope.public", "scope.empty"), {"scope.public"}, {"scope.light"}),
        (("missing",), (), set(), set()),
        (("scope.light",), ("scope.private",), set(), set()),
        ((), ("missing",), set(), set()),
        ((), ("scope.empty",), {"scope.empty"}, set()),
        (("scope.orphan",), (), set(), {"scope.orphan"}),
    ],
)
async def test_inventory_area_acl_matrix(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    devices: tuple[str, ...],
    areas: tuple[str, ...],
    expected_areas: set[str],
    expected_devices: set[str],
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    authenticate(monkeypatch, scoped_authority(devices=devices, areas=areas))

    result = await read_inventory(create_unified_server(context), surface)

    assert {area["id"] for area in result["areas"]} == expected_areas
    if "devices" in result:
        assert {device["id"] for device in result["devices"]} == expected_devices
        assert all(Device.model_validate(device).source_refs for device in result["devices"])
    assert "PRIVATE-ROOM-NAME" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_restricted_area_metadata_does_not_leak(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    authenticate(monkeypatch, scoped_authority(areas=("scope.public",)))

    result = await read_inventory(create_unified_server(context), surface)

    assert result["areas"] == [
        {"id": "scope.public", "name": "scope public", "parent_id": None, "source_refs": []}
    ]
    assert Area.model_validate(result["areas"][0]).parent_id is None
    payload = json.dumps(result)
    for marker in (
        "private-parent",
        "PRIVATE-PARENT-NAME",
        "PRIVATE-AREA-REF",
        "PRIVATE-ROOM-NAME",
    ):
        assert marker not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_explicit_visible_parent_and_missing_parent(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    authenticate(
        monkeypatch,
        scoped_authority(areas=("scope.public", "scope.private-parent", "scope.child")),
    )

    result = await read_inventory(create_unified_server(context), surface)

    areas = {area["id"]: Area.model_validate(area) for area in result["areas"]}
    assert set(areas) == {"scope.public", "scope.private-parent", "scope.child"}
    assert areas["scope.public"].parent_id == "scope.private-parent"
    assert areas["scope.child"].parent_id is None
    assert all(not area.source_refs for area in areas.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_unrestricted_inventory_preserves_empty_areas_and_metadata(
    monkeypatch: pytest.MonkeyPatch, authenticated: bool
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    context.domotics.last_refreshed_at = datetime(2026, 9, 10, tzinfo=UTC)
    authenticate(monkeypatch, scoped_authority() if authenticated else None)
    expected = {area.id: area.model_dump(mode="json") for area in context.domotics.registry.areas}
    server = create_unified_server(context)

    for surface in SURFACES:
        result = await read_inventory(server, surface)
        assert {area["id"]: area for area in result["areas"]} == expected
        assert result["schema_version"] == "v1"
        assert result["runtime_revision"] == context.domotics.discovery.state_store.runtime_revision
        if surface != "domotics://areas":
            assert result["refreshed_at"] == "2026-09-10T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["unrestricted", "areas", "devices"])
@pytest.mark.parametrize(
    "filters",
    [{"area_id": "scope.public"}, {"types": ["sensor"]}, {"types": []}, {"area_id": "scope.empty"}],
)
async def test_search_filters_preserve_authorized_area_projection(
    monkeypatch: pytest.MonkeyPatch, scope: str, filters: dict[str, Any]
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    authorities = {
        "unrestricted": None,
        "areas": scoped_authority(areas=("scope.public", "scope.empty")),
        "devices": scoped_authority(devices=("scope.light",)),
    }
    authenticate(monkeypatch, authorities[scope])
    server = create_unified_server(context)
    before = await read_inventory(server, "domotics://devices")

    result = await read_inventory(server, "discover_devices", **filters)
    areas = await read_inventory(server, "domotics://areas")

    assert result["areas"] == before["areas"]
    assert {a["id"]: a for a in result["areas"]} == {a["id"]: a for a in areas["areas"]}
    expected = before["devices"]
    if "area_id" in filters:
        expected = [device for device in expected if device["area_id"] == filters["area_id"]]
    if "types" in filters:
        expected = [device for device in expected if device["type"] in filters["types"]]
    assert result["devices"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"area_id": "scope.private"}, "insufficient_scope"),
        ({"types": ["invalid"]}, "validation_error"),
    ],
)
async def test_discovery_errors_keep_v1_envelope(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], code: str
) -> None:
    _, context = await build_context()
    seed_inventory(context.domotics.registry)
    authenticate(monkeypatch, scoped_authority(areas=("scope.public",)))

    result = await read_inventory(create_unified_server(context), "discover_devices", **arguments)

    assert result["error"]["code"] == code
    assert "areas" not in result
    assert "devices" not in result
