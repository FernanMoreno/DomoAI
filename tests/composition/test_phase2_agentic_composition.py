import pytest

from tests.fixtures.skill_workflow import build_workflow_fixture, structured


@pytest.mark.asyncio
async def test_agentic_catalog_composes_read_only_comparison_and_safe_scene_boundary() -> None:
    fixture = await build_workflow_fixture()
    tools = {tool.name: tool for tool in await fixture.router.unified_server.list_tools()}

    assert "compare_scenarios" in tools
    assert "execute_scene" in tools
    assert tools["compare_scenarios"].annotations.readOnlyHint is True
    assert tools["compare_scenarios"].annotations.destructiveHint is False
    assert tools["execute_scene"].annotations.readOnlyHint is False
    assert tools["execute_scene"].annotations.destructiveHint is True

    device_id = next(
        device.id
        for device in fixture.domotics_context.registry.devices
        if device.type.value == "light"
    )
    comparison = structured(
        await fixture.router.unified_server.call_tool(
            "compare_scenarios",
            {
                "baseline": {
                    "id": "phase2-composition-scenario",
                    "horizon": fixture.horizon.model_dump(mode="json"),
                    "loads": [
                        {
                            "id": "phase2-composition-load",
                            "device_id": device_id,
                            "capability": "brightness",
                            "command": "set_brightness",
                            "value": 50,
                            "unit": "%",
                            "power": 100,
                        }
                    ],
                },
                "variations": {},
            },
        )
    )

    assert comparison["schema_version"] == "v1"
    assert fixture.domotics_adapter.calls == []
