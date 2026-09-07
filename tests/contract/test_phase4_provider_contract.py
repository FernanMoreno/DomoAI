from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.sdk import AdapterRegistration, ConformanceHarness
from tests.fixtures.phase4 import phase4_fixture_manifest


class OverstatedBrightnessAdapter(SimulatedHomeAdapter):
    async def discover(self):
        snapshot = await super().discover()
        brightness = next(
            capability
            for entity in snapshot.source_entities
            for capability in entity["capabilities"]
            if capability["name"] == "brightness"
        )
        brightness["maximum"] = 120
        return snapshot


async def test_conformance_harness_includes_manifest_guarantees() -> None:
    result = await ConformanceHarness(
        AdapterRegistration(
            manifest=phase4_fixture_manifest(), factory=SimulatedHomeAdapter
        )
    ).run()

    assert result.status == "passed"
    assert "manifest_compatibility" in {check.check_id for check in result.checks}


async def test_conformance_rejects_observed_guarantee_overstatement() -> None:
    result = await ConformanceHarness(
        AdapterRegistration(
            manifest=phase4_fixture_manifest(), factory=OverstatedBrightnessAdapter
        )
    ).run()

    assert result.status == "failed"
    assert any(
        diagnostic.code == "capability_guarantee_mismatch"
        for diagnostic in result.diagnostics
    )
    assert all("120" not in diagnostic.message for diagnostic in result.diagnostics)
