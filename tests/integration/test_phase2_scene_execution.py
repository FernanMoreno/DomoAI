from typing import Any

import pytest

from domoai.application.bundle_commit import BundleCommitRequestMember, bundle_approval_digest
from domoai.application.scene import scene_commit_digest
from tests.fixtures.skill_workflow import build_workflow_fixture, structured


@pytest.mark.asyncio
async def test_execute_scene_delegates_to_bundle_commit_and_is_idempotent() -> None:
    fixture = await build_workflow_fixture()
    device_id = next(
        device.id
        for device in fixture.domotics_context.registry.devices
        if device.type.value == "light"
    )
    command = {
        "id": "phase2-scene-command",
        "device_id": device_id,
        "command": "turn_on",
        "idempotency_key": "phase2-scene-idempotency",
    }
    prepared = structured(
        await fixture.router.unified_server.call_tool(
            "prepare_plan",
            {"plan": {"id": "phase2-scene-plan", "commands": [command]}},
        )
    )
    member = BundleCommitRequestMember(
        plan_id="phase2-scene-plan",
        validation_digest=prepared["validation"]["digest"],
    )
    scenario_id = "phase2-scene"
    bundle_digest = bundle_approval_digest(scenario_id, [member])
    runtime_revision = fixture.domotics_context.facade.plan_service.current_revision
    digest = scene_commit_digest(
        scene_id="night-scene",
        scenario_id=scenario_id,
        runtime_revision=runtime_revision,
        bundle_digest=bundle_digest,
        members=[member],
    )
    arguments: dict[str, Any] = {
        "scene_id": "night-scene",
        "scene_digest": digest,
        "scenario_id": scenario_id,
        "runtime_revision": runtime_revision,
        "bundle_digest": bundle_digest,
        "members": [member.model_dump(mode="json")],
    }

    first = structured(
        await fixture.router.unified_server.call_tool("execute_scene", arguments)
    )
    second = structured(
        await fixture.router.unified_server.call_tool("execute_scene", arguments)
    )

    assert first["status"] == "completed"
    assert first["scene_digest"] == digest
    assert second["bundle_commit_id"] == first["bundle_commit_id"]
    assert first["members"][0]["execution_status"] == "confirmed_success"
    assert [call.id for call in fixture.domotics_adapter.calls] == ["phase2-scene-command"]


@pytest.mark.asyncio
async def test_execute_scene_rejects_stale_revision_before_physical_write() -> None:
    fixture = await build_workflow_fixture()
    result = structured(
        await fixture.router.unified_server.call_tool(
            "execute_scene",
            {
                "scene_id": "stale-scene",
                "scene_digest": "sha256:invalid",
                "scenario_id": "stale-scenario",
                "runtime_revision": "stale-runtime-revision",
                "bundle_digest": "sha256:invalid",
                "members": [],
            },
        )
    )

    assert result["error"]["code"] in {"validation_error", "stale_plan"}
    assert fixture.domotics_adapter.calls == []
