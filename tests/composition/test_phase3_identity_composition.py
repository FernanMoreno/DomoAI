from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.authority import AuthorityPolicy
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AuthorityContext, Command, PrincipalRole
from tests.integration.test_ortools_mcp_fixtures import scenario_for, structured


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_persists_authenticated_home_identity_with_plan(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            mcp_tenant_id="tenant-a",
            mcp_household_id="home-a",
        ),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        device_id = next(
            device.id for device in runtime.registry.devices if device.type.value == "light"
        )
        device = next(device for device in runtime.registry.devices if device.id == device_id)
        assert device.area_id is not None
        plan = runtime.plan_service.create_plan(
            "composition-identity-plan",
            [
                Command(
                    id="composition-identity-command",
                    device_id=device_id,
                    command="set_brightness",
                    value=50,
                    idempotency_key="composition-identity-intent",
                )
            ],
        )
        caller = AuthorityContext(
            tenant_id="tenant-a",
            household_id="home-a",
            household_ids=["home-a"],
            principal_id="operator-a",
            roles=[PrincipalRole.OPERATOR],
            area_ids=[device.area_id],
            device_ids=[device_id],
            capabilities=["set_brightness"],
            operations=["prepare_plan"],
        )
        bound = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            plan,
            caller,
            device_areas={item.id: item.area_id for item in runtime.registry.devices},
        )
        await runtime.plan_repository.save(bound)

        restored = await runtime.plan_repository.get(bound.id)

        assert restored is not None
        assert restored.authority.principal_id == "operator-a"
        assert restored.authority.household_id == "home-a"
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_configured_server_applies_deployment_policy_to_ortools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    from domoai.mcp.configured import build_configured_server

    runtime, server = await build_configured_server(
        Settings(
            database_path=tmp_path / "ortools-authority.sqlite3",
            mcp_tenant_id="tenant-a",
            mcp_household_id="home-a",
        )
    )
    try:
        device_id = next(
            device.id for device in runtime.registry.devices if device.type.value == "light"
        )
        authority = AuthorityContext(
            tenant_id="tenant-b",
            household_id="home-a",
            household_ids=["home-a"],
            principal_id="foreign-viewer",
            roles=[PrincipalRole.VIEWER],
            device_ids=[device_id],
            capabilities=["set_brightness"],
        )
        monkeypatch.setattr(
            auth_context,
            "get_access_token",
            lambda: AccessToken(
                token="opaque",
                client_id=authority.principal_id,
                scopes=["read"],
                claims={"authority": authority.model_dump(mode="json")},
            ),
        )

        result = structured(
            await server.call_tool(
                "validate_scenario",
                {"scenario": scenario_for(device_id).model_dump(mode="json")},
            )
        )
    finally:
        await runtime.close()

    assert result["error"]["code"] == "insufficient_scope"
