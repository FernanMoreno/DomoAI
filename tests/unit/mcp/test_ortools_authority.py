from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.server.auth.provider import AccessToken
from tests.integration.test_ortools_mcp_fixtures import build_context, scenario_for, structured

from domoai.application.authority import AuthorityPolicy
from domoai.domain.models import AuthorityContext, Command, Plan, PrincipalRole
from domoai.mcp.ortools_server import create_ortools_server
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus


class CountingWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def optimize(self, _scenario: Any) -> OptimizationResult:
        self.calls += 1
        raise AssertionError("the optimizer worker must not run for denied requests")


def _token(authority: AuthorityContext) -> AccessToken:
    return AccessToken(
        token="opaque-test-token",
        client_id=authority.principal_id,
        scopes=["read"],
        claims={"authority": authority.model_dump(mode="json")},
    )


def _authority(
    *,
    tenant_id: str = "default",
    household_id: str = "default",
    device_ids: list[str] | None = None,
    area_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> AuthorityContext:
    return AuthorityContext(
        tenant_id=tenant_id,
        household_id=household_id,
        household_ids=[household_id],
        principal_id="scoped-viewer",
        roles=[PrincipalRole.VIEWER],
        device_ids=device_ids or [],
        area_ids=area_ids or [],
        capabilities=capabilities or [],
    )


@pytest.mark.asyncio
async def test_in_scope_viewer_can_validate_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    authority = _authority(device_ids=[device_id], capabilities=["set_brightness"])
    context.authority_policy = AuthorityPolicy()
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    result = structured(
        await create_ortools_server(context).call_tool(
            "validate_scenario",
            {"scenario": scenario_for(device_id).model_dump(mode="json")},
        )
    )

    assert result["valid"] is True


@pytest.mark.asyncio
async def test_out_of_scope_scenario_is_rejected_before_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    worker = CountingWorker()
    context.optimization_worker = worker
    context.authority_policy = AuthorityPolicy()
    authority = _authority(device_ids=["another.device"], capabilities=["set_brightness"])
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    result = structured(
        await create_ortools_server(context).call_tool(
            "optimize_scenario",
            {"scenario": scenario_for(device_id).model_dump(mode="json")},
        )
    )

    assert result["error"]["code"] == "insufficient_scope"
    assert worker.calls == 0
    assert device_id not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "household_id"),
    [("other-tenant", "default"), ("default", "other-household")],
)
async def test_cross_boundary_scenario_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tenant_id: str, household_id: str
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    context.authority_policy = AuthorityPolicy()
    authority = _authority(
        tenant_id=tenant_id,
        household_id=household_id,
        device_ids=[device_id],
        capabilities=["set_brightness"],
    )
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    result = structured(
        await create_ortools_server(context).call_tool(
            "validate_scenario",
            {"scenario": scenario_for(device_id).model_dump(mode="json")},
        )
    )

    assert result["error"]["code"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_area_allowlist_uses_canonical_registry_area(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    context.authority_policy = AuthorityPolicy()
    authority = _authority(
        device_ids=[device_id],
        area_ids=["area-that-is-not-the-device-area"],
        capabilities=["set_brightness"],
    )
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    result = structured(
        await create_ortools_server(context).call_tool(
            "validate_scenario",
            {"scenario": scenario_for(device_id).model_dump(mode="json")},
        )
    )

    assert result["error"]["code"] == "insufficient_scope"


def _result_with_plans(device_ids: list[str]) -> dict[str, Any]:
    plans = [
        Plan(
            id=f"proposal-{index}",
            created_at=datetime(2026, 8, 15, tzinfo=UTC),
            commands=[
                Command(
                    id=f"command-{index}",
                    device_id=device_id,
                    command="set_brightness",
                    idempotency_key=f"intent-{index}",
                )
            ],
        )
        for index, device_id in enumerate(device_ids)
    ]
    return OptimizationResult(
        scenario_id="projection-scope",
        status=OptimizationStatus.OPTIMAL,
        solver="test",
        plan=plans[0],
        plans=plans,
        objective_values={"cost": 1.0},
        constraint_summary={"hard_satisfied": True},
    ).model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["explain_solution", "summarize_solution"])
async def test_projection_rejects_any_out_of_scope_plan(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    permitted = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    context.authority_policy = AuthorityPolicy()
    authority = _authority(device_ids=[permitted], capabilities=["set_brightness"])
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    result = structured(
        await create_ortools_server(context).call_tool(
            tool_name,
            {"result": _result_with_plans([permitted, "outside.device"])},
        )
    )

    assert result["error"]["code"] == "insufficient_scope"
    assert "outside.device" not in str(result)


@pytest.mark.asyncio
async def test_compare_rejects_out_of_scope_variation_before_counterfactual_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    _adapter, context = await build_context()
    permitted = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    context.authority_policy = AuthorityPolicy()
    authority = _authority(device_ids=[permitted], capabilities=["set_brightness"])
    monkeypatch.setattr(auth_context, "get_access_token", lambda: _token(authority))

    baseline = scenario_for(permitted)
    variation = baseline.model_copy(update={"id": "outside-variation"})
    variation.loads[0].device_id = "outside.device"
    result = structured(
        await create_ortools_server(context).call_tool(
            "compare_scenarios",
            {
                "baseline": baseline.model_dump(mode="json"),
                "variations": {"outside": variation.model_dump(mode="json")},
            },
        )
    )

    assert result["error"]["code"] == "insufficient_scope"
    assert "outside.device" not in str(result)
