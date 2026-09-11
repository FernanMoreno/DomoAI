from __future__ import annotations

from domoai.application.connector_qualification import ConnectorQualificationService
from domoai.domain.connector_recipes import CapabilityBinding, ConnectorRecipe


def test_matter_qualification_includes_commissioning_gate_without_claiming_it_passed() -> None:
    recipe = ConnectorRecipe(
        connector_id="matter_fixture",
        display_name="Matter fixture",
        transport_family="matter",
        capabilities=(
            CapabilityBinding(
                capability_id="power", kind="boolean", read_path="matter://power"
            ),
        ),
        protocol_profile={
            "transport": "matter",
            "endpoints": [
                {
                    "endpoint_id": 1,
                    "capabilities": [
                        {"name": "power", "cluster_id": 6, "attribute_id": 0}
                    ],
                }
            ]
        },
    )
    report = ConnectorQualificationService().report_for_recipe(recipe)
    gate = next(gate for gate in report.external_gates if gate.gate_id == "matter_commissioning")
    assert gate.status == "pending"
    assert report.software_status == "pass"
