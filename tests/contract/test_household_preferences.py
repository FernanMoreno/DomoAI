"""The preference wire surface rejects unsafe input before persistence."""

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from scripts.export_schemas import MODELS

from tests.fixtures.household_preferences import mutation, preference


def model(name="household-preference-mutation"):
    result = MODELS.get(name)
    assert result is not None, "the versioned preference contract is not exported"
    return result


def test_typed_preference_round_trip_and_schema():
    dto = model().model_validate(mutation())
    wire = json.loads(dto.model_dump_json())
    jsonschema.validate(wire, model().model_json_schema())
    assert model().model_validate_json(dto.model_dump_json()) == dto
    assert dto.preference.value is False


@pytest.mark.parametrize(
    "changes",
    [
        {"actor": "owner"},
        {"value": "false"},
        {"value": 0},
        {"unit": "percent"},
        {"timezone": "Mars/Olympus"},
        {"valid_from": "2026-09-10T22:00:00"},
        {"valid_from": "2026-03-29T02:30:00+01:00"},
        {"valid_from": "2026-10-25T02:30:00+03:00"},
        {"key": "unknown"},
        {"provenance": {"source_kind": "manual", "updated_by": "owner"}},
        {"provenance": {"source_kind": "manual", "source_ref": "secret / conversation"}},
    ],
)
def test_invalid_preferences_are_rejected(changes):
    dto = model()
    with pytest.raises(ValidationError):
        dto.model_validate(mutation(preference=preference(**changes)))


@pytest.mark.parametrize(
    "key,unit,value,valid",
    [
        ("comfort.temperature_target", "degC", 21.5, True),
        ("comfort.temperature_target", "degC", "21", False),
        ("comfort.temperature_target", "degC", True, False),
        ("comfort.temperature_target", "degC", float("nan"), False),
        ("comfort.temperature_target", "degC", float("inf"), False),
        ("comfort.temperature_target", "degC", 36, False),
        ("energy.minimum_soc", "percent", 0, True),
        ("energy.minimum_soc", "percent", 25, True),
        ("energy.minimum_soc", "percent", 100, True),
        ("energy.minimum_soc", "percent", 25.0, False),
        ("energy.minimum_soc", "percent", True, False),
        ("energy.minimum_soc", "kWh", 25, False),
    ],
)
def test_units_and_strict_values(key, unit, value, valid):
    dto = model()
    payload = mutation(preference=preference(key=key, unit=unit, value=value))
    if valid:
        wire = json.loads(dto.model_validate(payload).model_dump_json())
        jsonschema.validate(wire, dto.model_json_schema())
    else:
        with pytest.raises(ValidationError):
            dto.model_validate(payload)


def test_reevaluation_cannot_claim_completion_without_consumer():
    dto = model("household-preference-result")
    schema = dto.model_json_schema()
    assert set(schema["properties"]["task_reevaluation"]["enum"]) == {
        "consumer_unavailable",
        "pending",
    }


@pytest.mark.parametrize(
    "value,valid",
    [
        ({"weekdays": [1, 3, 7], "start": "22:00", "end": "07:00", "overnight": True}, True),
        ({"weekdays": [1, 1], "start": "22:00", "end": "07:00", "overnight": True}, False),
        ({"weekdays": [True], "start": "22:00", "end": "07:00", "overnight": True}, False),
        ({"weekdays": [1], "start": "24:00", "end": "07:00", "overnight": True}, False),
        ({"weekdays": [1], "start": "22:00", "end": "07:00", "overnight": False}, False),
    ],
)
def test_schedule_window_requires_coherent_explicit_values(value, valid):
    payload = mutation(
        preference=preference(key="schedule.preferred_run_window", value=value, unit="local_time")
    )
    if valid:
        assert model().model_validate(payload).preference.value.overnight is True
    else:
        with pytest.raises(ValidationError):
            model().model_validate(payload)


@pytest.mark.parametrize("instant", ["2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00"])
def test_dst_fold_is_resolved_by_explicit_offset(instant):
    dto = model().model_validate(
        mutation(preference=preference(valid_from=instant, valid_until=None))
    )
    assert dto.preference.valid_from.utcoffset().total_seconds() == 0


def test_non_temperature_preference_rejects_area_target():
    with pytest.raises(ValidationError):
        model().model_validate(
            mutation(preference=preference(target={"kind": "area", "id": "kitchen"}))
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"target": {"kind": "device"}},
        {"target": {"kind": "device", "id": "water-heater", "alias": "heater"}},
        {
            "key": "schedule.preferred_run_window",
            "unit": "local_time",
            "value": {
                "weekdays": [1, 1],
                "start": "22:00",
                "end": "07:00",
                "overnight": True,
            },
        },
    ],
)
def test_exported_schema_rejects_same_identity_and_weekday_errors_as_dto(changes):
    payload = mutation(preference=preference(**changes))
    with pytest.raises(ValidationError):
        model().model_validate(payload)
    schema = json.loads(Path("schemas/v1/household-preference-mutation.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
