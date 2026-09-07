from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.authority import AuthorityPolicy
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AuthorityContext, Command, PrincipalRole


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
        )
        bound = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(plan, caller)
        await runtime.plan_repository.save(bound)

        restored = await runtime.plan_repository.get(bound.id)

        assert restored is not None
        assert restored.authority.principal_id == "operator-a"
        assert restored.authority.household_id == "home-a"
    finally:
        await runtime.close()
