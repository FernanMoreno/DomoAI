from __future__ import annotations

import time

import pytest

from domoai.skills.workflow import EnergySkillWorkflow, WorkflowStatus
from tests.fixtures.skill_workflow import build_workflow_fixture, light_device_id
from tests.integration.test_energy_skill_workflow import request_for


@pytest.mark.asyncio
async def test_reference_energy_workflow_completes_under_two_seconds() -> None:
    fixture = await build_workflow_fixture()
    workflow = EnergySkillWorkflow(fixture.router, fixture.approval)
    request = request_for(fixture, solver_time_limit_seconds=1.0)

    started = time.perf_counter()
    result = await workflow.run(request)
    elapsed = time.perf_counter() - started

    assert result.status is WorkflowStatus.COMPLETED
    assert elapsed < 2.0, f"reference workflow took {elapsed:.3f}s"
    assert fixture.router.calls[-1][1] == "execute_plan"
    assert light_device_id(fixture) in request.devices
