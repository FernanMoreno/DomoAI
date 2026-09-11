from __future__ import annotations

from domoai.application.connector_qualification import ConnectorQualificationService
from domoai.domain.connector_recipes import CapabilityBinding, ConnectorRecipe


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe(
        connector_id="fixture",
        display_name="Fixture",
        transport_family="mqtt",
        capabilities=(
            CapabilityBinding(
                capability_id="temperature", kind="number", read_path="/state"
            ),
        ),
    )


def test_qualification_report_separates_software_from_external_gates() -> None:
    report = ConnectorQualificationService().report_for_recipe(_recipe())
    assert report.software_status == "pass"
    assert report.secret_safe is True
    assert any(
        gate.gate_id == "real_device" and gate.status == "pending"
        for gate in report.external_gates
    )
    assert report.evidence_digest.startswith("sha256:")


def test_qualification_report_is_reproducible_and_secret_free() -> None:
    report = ConnectorQualificationService().report_for_recipe(_recipe())
    serialized = report.model_dump_json()
    assert "client_secret" not in serialized
    assert "access_token" not in serialized
    assert report.evidence_digest == report.model_copy(update={"evidence_digest": ""}).digest()
