"""Configured runtimes keep inventory scope separate from shared revisions."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from domoai.config.settings import Settings
from domoai.domain.models import AuthorityContext
from domoai.mcp.configured import build_configured_server
from tests.contract.test_scoped_area_inventory_contract import (
    SURFACES,
    access_token,
    read_inventory,
    scoped_authority,
    seed_inventory,
)

pytestmark = [pytest.mark.composition, pytest.mark.asyncio]


@contextmanager
def request_authority(authority: AuthorityContext | None):
    user = AuthenticatedUser(access_token(authority)) if authority is not None else None
    token = auth_context_var.set(user)
    try:
        yield
    finally:
        auth_context_var.reset(token)


@asynccontextmanager
async def configured_homes(tmp_path: Path):
    async with AsyncExitStack() as stack:
        homes = []
        for household in ("home-a", "home-b"):
            runtime, server = await build_configured_server(
                Settings(
                    database_path=tmp_path / f"{household}.sqlite3",
                    connection_master_key_path=tmp_path / f"{household}.key",
                    mcp_tenant_id="tenant-a",
                    mcp_household_id=household,
                )
            )
            stack.push_async_callback(runtime.close)
            seed_inventory(runtime.registry, household)
            homes.append((household, runtime, server))
        yield homes


async def test_scoped_inventory_rejects_foreign_home_and_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with configured_homes(tmp_path) as homes:
        for household, runtime, server in homes:
            refresh = AsyncMock(side_effect=AssertionError("denied refresh must not run"))
            monkeypatch.setattr(runtime.discovery, "refresh", refresh)
            before = runtime.state_store.runtime_revision
            foreign_home = "home-b" if household == "home-a" else "home-a"
            authorities = [
                scoped_authority(tenant="tenant-a", household=foreign_home),
                scoped_authority(tenant="tenant-b", household=household),
                scoped_authority(tenant="tenant-a", household=household).model_copy(
                    update={"operations": ["get_state"]}
                ),
            ]
            for authority in authorities:
                with request_authority(authority):
                    for surface in SURFACES:
                        result = await read_inventory(server, surface, refresh=True)
                        assert result["error"]["code"] == "insufficient_scope"
                        assert "areas" not in result and "devices" not in result
                        assert "PRIVATE" not in json.dumps(result)
            assert refresh.await_count == 0
            assert runtime.state_store.runtime_revision == before


async def test_scoped_inventory_reprojects_without_revision_change(tmp_path: Path) -> None:
    async with configured_homes(tmp_path) as homes:
        for household, runtime, server in homes:
            before_areas = [area.model_dump(mode="json") for area in runtime.registry.areas]
            before_devices = [device.model_dump(mode="json") for device in runtime.registry.devices]
            revision = runtime.state_store.runtime_revision
            cases = [
                ((f"{household}.light",), (), {f"{household}.public"}),
                (
                    (),
                    (f"{household}.public", f"{household}.empty"),
                    {f"{household}.public", f"{household}.empty"},
                ),
                ((), (), {area["id"] for area in before_areas}),
                (("missing",), (), set()),
                ((f"{household}.light",), (), {f"{household}.public"}),
            ]
            for index, (devices, areas, expected) in enumerate(cases):
                authority = scoped_authority(
                    tenant="tenant-a",
                    household=household,
                    devices=devices,
                    areas=areas,
                    principal=f"reader-{index}",
                )
                authority_before = authority.model_dump(mode="json")
                with request_authority(authority):
                    views = [await read_inventory(server, surface) for surface in SURFACES]
                area_views = [{area["id"]: area for area in view["areas"]} for view in views]
                assert area_views[0] == area_views[1] == area_views[2]
                assert set(area_views[0]) == expected
                assert all(view["runtime_revision"] == revision for view in views)
                if devices or areas:
                    assert "PRIVATE" not in json.dumps(views)
                else:
                    assert area_views[0] == {area["id"]: area for area in before_areas}
                    assert views[0]["devices"] == sorted(
                        before_devices, key=lambda item: item["id"]
                    )
                assert authority.model_dump(mode="json") == authority_before
            # No token: the configured local service still sees the whole home.
            with request_authority(None):
                local = await read_inventory(server, "domotics://areas")
            assert {area["id"]: area for area in local["areas"]} == {
                area["id"]: area for area in before_areas
            }
            assert [area.model_dump(mode="json") for area in runtime.registry.areas] == before_areas
            assert [
                device.model_dump(mode="json") for device in runtime.registry.devices
            ] == before_devices


async def test_scoped_inventory_keeps_concurrent_home_views_independent(tmp_path: Path) -> None:
    async with configured_homes(tmp_path) as homes:

        async def read_home(household, runtime, server, visible: bool):
            authority = scoped_authority(
                tenant="tenant-a",
                household=household,
                devices=(f"{household}.light",) if visible else ("missing",),
            )
            with request_authority(authority):
                for surface in SURFACES:
                    await asyncio.sleep(0)
                    result = await read_inventory(server, surface)
                    assert {area["id"] for area in result["areas"]} == (
                        {f"{household}.public"} if visible else set()
                    )
                    assert result["runtime_revision"] == runtime.state_store.runtime_revision
                    other = "home-b" if household == "home-a" else "home-a"
                    assert other not in json.dumps(result)
                    assert "PRIVATE" not in json.dumps(result)

        await asyncio.gather(
            *(read_home(*home, visible) for home in homes for visible in (True, False))
        )
