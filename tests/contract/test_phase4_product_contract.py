import json
from datetime import UTC, datetime
from pathlib import Path

from domoai.adapters.sdk import AdapterManifest
from domoai.domain.commissioning import CommissioningQualification
from domoai.domain.models import AuthorityContext
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.domain.product import ProductSummary

SCHEMA_DIR = Path(__file__).parents[2] / "schemas" / "v1"


def test_phase4_public_schemas_are_exported_and_versioned() -> None:
    for name in (
        "commissioning-evidence",
        "commissioning-qualification",
        "commissioning-report",
        "household-data-policy",
        "privacy-export",
        "privacy-deletion",
        "product-summary",
    ):
        payload = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert payload["properties"]["schema_version"]["const"] == "v1"

    for name in ("capability", "commissioning-check"):
        payload = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert payload["type"] == "object"
        assert payload["additionalProperties"] is False


def test_provider_manifest_carries_the_universal_guarantee_contract() -> None:
    manifest = AdapterManifest(
        adapter_id="fixture",
        name="Fixture",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="1.0.0",
        device_types=["light"],
        capabilities=[
            {
                "name": "brightness",
                "kind": "integer",
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
                "guarantees": {
                    "resolution": 1,
                    "readback_required": True,
                },
            }
        ],
    )

    assert manifest.capabilities[0].guarantees.readback_required is True


def test_phase4_contract_models_are_strict_and_secret_free() -> None:
    authority = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="owner-a",
        roles=["owner"],
    )
    qualification = CommissioningQualification(
        authority=authority,
        candidate_digest="a" * 64,
        evidence_digest="sha256:" + "b" * 64,
        status="blocked_external_dependency",
        blockers=["physical_evidence_required"],
        checked_at=datetime.now(UTC),
    )
    policy = HouseholdDataPolicy(
        authority=authority,
        exportable_categories=[PrivacyCategory.PLANS],
        deletable_categories=[PrivacyCategory.PLANS],
        retention_days=90,
    )
    summary = ProductSummary(
        scenario_id="scenario-1",
        status="feasible",
        headline="Proposal",
        next_step="validate_and_request_approval_before_execution",
    )

    serialized = json.dumps(
        {
            "qualification": qualification.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    assert "token" not in serialized.casefold()
    assert "secret" not in serialized.casefold()
