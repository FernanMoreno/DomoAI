from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.domain.digital_twin import DigitalTwinEvidence


def _payload() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "run_id": "twin-contract-1",
        "scope": "digital_twin",
        "seed": 187,
        "plant_digest": "a" * 64,
        "trace_digest": "b" * 64,
        "coverage": {
            "adapters": ["fixture"],
            "domains": ["light"],
            "checks": ["discover"],
        },
        "checks": [{"check_id": "fixture.light.discover", "status": "passed"}],
        "invariant_violations": [],
    }


def test_v1_evidence_round_trips_without_hardware_claim() -> None:
    evidence = DigitalTwinEvidence.model_validate(_payload())
    dumped = evidence.model_dump(mode="json")

    assert dumped["schema_version"] == "v1"
    assert dumped["scope"] == "digital_twin"
    assert dumped["status"] == "passed"
    assert dumped["invariant_violations"] == []


def test_v1_evidence_rejects_unknown_fields_and_invalid_digest() -> None:
    unknown = _payload() | {"hardware_qualification": True}
    with pytest.raises(ValidationError):
        DigitalTwinEvidence.model_validate(unknown)

    invalid = _payload() | {"plant_digest": "not-a-digest"}
    with pytest.raises(ValidationError):
        DigitalTwinEvidence.model_validate(invalid)


def test_v1_digital_twin_evidence_schema_is_exported() -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "v1" / "digital-twin-evidence.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["scope"]["const"] == "digital_twin"
    assert schema["properties"]["schema_version"]["const"] == "v1"
