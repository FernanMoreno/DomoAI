import pytest

from domoai.skills.workflow import EnergySkillRequest, EnergySkillWorkflow, WorkflowStatus
from tests.fixtures.skill_workflow import (
    build_workflow_fixture,
    light_device_id,
    scenario_for,
)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_default_skill_selects_v3_and_commits_through_bundle_boundary() -> None:
    fixture = await build_workflow_fixture()
    device_id = light_device_id(fixture)
    workflow = EnergySkillWorkflow(fixture.router, fixture.approval)

    result = await workflow.run(
        EnergySkillRequest(
            scenario=scenario_for(device_id),
            devices=[device_id],
            capabilities=["brightness"],
        )
    )

    assert workflow.contract_version == "v3"
    assert result.status in {WorkflowStatus.COMPLETED, WorkflowStatus.SCHEDULED}
    assert any(
        provider == "mcp" and tool == "commit_or_schedule_bundle"
        for provider, tool, _arguments in fixture.router.calls
    )
    assert all(
        tool not in {"execute_plan", "schedule_plan"}
        for _provider, tool, _arguments in fixture.router.calls
    )
