from __future__ import annotations

from typing import Any
from unittest.mock import ANY

import pytest

from domoai.domain.models import Policy, PolicyAction, StateStatus
from domoai.skills.validator import V2_OPERATION_BINDINGS, V3_OPERATION_BINDINGS
from domoai.skills.workflow import (
    ApprovalDecision,
    EnergySkillRequest,
    EnergySkillWorkflow,
    WorkflowStage,
    WorkflowStatus,
    bundle_approval_digest,
)
from tests.fixtures.skill_workflow import (
    FIXTURE_OPERATOR_TOKEN,
    build_workflow_fixture,
    default_horizon,
    future_horizon,
    light_device_id,
    multi_slot_scenario_for,
    scenario_for,
    switch_device_id,
)


def request_for(fixture: Any, **scenario_options: Any) -> EnergySkillRequest:
    device_id = light_device_id(fixture)
    scenario_options.setdefault("horizon", fixture.horizon)
    return EnergySkillRequest(
        scenario=scenario_for(device_id, **scenario_options),
        devices=[device_id],
        capabilities=["brightness"],
    )


def bundle_request_for(fixture: Any, **scenario_options: Any) -> EnergySkillRequest:
    light_id = light_device_id(fixture)
    switch_id = switch_device_id(fixture)
    scenario_options.setdefault("device_ids", (light_id, switch_id))
    scenario_options.setdefault("horizon", fixture.horizon)
    return EnergySkillRequest(
        scenario=multi_slot_scenario_for(light_id, **scenario_options),
        devices=[light_id, switch_id],
        capabilities=["power"],
    )


@pytest.mark.asyncio
async def test_workflow_routes_semantic_operations_across_both_mcp_servers() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(request_for(fixture))

    assert result.status is WorkflowStatus.AWAITING_APPROVAL
    assert result.stage is WorkflowStage.AWAITING_APPROVAL
    assert result.completed_operations == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "optimize_scenario",
        "validate_plan",
        "explain_solution",
        "operator_approval",
    ]
    assert fixture.router.calls == [
        ("mcp", "discover_devices", {"refresh": False}),
        (
            "mcp",
            "get_state",
            {
                "devices": [light_device_id(fixture)],
                "capabilities": ["brightness"],
                "allow_stale": True,
            },
        ),
        ("mcp", "get_energy_context", ANY),
        ("mcp", "optimize_scenario", ANY),
        ("mcp", "validate_plan", ANY),
        ("mcp", "explain_solution", ANY),
    ]
    optimize_arguments = fixture.router.calls[3][2]
    assert optimize_arguments["scenario"]["energy_context"]["schema_version"] == "v1"
    assert fixture.domotics_adapter.calls == []
    assert result.plan_id
    assert result.validation_digest
    assert fixture.approval.requests


@pytest.mark.asyncio
async def test_workflow_pauses_then_resumes_through_domotics_execute_once() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    pending = await workflow.run(request_for(fixture))

    approval = ApprovalDecision(
        approved=True,
        approved_by="fixture-operator",
        validation_digest=pending.validation_digest or "",
        operator_token=FIXTURE_OPERATOR_TOKEN,
    )
    completed = await workflow.resume(pending, approval)
    repeated = await workflow.resume(pending, approval)

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.stage is WorkflowStage.COMPLETED
    assert [call[0:2] for call in fixture.router.calls] == [
        ("mcp", "discover_devices"),
        ("mcp", "get_state"),
        ("mcp", "get_energy_context"),
        ("mcp", "optimize_scenario"),
        ("mcp", "validate_plan"),
        ("mcp", "explain_solution"),
        ("mcp", "request_approval"),
        ("mcp", "execute_plan"),
    ]
    request_approval_arguments = next(
        arguments
        for _provider, tool, arguments in fixture.router.calls
        if tool == "request_approval"
    )
    execute_plan_arguments = next(
        arguments for _provider, tool, arguments in fixture.router.calls if tool == "execute_plan"
    )
    assert "bundle_digest" not in request_approval_arguments
    assert "bundle_digest" not in execute_plan_arguments
    assert len(fixture.domotics_adapter.calls) == 1
    assert repeated == completed
    assert completed.stage_history == [
        WorkflowStage.STARTED,
        WorkflowStage.CONTEXT_READY,
        WorkflowStage.PROPOSAL_READY,
        WorkflowStage.VALIDATED,
        WorkflowStage.AWAITING_APPROVAL,
        WorkflowStage.EXECUTING,
        WorkflowStage.COMPLETED,
    ]


@pytest.mark.asyncio
async def test_safe_plan_executes_without_synthetic_operator_approval() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(request_for(fixture))

    assert result.status is WorkflowStatus.COMPLETED
    assert result.completed_operations[-1] == "execute_plan"
    assert fixture.approval.requests == []
    assert len(fixture.domotics_adapter.calls) == 1


@pytest.mark.asyncio
async def test_workflow_rejects_unknown_input_before_any_mcp_call() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    device_id = light_device_id(fixture)
    scenario = scenario_for(device_id)

    result = await workflow.run(
        {
            "scenario": scenario.model_dump(mode="json"),
            "devices": [device_id],
            "unexpected": True,
        }
    )

    assert result.status is WorkflowStatus.BLOCKED
    assert result.stage is WorkflowStage.STARTED
    assert result.diagnostics[0].code == "invalid_workflow_input"
    assert fixture.router.calls == []


@pytest.mark.asyncio
async def test_workflow_stops_on_stale_state_before_optimization() -> None:
    fixture = await build_workflow_fixture()
    await fixture.domotics_context.discovery.state_store.mark_all_stale()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(request_for(fixture))

    assert result.status is WorkflowStatus.BLOCKED
    assert result.stage is WorkflowStage.CONTEXT_READY
    assert result.diagnostics[0].code == "stale_state"
    assert [call[1] for call in fixture.router.calls] == ["discover_devices", "get_state"]


@pytest.mark.asyncio
async def test_explicit_stale_assumption_allows_stale_state() -> None:
    fixture = await build_workflow_fixture()
    await fixture.domotics_context.discovery.state_store.mark_all_stale()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    request = request_for(fixture).model_copy(update={"accept_stale_assumption": True})

    result = await workflow.run(request)

    assert result.status is WorkflowStatus.COMPLETED
    assert fixture.router.calls[-1][1] == "execute_plan"


@pytest.mark.asyncio
async def test_stale_energy_context_stops_before_optimization() -> None:
    fixture = await build_workflow_fixture()
    original = fixture.router.call

    async def stale_context(provider: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await original(provider, tool, arguments)
        if tool == "get_energy_context":
            response["runtime_revision"] = "stale-runtime-revision"
        return response

    fixture.router.call = stale_context  # type: ignore[method-assign]
    result = await EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    ).run(request_for(fixture))

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "stale_energy_context"
    assert [call[1] for call in fixture.router.calls] == [
        "discover_devices",
        "get_state",
        "get_energy_context",
    ]
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_invalid_energy_context_stops_before_optimization() -> None:
    fixture = await build_workflow_fixture()
    original = fixture.router.call

    async def invalid_context(
        provider: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        response = await original(provider, tool, arguments)
        if tool == "get_energy_context":
            response["context"] = {"schema_version": "v1"}
        return response

    fixture.router.call = invalid_context  # type: ignore[method-assign]
    result = await EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    ).run(request_for(fixture))

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "invalid_energy_context"
    assert all(call[1] != "optimize_scenario" for call in fixture.router.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_options", "expected_code"),
    [
        ({"max_power": 50}, "infeasible_proposal"),
        ({"solver_time_limit_seconds": 0}, "timeout_proposal"),
    ],
)
async def test_failed_optimizer_statuses_stop_before_validation(
    scenario_options: dict[str, Any], expected_code: str
) -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(request_for(fixture, **scenario_options))

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == expected_code
    assert [call[1] for call in fixture.router.calls] == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "optimize_scenario",
    ]


@pytest.mark.asyncio
async def test_invalid_proposal_status_stops_before_validation() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    request = request_for(fixture).model_copy(
        update={
            "scenario": scenario_for("unknown.device"),
        }
    )

    result = await workflow.run(request)

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "missing_device"
    assert [call[1] for call in fixture.router.calls] == ["discover_devices"]


@pytest.mark.asyncio
async def test_workflow_reports_operator_authentication_failure_when_host_omits_token() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    pending = await workflow.run(request_for(fixture))

    result = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            validation_digest=pending.validation_digest or "",
        ),
    )

    assert result.status is WorkflowStatus.FAILED
    assert result.diagnostics[-1].code == "operator_authentication_failed"
    assert fixture.domotics_adapter.calls == []
    assert all(call[1] != "execute_plan" for call in fixture.router.calls)


@pytest.mark.asyncio
async def test_runtime_revision_change_blocks_before_execution() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    pending = await workflow.run(request_for(fixture))
    fixture.domotics_context.discovery.state_store.begin_revision()

    result = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            validation_digest=pending.validation_digest or "",
            operator_token=FIXTURE_OPERATOR_TOKEN,
        ),
    )

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "runtime_revision_changed"
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_missing_approval_and_mismatched_digest_never_execute() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    pending = await workflow.run(request_for(fixture))

    missing = await workflow.resume(pending, None)
    mismatched = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            validation_digest="wrong-digest",
        ),
    )

    assert missing.status is WorkflowStatus.AWAITING_APPROVAL
    assert mismatched.status is WorkflowStatus.BLOCKED
    assert fixture.domotics_adapter.calls == []
    assert all(call[1] != "execute_plan" for call in fixture.router.calls)


@pytest.mark.asyncio
async def test_malformed_tool_response_is_sanitized_and_stops() -> None:
    fixture = await build_workflow_fixture()
    original = fixture.router.call

    async def malformed(provider: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "get_state":
            fixture.router.calls.append((provider, tool, arguments))
            return {"unexpected": object()}
        return await original(provider, tool, arguments)

    fixture.router.call = malformed  # type: ignore[method-assign]
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(request_for(fixture))

    assert result.status is WorkflowStatus.FAILED
    assert result.diagnostics[0].code == "malformed_response"
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_state_status_invalid_is_not_overridden_by_stale_assumption() -> None:
    fixture = await build_workflow_fixture()
    device_id = light_device_id(fixture)
    states = await fixture.domotics_context.discovery.state_store.all()
    target = next(
        state
        for state in states
        if state.device_id == device_id and state.capability == "brightness"
    )
    await fixture.domotics_context.discovery.state_store.save(
        target.model_copy(update={"status": StateStatus.INVALID})
    )
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )
    request = request_for(fixture).model_copy(update={"accept_stale_assumption": True})

    result = await workflow.run(request)

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "invalid_state"


@pytest.mark.asyncio
async def test_two_host_role_mappings_produce_equivalent_traces() -> None:
    first = await build_workflow_fixture()
    second = await build_workflow_fixture(
        tool_aliases={
            ("mcp", "discover_devices"): "discover_devices",
            ("mcp", "get_state"): "get_state",
            ("mcp", "get_energy_context"): "get_energy_context",
            ("mcp", "optimize_scenario"): "optimize_scenario",
            ("mcp", "validate_plan"): "validate_plan",
            ("mcp", "explain_solution"): "explain_solution",
            ("mcp", "execute_plan"): "execute_plan",
        }
    )
    first_result = await EnergySkillWorkflow(
        first.router, first.approval, operation_bindings=V2_OPERATION_BINDINGS
    ).run(request_for(first))
    second_result = await EnergySkillWorkflow(
        second.router, second.approval, operation_bindings=V2_OPERATION_BINDINGS
    ).run(request_for(second))

    assert first_result.status is second_result.status is WorkflowStatus.COMPLETED
    assert [(provider, tool) for provider, tool, _ in first.router.calls] == [
        (provider, tool) for provider, tool, _ in second.router.calls
    ]
    assert first_result.plan_id == second_result.plan_id
    assert first_result.validation_digest == second_result.validation_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_binding",
    [
        ("hue", "turn_on"),
        ("mcp", "vendor_cluster"),
        ("mcp", "execute_python"),
    ],
)
async def test_router_rejects_nonportable_routes(
    invalid_binding: tuple[str, str],
) -> None:
    fixture = await build_workflow_fixture()
    with pytest.raises(ValueError, match="portable v1 or v2 routes"):
        EnergySkillWorkflow(
            fixture.router,
            fixture.approval,
            operation_bindings={"discover_devices": (*invalid_binding, "read")},
        )


@pytest.mark.asyncio
async def test_bundle_validation_covers_every_member() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))

    assert result.status is WorkflowStatus.COMPLETED
    assert len(result.plan_ids) == 2
    assert len(result.validation_digests) == 2
    assert result.plan_id == result.plan_ids[0]


@pytest.mark.asyncio
async def test_v3_workflow_commits_bundle_through_one_runtime_boundary() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))

    assert result.status is WorkflowStatus.COMPLETED
    assert result.bundle_commit_status == "completed"
    assert result.bundle_commit_id
    assert result.completed_operations[-1] == "commit_or_schedule_bundle"
    assert [tool for _provider, tool, _arguments in fixture.router.calls] == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "optimize_scenario",
        "validate_plan",
        "validate_plan",
        "explain_solution",
        "commit_or_schedule_bundle",
    ]
    commit_arguments = fixture.router.calls[-1][2]
    assert commit_arguments["bundle_digest"] == result.bundle_digest
    assert [member["validation_digest"] for member in commit_arguments["members"]] == (
        result.validation_digests
    )
    assert fixture.domotics_adapter.calls


@pytest.mark.asyncio
async def test_v3_workflow_uses_member_grants_after_one_bundle_decision() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    )

    pending = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))
    assert pending.status is WorkflowStatus.AWAITING_APPROVAL
    assert pending.bundle_digest
    assert fixture.approval.requests[0][1]["bundle_digest"] == pending.bundle_digest

    completed = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            bundle_digest=pending.bundle_digest,
            operator_token=FIXTURE_OPERATOR_TOKEN,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert [tool for _provider, tool, _arguments in fixture.router.calls].count(
        "request_approval"
    ) == 2
    assert [tool for _provider, tool, _arguments in fixture.router.calls].count(
        "commit_or_schedule_bundle"
    ) == 1
    assert all(
        tool not in {"execute_plan", "schedule_plan"}
        for _provider, tool, _arguments in fixture.router.calls
    )


@pytest.mark.asyncio
async def test_v3_workflow_reports_mixed_physical_and_scheduled_members() -> None:
    horizon = future_horizon(slots=4)
    fixture = await build_workflow_fixture(horizon=horizon)
    workflow = EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=horizon, slots=(0, 3)))

    assert result.status is WorkflowStatus.SCHEDULED
    assert result.bundle_commit_status == "scheduled"
    assert result.scheduled_plan_ids == [result.plan_ids[1]]
    assert len(fixture.domotics_adapter.calls) == 1
    assert [tool for _provider, tool, _arguments in fixture.router.calls].count(
        "commit_or_schedule_bundle"
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_status", "expected_status"),
    [
        ("partially_committed", WorkflowStatus.PARTIALLY_COMMITTED),
        ("unknown", WorkflowStatus.UNKNOWN),
    ],
)
async def test_v3_workflow_preserves_partial_commit_statuses(
    aggregate_status: str, expected_status: WorkflowStatus
) -> None:
    fixture = await build_workflow_fixture()
    original = fixture.router.call

    async def partial_commit(provider: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "commit_or_schedule_bundle":
            return {
                "schema_version": "v1",
                "bundle_commit_id": "bundle-failure-fixture",
                "status": aggregate_status,
                "members": [
                    {
                        "plan_id": member["plan_id"],
                        "status": "unknown",
                        "execution_status": "unknown",
                        "error_code": "execution_unknown",
                    }
                    for member in arguments["members"]
                ],
            }
        return await original(provider, tool, arguments)

    fixture.router.call = partial_commit  # type: ignore[method-assign]
    workflow = EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))

    assert result.status is expected_status
    assert result.bundle_commit_status == aggregate_status
    assert result.member_outcomes
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_bundle_all_or_nothing_on_invalid_member() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(
        bundle_request_for(
            fixture,
            horizon=default_horizon(),
            invalid_device_id="nonexistent.device",
        )
    )

    assert result.status in {WorkflowStatus.BLOCKED, WorkflowStatus.FAILED}
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_bundle_single_decision_covers_every_confirming_member() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    pending = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))
    assert pending.status is WorkflowStatus.AWAITING_APPROVAL
    assert len(fixture.approval.requests) == 1

    completed = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            validation_digest=pending.validation_digest or "",
            operator_token=FIXTURE_OPERATOR_TOKEN,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert len(completed.plan_ids) == 2


def test_bundle_approval_digest_changes_when_order_or_member_evidence_changes() -> None:
    bundle = [
        {
            "plan_id": "plan-a",
            "validation_digest": "sha256:a",
            "execute_at": "2026-08-21T10:00:00+00:00",
        },
        {
            "plan_id": "plan-b",
            "validation_digest": "sha256:b",
            "execute_at": None,
        },
    ]

    original = bundle_approval_digest("scenario-1", bundle)
    reordered = bundle_approval_digest("scenario-1", list(reversed(bundle)))
    changed_member = bundle_approval_digest(
        "scenario-1",
        [bundle[0], {**bundle[1], "validation_digest": "sha256:changed"}],
    )
    changed_time = bundle_approval_digest(
        "scenario-1",
        [{**bundle[0], "execute_at": "2026-08-21T10:15:00+00:00"}, bundle[1]],
    )
    changed_scenario = bundle_approval_digest("scenario-2", bundle)

    assert len(original) == 71
    assert len({original, reordered, changed_member, changed_time, changed_scenario}) == 5


@pytest.mark.asyncio
async def test_mixed_bundle_approval_uses_full_bundle_digest() -> None:
    fixture = await build_workflow_fixture()
    switch_id = switch_device_id(fixture)
    fixture.domotics_context.facade.plan_service.policy_engine.policies.append(
        Policy(
            id="confirm-only-switch-power",
            target={"device_id": switch_id, "capability": "power"},
            action=PolicyAction.CONFIRM,
        )
    )
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    pending = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))

    assert pending.status is WorkflowStatus.AWAITING_APPROVAL
    assert pending.bundle_digest
    assert pending.bundle_digest == pending.validation_digest
    assert pending.bundle_digest != pending.validation_digests[0]
    assert fixture.approval.requests[0][1]["bundle_digest"] == pending.bundle_digest

    completed = await workflow.resume(
        pending,
        ApprovalDecision(
            approved=True,
            approved_by="fixture-operator",
            bundle_digest=pending.bundle_digest,
            operator_token=FIXTURE_OPERATOR_TOKEN,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    member_approval_calls = [
        arguments
        for _provider, tool, arguments in fixture.router.calls
        if tool == "request_approval"
    ]
    assert [call["validation_digest"] for call in member_approval_calls] == [
        pending.validation_digests[1]
    ]
    assert all(
        tool not in {"execute_plan", "schedule_plan"}
        for _provider, tool, _arguments in fixture.router.calls
    )
    commit_calls = [
        arguments
        for _provider, tool, arguments in fixture.router.calls
        if tool == "commit_or_schedule_bundle"
    ]
    assert len(commit_calls) == 1
    assert [member["validation_digest"] for member in commit_calls[0]["members"]] == [
        pending.validation_digests[0],
        pending.validation_digests[1],
    ]


@pytest.mark.asyncio
async def test_bundle_decline_blocks_every_member() -> None:
    fixture = await build_workflow_fixture(confirmation_required=True)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    pending = await workflow.run(bundle_request_for(fixture, horizon=default_horizon()))

    result = await workflow.resume(
        pending,
        ApprovalDecision(approved=False, reason="declined"),
    )

    assert result.status is WorkflowStatus.CANCELLED
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_bundle_future_members_are_scheduled_not_dropped() -> None:
    horizon = future_horizon(slots=4)
    fixture = await build_workflow_fixture(horizon=horizon)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=horizon, slots=(0, 3)))

    assert result.status is WorkflowStatus.SCHEDULED
    assert len(result.scheduled_plan_ids) == 1
    assert len(fixture.domotics_adapter.calls) == 1

    assert all(
        tool not in {"execute_plan", "schedule_plan"}
        for _provider, tool, _arguments in fixture.router.calls
    )
    commit_calls = [
        arguments
        for _provider, tool, arguments in fixture.router.calls
        if tool == "commit_or_schedule_bundle"
    ]
    assert len(commit_calls) == 1
    assert [member["validation_digest"] for member in commit_calls[0]["members"]] == (
        result.validation_digests
    )

    listed = await fixture.router.call("mcp", "list_scheduled_plans", {})
    listed_ids = {entry["plan_id"] for entry in listed["plans"]}
    assert set(result.scheduled_plan_ids) <= listed_ids


@pytest.mark.asyncio
async def test_single_future_plan_is_scheduled_instead_of_rejected() -> None:
    horizon = future_horizon(slots=4)
    fixture = await build_workflow_fixture(horizon=horizon)
    workflow = EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    )

    result = await workflow.run(bundle_request_for(fixture, horizon=horizon, slots=(3,)))

    assert result.status is WorkflowStatus.SCHEDULED
    assert result.scheduled_plan_ids == result.plan_ids
    assert fixture.domotics_adapter.calls == []


@pytest.mark.asyncio
async def test_workflow_stops_before_approval_for_unbound_battery_proposal() -> None:
    fixture = await build_workflow_fixture()
    original = fixture.router.call

    async def unbound_battery_proposal(
        provider: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        response = await original(provider, tool, arguments)
        if tool == "optimize_scenario":
            return {
                **response,
                "status": "invalid",
                "plan": None,
                "plans": [],
                "diagnostics": [
                    {
                        "code": "battery_actuation_unbound",
                        "message": "Battery dispatch has no physical actuator binding",
                        "retryable": False,
                    }
                ],
            }
        return response

    fixture.router.call = unbound_battery_proposal  # type: ignore[method-assign]
    result = await EnergySkillWorkflow(
        fixture.router, fixture.approval, operation_bindings=V2_OPERATION_BINDINGS
    ).run(request_for(fixture))

    assert result.status is WorkflowStatus.BLOCKED
    assert result.diagnostics[0].code == "invalid_proposal"
    assert fixture.approval.requests == []
    assert fixture.domotics_adapter.calls == []
    assert all(
        call[1] not in {"request_approval", "execute_plan", "schedule_plan"}
        for call in fixture.router.calls
    )
