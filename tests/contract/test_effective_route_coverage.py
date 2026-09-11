"""MCP availability describes one available route backed by a live provider."""

import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.models import AdapterSnapshot, Capability, Device, DeviceType, SourceRef
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

CAPABILITIES: list[dict[str, Any]] = [
    {
        "name": "brightness",
        "kind": "number",
        "readable": True,
        "writable": True,
        "commands": ["set_brightness"],
        "unit": "%",
        "minimum": 0,
        "maximum": 100,
        "guarantees": {"confirmation_required": True, "readback_required": True},
    },
    {
        "name": "temperature",
        "kind": "number",
        "readable": True,
        "writable": False,
        "unit": "°C",
    },
]


def route_snapshot(
    *, available: bool = True, device_id: str = "home.light", area_id: str = "home.room"
) -> AdapterSnapshot:
    return AdapterSnapshot(
        areas=[{"id": area_id, "name": area_id}],
        source_entities=[
            {
                "entity_id": device_id,
                "canonical_id": device_id,
                "semantic_type": "light",
                "name": device_id,
                "area_id": area_id,
                "available": available,
                "capabilities": CAPABILITIES,
            }
        ],
    )


def build_route_context(
    route_availability: tuple[tuple[str, bool], ...], providers: tuple[str, ...]
) -> tuple[SimulatedHomeAdapter, DomoticsMcpContext, FastMCP]:
    adapter = SimulatedHomeAdapter(entities=[])
    registry = DeviceRegistry()
    if not route_availability:
        registry.load_persisted(
            [
                Device(
                    id="home.light",
                    name="Light",
                    type=DeviceType.LIGHT,
                    protocol="fixture",
                    source_refs=[SourceRef(adapter_id="a", external_id="home.light")],
                    capabilities=[Capability.model_validate(item) for item in CAPABILITIES],
                )
            ]
        )
    for provider, available in route_availability:
        registry.apply_snapshot(route_snapshot(available=available), provider)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    plans = PlanService(registry, state_store, PolicyEngine([]), audit)
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plans, PlanExecutor(adapter, plans, audit)),
        registry=registry,
        policies=[],
        active_provider_ids=providers,
    )
    return adapter, context, create_domotics_server(context)


async def read_resource(server: FastMCP, resource: str) -> dict[str, Any]:
    contents = await server.read_resource(f"domotics://{resource}")
    return json.loads(contents[0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["coverage", "runtime"])
@pytest.mark.parametrize(
    ("routes", "providers", "available"),
    [
        ((), ("a",), False),
        ((("a", False),), ("a",), False),
        ((("a", True),), (), False),
        ((("a", True),), ("a",), True),
        ((("a", True), ("b", False)), ("b",), False),
        ((("a", False), ("b", True)), ("b",), True),
        ((("a", True), ("b", True)), ("a", "b"), True),
        ((("a", True),), ("",), False),
    ],
    ids=[
        "no-routes",
        "route-down",
        "provider-absent",
        "active",
        "crossed-routes",
        "one-valid-route",
        "two-valid-routes",
        "empty-provider-id",
    ],
)
async def test_resources_require_available_route_and_its_current_provider(
    resource: str, routes: tuple[tuple[str, bool], ...], providers: tuple[str, ...], available: bool
) -> None:
    adapter, _, server = build_route_context(routes, providers)

    result = await read_resource(server, resource)

    entries = result["routes"] if resource == "coverage" else result["writable_capabilities"]
    assert entries
    for entry in entries:
        if resource == "coverage":
            assert entry["status"] == ("active" if available else "unavailable")
        else:
            assert entry["available"] is available
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_same_revision_operational_changes_preserve_metadata() -> None:
    adapter, context, server = build_route_context((("a", True),), ("a",))
    before_coverage = await read_resource(server, "coverage")
    before_runtime = await read_resource(server, "runtime")
    revision = context.discovery.state_store.runtime_revision
    metadata = [
        {key: value for key, value in item.items() if key != "status"}
        for item in before_coverage["routes"]
    ]
    assert len(before_coverage["routes"]) == 2
    assert len(before_runtime["writable_capabilities"]) == 1
    assert before_runtime["authority"]["battery_dispatch"] == "unsupported"

    for available in (False, True):
        if available:
            context.registry.mark_source_available("a")
        else:
            context.registry.mark_source_unavailable("a")
        coverage = await read_resource(server, "coverage")
        runtime = await read_resource(server, "runtime")
        assert coverage["runtime_revision"] == runtime["runtime_revision"] == revision
        assert [
            {key: value for key, value in item.items() if key != "status"}
            for item in coverage["routes"]
        ] == metadata
        assert all(
            item["status"] == ("active" if available else "unavailable")
            for item in coverage["routes"]
        )
        assert runtime["writable_capabilities"][0] == {
            **before_runtime["writable_capabilities"][0],
            "available": available,
        }
        assert runtime["authority"] == before_runtime["authority"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_effective_availability_never_resolves_ambiguous_command_authority() -> None:
    adapter, context, server = build_route_context((("a", False), ("b", True)), ("b",))
    before = context.registry.resolve_command_route("home.light", "set_brightness")

    coverage = await read_resource(server, "coverage")
    runtime = await read_resource(server, "runtime")

    assert all(item["status"] == "active" for item in coverage["routes"])
    assert runtime["writable_capabilities"][0]["available"] is True
    after = context.registry.resolve_command_route("home.light", "set_brightness")
    assert before == after
    assert after.reason == "ambiguous_route"
    assert after.route is None
    assert len(after.candidates) == 2
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["runtime", "coverage"])
async def test_live_provider_sample_is_taken_once_and_empty_overrides_fallback(
    resource: str,
) -> None:
    _, context, server = build_route_context((("a", True),), ("stale-fallback",))
    samples = 0

    def supplier() -> tuple[str, ...]:
        nonlocal samples
        samples += 1
        return ("a", "", "a") if samples == 1 else ()

    # Set on the context so the baseline fails on observable stale output,
    # not on a constructor argument which does not exist until implementation.
    context.active_provider_ids_supplier = supplier
    revision = context.discovery.state_store.runtime_revision

    for index, available in enumerate((True, False), start=1):
        result = await read_resource(server, resource)
        if resource == "runtime":
            assert result["providers"] == (
                [{"provider_id": "a", "active": True}] if available else []
            )
            assert result["writable_capabilities"][0]["available"] is available
        else:
            assert all(
                item["status"] == ("active" if available else "unavailable")
                for item in result["routes"]
            )
        assert samples == index
        assert result["runtime_revision"] == revision
