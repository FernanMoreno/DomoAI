import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from domoai.domain.automation import AutomationEvent
from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
from domoai.domain.models import Plan
from tests.fixtures.runtime_schema_compatibility import FIXTURES, NOW, historical_readers, plan


def assert_legacy(family: str, model: object) -> None:
    payload = model.model_dump(mode="json")
    Draft202012Validator(json.loads((FIXTURES / f"{family}.schema.json").read_text())).validate(
        payload
    )
    assert historical_readers()[family].model_validate(payload).model_dump(mode="json") == payload


def test_absent_plan_metadata_retains_exact_legacy_reader_shape() -> None:
    assert_legacy("plan", plan())


def test_scoped_plan_selects_version_before_serialization() -> None:
    current = plan(deployment_id="deployment")
    assert current.schema_version == "v2"
    with pytest.raises(ValidationError):
        historical_readers()["plan"].model_validate(current.model_dump(mode="json"))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "v1", "deployment_id": "deployment"},
        {"schema_version": "v2"},
        {"schema_version": "future"},
    ],
)
def test_explicit_plan_shape_version_conflicts_are_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError, match="version"):
        plan(**changes)


def test_mutated_plan_cannot_cross_canonical_reader() -> None:
    mutated = plan().model_copy(update={"deployment_id": "deployment"})
    with pytest.raises(ValidationError, match="version"):
        Plan.model_validate(mutated.model_dump(mode="python"))


def test_absent_state_target_retains_exact_legacy_reader_shape() -> None:
    assert_legacy(
        "automation-event",
        AutomationEvent(
            event_id="event",
            event_type="state_changed",
            occurred_at=NOW,
            device_id="device",
            capability="power",
        ),
    )


def test_untargeted_historical_time_is_not_an_active_event() -> None:
    old = historical_readers()["automation-event"](
        event_id="event",
        event_type="time",
        occurred_at=NOW,
    )
    with pytest.raises(ValidationError, match="target_rule_id"):
        AutomationEvent.model_validate(old.model_dump(mode="json"))


def test_absent_physical_identity_retains_exact_legacy_reader_shape() -> None:
    assert_legacy(
        "physical-intent",
        PhysicalIntent(
            tenant_id="tenant",
            household_id="home",
            deployment_id="deployment",
            idempotency_key="intent",
            plan_id="plan",
            command_id="cmd",
            fencing_epoch=1,
            created_at=NOW,
            updated_at=NOW,
        ),
    )


def test_dispatching_identity_is_never_stripped() -> None:
    current = PhysicalIntent(
        tenant_id="tenant",
        household_id="home",
        deployment_id="deployment",
        idempotency_key="intent",
        plan_id="plan",
        command_id="cmd",
        fencing_epoch=1,
        status=PhysicalIntentStatus.DISPATCHING,
        owner_id="owner",
        execution_attempt_id="attempt",
        lease_id="lease",
        created_at=NOW,
        updated_at=NOW,
    )
    payload = current.model_dump(mode="json")
    assert payload["execution_attempt_id"] == "attempt"
    with pytest.raises(ValidationError):
        historical_readers()["physical-intent"].model_validate(payload)


def rule(template: Plan, **changes: object):
    from domoai.domain.automation import AutomationRule

    values = {
        "id": "rule",
        "name": "Rule",
        "scope": "home",
        "trigger": {"type": "state_changed", "device_id": "device", "capability": "power"},
        "plan_template": template,
    }
    return AutomationRule.model_validate(values | changes)


def test_rule_version_precedes_digest_and_keeps_legacy_digest() -> None:
    from domoai.domain.automation import automation_rule_digest

    assert_legacy("automation-rule", rule(plan()))
    current = rule(plan(deployment_id="deployment"))
    assert current.schema_version == "v2"
    assert current.definition_digest == automation_rule_digest(current)
    with pytest.raises(ValidationError):
        historical_readers()["automation-rule"].model_validate(current.model_dump(mode="json"))
    with pytest.raises(ValidationError, match="version"):
        rule(current.plan_template, schema_version="v1")
    with pytest.raises(ValidationError, match="version"):
        rule(plan(), schema_version="v2")


@pytest.mark.parametrize(
    "singular,versions,expected",
    [
        (None, [], "v1"),
        ("v1", [], "v1"),
        (None, ["v1"], "v1"),
        ("v2", [], "v2"),
        (None, ["v2"], "v2"),
        ("v1", ["v1", "v2"], "v2"),
    ],
)
def test_result_selects_version_across_both_plan_fields(singular, versions, expected) -> None:
    from domoai.optimizer.ports import OptimizationResult

    def make(version):
        return plan(**({"deployment_id": "deployment"} if version == "v2" else {}))

    values = {
        "scenario_id": "scenario",
        "solver": "fixture",
        "status": "feasible",
        "plan": make(singular) if singular else None,
        "plans": [make(version) for version in versions],
    }
    result = OptimizationResult.model_validate(values)
    assert result.schema_version == expected
    if expected == "v1":
        assert_legacy("optimization-result", result)
    else:
        with pytest.raises(ValidationError):
            historical_readers()["optimization-result"].model_validate(
                result.model_dump(mode="json")
            )
    with pytest.raises(ValidationError, match="version"):
        OptimizationResult.model_validate(
            values | {"schema_version": "v1" if expected == "v2" else "v2"}
        )


def test_selected_new_schemas_validate_payloads_and_reject_inconsistent_shapes() -> None:
    from pathlib import Path

    from domoai.optimizer.ports import OptimizationResult

    root = Path(__file__).resolve().parents[2]
    current = plan(deployment_id="deployment")
    result = OptimizationResult(
        scenario_id="scenario",
        solver="fixture",
        status="feasible",
        plan=current,
        plans=[plan(), current],
    )
    examples = {
        "plan": current,
        "automation-rule": rule(current),
        "optimization-result": result,
        "automation-event": AutomationEvent(
            event_id="time", event_type="time", occurred_at=NOW, target_rule_id="rule"
        ),
        "physical-intent": PhysicalIntent(
            tenant_id="tenant",
            household_id="home",
            deployment_id="deployment",
            idempotency_key="intent",
            plan_id="plan",
            command_id="cmd",
            fencing_epoch=1,
            status="dispatching",
            owner_id="owner",
            execution_attempt_id="attempt",
            created_at=NOW,
            updated_at=NOW,
        ),
    }
    for family, model in examples.items():
        schema = json.loads((root / "schemas/v2" / f"{family}.schema.json").read_text())
        validator = Draft202012Validator(schema)
        payload = model.model_dump(mode="json")
        validator.validate(payload)
        bad = dict(payload)
        if family in {"plan", "automation-rule", "optimization-result"}:
            bad["schema_version"] = "v1"
        elif family == "automation-event":
            bad.pop("target_rule_id")
        else:
            bad.pop("owner_id")
        assert list(validator.iter_errors(bad)), family
    schema = json.loads((root / "schemas/v2/optimization-result.schema.json").read_text())
    assert list(
        Draft202012Validator(schema).iter_errors(
            result.model_dump(mode="json") | {"plan": None, "plans": []}
        )
    )
