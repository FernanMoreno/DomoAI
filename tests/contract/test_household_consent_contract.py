"""T005 RED: proposed public review contracts, not consent issuance or execution.

The exporter is an existing public boundary. Missing registrations/projection
fail at test runtime, so collection errors cannot masquerade as contract RED.
Payloads below specify the proposed T006 wire shape; no substitute DTO is used.
"""

import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from pydantic import ValidationError
from scripts.export_schemas import MODELS

from domoai.domain.automation import AutomationRule
from domoai.domain.household_preferences import PreferenceProfile, PreferenceVersion
from domoai.domain.models import AuthorityContext, Device, Plan

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
END = datetime(2026, 9, 13, 12, 4, tzinfo=UTC)
PREFERENCE_GENERATION = "00000000-0000-4000-8000-000000000002"
CONTROL_GENERATION = "00000000-0000-4000-8000-000000000004"
DIGEST = "sha256:" + "1" * 64
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "v1"


def exported(suffix):
    key = f"household-consent-{suffix}"
    model = MODELS.get(key)
    assert model is not None, f"P04 public contract missing from schema exporter: {key}"
    return model


def projector():
    module_name = "domoai.application.household_consent"
    assert importlib.util.find_spec(module_name) is not None, (
        f"P04 public review projection missing: {module_name}.build_consent_view"
    )
    module = importlib.import_module(module_name)
    projection = getattr(module, "build_consent_view", None)
    assert callable(projection), (
        f"P04 public review projection missing: {module_name}.build_consent_view"
    )
    return projection


def review_payload():
    return {
        "schema_version": "domoai.household-consent.v1",
        "reference": {"kind": "plan", "id": "p04-plan"},
        "tenant_id": "p04-tenant",
        "household_id": "p04-home-a",
        "actions": [
            {
                "command_id": "set-target",
                "device_id": "heater-a",
                "device_name": "Calefacción salón",
                "command": "climate.set_temperature",
                "value": 21,
                "unit": "degC",
                "risk_class": "confirm",
            }
        ],
        "window": {
            "not_before": "2026-09-13T12:00:00Z",
            "not_after": "2026-09-13T12:04:00Z",
            "timezone": "Europe/Madrid",
        },
        "duration_seconds": 240,
        "expires_at": "2026-09-13T12:04:00Z",
        "constraints": None,
        "risks": [
            {
                "policy_id": "comfort-confirm",
                "action": "confirm",
                "reason": "El cambio de temperatura requiere confirmación del residente",
                "allows_stale": False,
            }
        ],
        "evidence_refs": [{"kind": "validation", "id": "p04-plan", "digest": DIGEST}],
        "bindings": {
            "validation_digest": DIGEST,
            "window_digest": "sha256:" + "2" * 64,
            "bundle_digest": None,
            "recurrence_digest": None,
            "rule_digest": None,
            "schedule_revision": 2,
            "preference_version": {"generation": PREFERENCE_GENERATION, "revision": 7},
            "preference_observed_at": "2026-09-13T12:00:00Z",
            "preference_next_transition_at": None,
            "preference_horizon": {
                "start": "2026-09-13T12:00:00Z",
                "end": "2026-09-13T12:04:00Z",
            },
            "control_version": {"generation": CONTROL_GENERATION, "revision": 3},
        },
        "permission_status": "awaiting_review",
        "physical_effect_refs": [],
    }


def projection_inputs():
    authority = AuthorityContext(
        tenant_id="p04-tenant",
        household_id="p04-home-a",
        household_ids=["p04-home-a"],
        principal_id="planner-a",
        roles=["planner"],
    )
    plan = Plan.model_validate(
        {
            "id": "p04-plan",
            "authority": authority,
            "created_at": NOW,
            "expires_at": END,
            "execute_at": NOW,
            "execution_window": {
                "intended_at": NOW,
                "not_before": NOW,
                "not_after": END,
                "timezone": "Europe/Madrid",
                "revision": 2,
            },
            "schedule_revision": 2,
            "commands": [
                {
                    "id": "set-target",
                    "device_id": "heater-a",
                    "command": "climate.set_temperature",
                    "value": 21,
                    "unit": "degC",
                    "risk_class": "confirm",
                    "idempotency_key": "p04-command-once",
                }
            ],
            "status": "requires_confirmation",
            "policy_decisions": [
                {
                    "policy_id": "comfort-confirm",
                    "action": "confirm",
                    "reason": "El cambio de temperatura requiere confirmación del residente",
                    "allows_stale": False,
                }
            ],
            "validation": {
                "status": "requires_confirmation",
                "validated_at": NOW,
                "runtime_revision": "fixture-v1",
                "digest": DIGEST,
                "valid_until": END,
            },
        }
    )
    device = Device.model_validate(
        {
            "id": "heater-a",
            "name": "Calefacción salón",
            "type": "climate",
            "protocol": "fixture",
            "source_refs": [{"adapter_id": "fixture", "external_id": "a"}],
            "capabilities": [
                {
                    "name": "climate.target_temperature",
                    "kind": "number",
                    "unit": "degC",
                    "readable": True,
                    "writable": True,
                    "minimum": 16,
                    "maximum": 30,
                    "commands": ["climate.set_temperature"],
                }
            ],
        }
    )
    preferences = PreferenceProfile(
        version=PreferenceVersion(generation=PREFERENCE_GENERATION, revision=7),
        observed_at=NOW,
        next_transition_at=None,
        preferences=[],
        next_cursor=None,
    )
    return {
        "target": plan,
        "devices": [device],
        "preferences": preferences,
        "control_version": {"generation": CONTROL_GENERATION, "revision": 3},
        "now": NOW,
        "timezone": "Europe/Madrid",
    }


def wire_schema(suffix, model):
    path = SCHEMAS / f"household-consent-{suffix}.schema.json"
    assert path.is_file(), f"P04 public schema has not been exported: {path.name}"
    schema = json.loads(path.read_text())
    assert schema == model.model_json_schema(by_alias=True)
    return schema


def assert_shape_rejected(model, schema, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_review_projection_shows_exact_plan_scope_without_mutating_authority():
    """Catches lost risks/evidence or replaced target, unit, timing and generation provenance."""
    inputs = projection_inputs()
    before = inputs["target"].model_dump_json()
    view = projector()(**inputs)
    wire = view.model_dump(mode="json")
    assert wire["reference"] == {"kind": "plan", "id": "p04-plan"}
    assert (wire["tenant_id"], wire["household_id"]) == ("p04-tenant", "p04-home-a")
    assert wire["actions"] == review_payload()["actions"]
    assert wire["risks"] == [
        {
            "policy_id": "comfort-confirm",
            "action": "confirm",
            "reason": "El cambio de temperatura requiere confirmación del residente",
            "allows_stale": False,
        }
    ]
    assert wire["evidence_refs"] == [
        {
            "kind": "validation",
            "id": "p04-plan",
            "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        }
    ]
    assert view.window.not_before == NOW and view.window.not_after == END
    assert view.window.timezone == "Europe/Madrid"
    assert view.duration_seconds == 240 and view.expires_at == END
    assert view.bindings.validation_digest == DIGEST
    assert view.bindings.window_digest == inputs["target"].execution_window.digest
    assert view.bindings.schedule_revision == 2
    assert wire["bindings"]["preference_version"] == {
        "generation": "00000000-0000-4000-8000-000000000002",
        "revision": 7,
    }
    assert view.bindings.preference_observed_at == NOW
    assert view.bindings.preference_next_transition_at is None
    assert view.bindings.preference_horizon.start == NOW
    assert view.bindings.preference_horizon.end == END
    assert wire["bindings"]["control_version"] == {
        "generation": "00000000-0000-4000-8000-000000000004",
        "revision": 3,
    }
    assert view.permission_status == "awaiting_review"
    assert view.physical_effect_refs == []
    assert inputs["target"].model_dump_json() == before


def test_ambiguous_device_alias_cannot_produce_approvable_review():
    """Catches choosing the first equal-name device instead of requiring clarification."""
    inputs = projection_inputs()
    device = inputs["devices"][0]
    inputs["devices"].append(device.model_copy(update={"id": "heater-b"}))
    payload = inputs["target"].model_dump()
    payload["commands"][0]["device_id"] = "Calefacción salón"
    inputs["target"] = Plan.model_validate(payload)
    build = projector()
    with pytest.raises(ValueError, match="clarification_required"):
        build(**inputs)


@pytest.mark.parametrize("unit,value", [(None, 21), ("kWh", 21), ("degC", "21"), ("degC", True)])
def test_review_rejects_unresolved_or_coerced_command_units(unit, value):
    """Catches treating missing/mismatched units or nonnumeric values as a valid target."""
    inputs = projection_inputs()
    payload = inputs["target"].model_dump()
    payload["commands"][0].update(unit=unit, value=value)
    inputs["target"] = Plan.model_validate(payload)
    build = projector()
    with pytest.raises(ValueError, match="clarification_required"):
        build(**inputs)


@pytest.mark.parametrize("case", ["unspecified-window", "unknown-timezone"])
def test_review_requires_concrete_explainable_window(case):
    """Catches approving 'tonight' without an interval or an unresolvable local zone."""
    inputs = projection_inputs()
    payload = inputs["target"].model_dump()
    if case == "unspecified-window":
        payload.update(execute_at=None, execution_window=None)
        payload["commands"][0]["intent"] = "esta noche"
    else:
        payload["execution_window"]["timezone"] = "Mars/Olympus"
        inputs["timezone"] = "Mars/Olympus"
    inputs["target"] = Plan.model_validate(payload)
    build = projector()
    with pytest.raises(ValueError, match="clarification_required"):
        build(**inputs)


def rule_inputs():
    inputs = projection_inputs()
    template = inputs["target"].model_dump()
    template.update(status="draft", validation=None, execute_at=None, execution_window=None)
    template["commands"] = [
        template["commands"][0] | {"id": f"command-{n}", "idempotency_key": f"key-{n}"}
        for n in range(10)
    ]
    inputs["target"] = AutomationRule.model_validate(
        {
            "id": "p04-rule",
            "name": "Mantener confort",
            "authority": template["authority"],
            "scope": "salon-climate",
            "plan_template": template,
            "trigger": {
                "type": "state_changed",
                "device_id": "heater-a",
                "capability": "climate.target_temperature",
                "expected": 19,
            },
            "conditions": [
                {
                    "device_id": "heater-a",
                    "capability": "climate.target_temperature",
                    "operator": "not_equals",
                    "expected": value,
                }
                for value in (16, 17, 18, 20)
            ],
            "cooldown_seconds": 86400,
            "expires_at": "2026-09-20T12:00:00Z",
        }
    )
    return inputs


def test_rule_review_retains_existing_autonomy_bounds_and_definition():
    """Catches lost/changed action content, constraints, expiry or rule definition binding."""
    inputs = rule_inputs()
    rule = inputs["target"]
    view = projector()(**inputs)
    assert view.reference.kind == "rule" and view.reference.id == "p04-rule"
    assert [action.model_dump(mode="json") for action in view.actions] == [
        {
            "command_id": f"command-{n}",
            "device_id": "heater-a",
            "device_name": "Calefacción salón",
            "command": "climate.set_temperature",
            "value": 21,
            "unit": "degC",
            "risk_class": "confirm",
        }
        for n in range(10)
    ]
    assert view.constraints.scope == "salon-climate"
    assert view.constraints.trigger == rule.trigger
    assert view.constraints.conditions == rule.conditions
    assert view.constraints.cooldown_seconds == 86400
    assert view.expires_at == datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert view.bindings.rule_digest == rule.definition_digest


def test_rule_without_expiry_is_not_presented_as_bounded_consent():
    """Catches using the short review/grant TTL as the recurring authorization's expiry."""
    inputs = rule_inputs()
    payload = inputs["target"].model_dump(exclude={"definition_digest"})
    payload["expires_at"] = None
    inputs["target"] = AutomationRule.model_validate(payload)
    build = projector()
    with pytest.raises(ValueError, match="clarification_required"):
        build(**inputs)


@pytest.mark.parametrize("field", list(review_payload()["bindings"]))
def test_view_and_exported_schema_require_every_review_binding(field):
    """Catches making scope/version/horizon evidence silently optional in either contract."""
    model = exported("view")
    payload = review_payload()
    dto = model.model_validate(payload)
    schema = wire_schema("view", model)
    jsonschema.validate(json.loads(dto.model_dump_json()), schema)
    del payload["bindings"][field]
    assert_shape_rejected(model, schema, payload)


@pytest.mark.parametrize(
    "kind,required",
    [
        ("plan", "validation_digest"),
        ("bundle", "bundle_digest"),
        ("recurrence", "recurrence_digest"),
        ("rule", "rule_digest"),
    ],
)
def test_review_requires_the_digest_of_its_exact_target_kind(kind, required):
    """Catches a rule/bundle/recurrence borrowing a plan digest as sufficient consent."""
    model = exported("view")
    payload = review_payload()
    payload["reference"]["kind"] = kind
    payload["bindings"][required] = DIGEST
    if kind == "rule":
        rule = rule_inputs()["target"]
        payload["constraints"] = {
            "scope": rule.scope,
            "trigger": rule.trigger.model_dump(mode="json"),
            "conditions": [item.model_dump(mode="json") for item in rule.conditions],
            "cooldown_seconds": rule.cooldown_seconds,
        }
    model.model_validate(payload)
    payload["bindings"][required] = None
    assert_shape_rejected(model, wire_schema("view", model), payload)


@pytest.mark.parametrize(
    "path,value",
    [
        (("approved_by",), "owner"),
        (("actions", 0, "credential"), "secret"),
        (("bindings", "control_version", "revision"), True),
        (("duration_seconds",), "240"),
        (("reference", "kind"), "all-households"),
    ],
)
def test_review_shape_is_strict_at_nested_public_boundaries(path, value):
    """Catches ignored extras, coercion and wildcard scope in the review wire contract."""
    model = exported("view")
    payload = review_payload()
    model.model_validate(payload)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert_shape_rejected(model, wire_schema("view", model), payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"not_before": "2026-09-13T12:00:00"},
        {"not_after": "2026-09-13T11:59:00Z"},
        {"timezone": "Mars/Olympus"},
    ],
)
def test_review_window_rejects_naive_reversed_and_nonexistent_local_instants(changes):
    """Catches representing an impossible or ambiguous interval as explicit consent."""
    model = exported("view")
    payload = review_payload()
    model.model_validate(payload)
    payload["window"].update(changes)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_review_window_does_not_silently_normalize_a_dst_gap():
    """Catches normalizing a nonexistent Madrid local time before asking for clarification."""
    model = exported("view")
    payload = review_payload()
    payload["window"].update(
        not_before="2026-03-29T03:30:00+02:00",
        not_after="2026-03-29T03:34:00+02:00",
    )
    payload["expires_at"] = "2026-03-29T01:34:00Z"
    payload["bindings"]["preference_observed_at"] = "2026-03-29T01:30:00Z"
    payload["bindings"]["preference_horizon"] = {
        "start": "2026-03-29T01:30:00Z",
        "end": "2026-03-29T01:34:00Z",
    }
    model.model_validate(payload)
    # Same absolute instant, but this civil time never occurred in Europe/Madrid.
    payload["window"]["not_before"] = "2026-03-29T02:30:00+01:00"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "case",
    [
        "eleven-actions",
        "five-conditions",
        "negative-cooldown",
        "long-cooldown",
        "unsupported-budget",
    ],
)
def test_rule_review_and_schema_do_not_advertise_unenforceable_limits(case):
    """Catches exposing unsupported autonomy or widening existing 10/4/0..86400 bounds."""
    model = exported("view")
    payload = review_payload()
    rule = rule_inputs()["target"]
    payload["reference"] = {"kind": "rule", "id": "p04-rule"}
    payload["bindings"]["rule_digest"] = rule.definition_digest
    payload["constraints"] = {
        "scope": rule.scope,
        "trigger": rule.trigger.model_dump(mode="json"),
        "conditions": [item.model_dump(mode="json") for item in rule.conditions],
        "cooldown_seconds": 86400,
    }
    payload["actions"] = [payload["actions"][0] | {"command_id": f"command-{n}"} for n in range(10)]
    model.model_validate(payload)
    schema = wire_schema("view", model)
    jsonschema.validate(payload, schema)
    if case == "eleven-actions":
        payload["actions"].append(payload["actions"][0] | {"command_id": "command-10"})
    elif case == "five-conditions":
        payload["constraints"]["conditions"].append(
            {
                "device_id": "heater-a",
                "capability": "climate.target_temperature",
                "operator": "not_equals",
                "expected": 22,
            }
        )
    elif case == "unsupported-budget":
        payload["constraints"]["maximum_energy_kwh"] = 2
    else:
        payload["constraints"]["cooldown_seconds"] = -1 if case == "negative-cooldown" else 86401
    assert_shape_rejected(model, schema, payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"approved_by": "owner"},
        {"authority": {"roles": ["owner"]}},
        {"target": {"kind": "plan", "id": "other-plan"}},
        {"assertion": {"approved_by": "owner"}},
        {"confirmation": True},
    ],
)
def test_resident_decision_cannot_supply_authority_or_replace_reviewed_content(extra):
    """Catches accepting client-supplied approval identity, target or assertion (R02)."""
    model = exported("decision")
    payload = {
        "challenge_id": "0123456789abcdef0123456789abcdef",
        "action": "approve",
        "expected_review_digest": DIGEST,
        "request_id": "resident-request-1",
    }
    dto = model.model_validate(payload)
    schema = wire_schema("decision", model)
    assert json.loads(dto.model_dump_json()) == payload
    jsonschema.validate(payload, schema)
    assert_shape_rejected(model, schema, payload | extra)


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "execute"),
        ("challenge_id", ""),
        ("expected_review_digest", "plan-1"),
        ("request_id", ""),
        pytest.param("request_id", "x" * 10000, id="oversized-request-id"),
    ],
)
def test_resident_decision_rejects_unbounded_or_unbound_input(field, value):
    """Catches unbounded retry keys, empty challenges or decisions without a summary digest."""
    model = exported("decision")
    payload = {
        "challenge_id": "0123456789abcdef0123456789abcdef",
        "action": "approve",
        "expected_review_digest": DIGEST,
        "request_id": "resident-request-1",
    }
    model.model_validate(payload)
    invalid = deepcopy(payload)
    invalid[field] = value
    assert_shape_rejected(model, wire_schema("decision", model), invalid)


def test_native_zoneinfo_gap_is_rejected_at_window_and_real_plan_projection():
    """Catches native DST-gap normalization and wall-time ordering across explicit folds."""
    from domoai.domain.household_consent import ConsentWindow

    zone = ZoneInfo("Europe/Madrid")
    gap_start = datetime(2026, 3, 29, 2, 30, tzinfo=zone)
    gap_end = datetime(2026, 3, 29, 2, 34, tzinfo=zone)
    observed = datetime(2026, 3, 29, 1, tzinfo=UTC)
    expiry = datetime(2026, 3, 29, 1, 34, tzinfo=UTC)
    inputs = projection_inputs()
    payload = inputs["target"].model_dump()
    payload.update(created_at=observed, execute_at=gap_start, expires_at=expiry)
    payload["execution_window"].update(
        intended_at=gap_start,
        not_before=gap_start,
        not_after=gap_end,
    )
    payload["validation"].update(validated_at=observed, valid_until=expiry)
    inputs["target"] = Plan.model_validate(payload)
    inputs["now"] = observed
    inputs["preferences"] = inputs["preferences"].model_copy(update={"observed_at": observed})
    rejected = []
    for boundary, build in (
        (
            "window",
            lambda: ConsentWindow(
                not_before=gap_start, not_after=gap_end, timezone="Europe/Madrid"
            ),
        ),
        ("projection", lambda: projector()(**inputs)),
    ):
        try:
            build()
        except ValueError as error:
            if boundary == "projection":
                assert "clarification_required" in str(error)
            rejected.append(boundary)
    # Exercise BOTH paths before asserting, so RED identifies both accepted gaps.
    assert rejected == ["window", "projection"]

    # A native zone whose pre-transition offset is zero is not a canonical
    # UTC input: it can still name a nonexistent local civil time.
    with pytest.raises(ValidationError):
        ConsentWindow(
            not_before=datetime(2026, 3, 29, 1, 30, tzinfo=ZoneInfo("Europe/London")),
            not_after=datetime(2026, 3, 29, 1, 34, tzinfo=ZoneInfo("Europe/London")),
            timezone="Europe/London",
        )

    for fold, utc_hour in ((0, 0), (1, 1)):
        window = ConsentWindow(
            not_before=datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=fold),
            not_after=datetime(2026, 10, 25, 2, 34, tzinfo=zone, fold=fold),
            timezone="Europe/Madrid",
        )
        assert window.not_before == datetime(2026, 10, 25, utc_hour, 30, tzinfo=UTC)
        assert window.not_after == datetime(2026, 10, 25, utc_hour, 34, tzinfo=UTC)
    crossing = ConsentWindow(
        not_before=datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0),
        not_after=datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1),
        timezone="Europe/Madrid",
    )
    assert crossing.not_before == datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    assert crossing.not_after == datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ConsentWindow(
            not_before=datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1),
            not_after=datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0),
            timezone="Europe/Madrid",
        )
    utc_window = ConsentWindow(not_before=NOW, not_after=END, timezone="Europe/Madrid")
    assert utc_window.not_before == NOW and utc_window.not_after == END


def test_rule_wire_rejects_numeric_time_without_rejecting_real_temporal_rule():
    """Catches nested legacy time/scalar coercion bypassing the strict P04 wire boundary."""
    inputs = rule_inputs()
    rule_payload = inputs["target"].model_dump(exclude={"definition_digest"})
    rule_payload["trigger"] = {
        "type": "time",
        "time_of_day": "01:00:00",
        "timezone": "Europe/Madrid",
    }
    rule = AutomationRule.model_validate(rule_payload)
    inputs["target"] = rule
    view = projector()(**inputs)
    assert view.constraints.trigger == rule.trigger
    assert view.constraints.trigger.time_of_day == time(1)
    model = exported("view")
    schema = wire_schema("view", model)
    wire = view.model_dump(mode="json")
    jsonschema.validate(wire, schema)
    assert model.model_validate(wire).constraints.trigger == rule.trigger
    invalid = deepcopy(wire)
    invalid["constraints"]["trigger"]["time_of_day"] = 3600
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    with pytest.raises(ValidationError):
        model.model_validate(invalid)

    # Same raw nested boundary: bytes are not wire strings; non-finite numbers
    # cannot be carried by JSON, but can arrive through model_validate(Python).
    for field, value in (("time_of_day", b"01:00:00"), ("timezone", b"Europe/Madrid")):
        invalid = deepcopy(wire)
        invalid["constraints"]["trigger"][field] = value
        assert_shape_rejected(model, schema, invalid)
    for expected in (float("inf"), float("nan")):
        invalid = deepcopy(wire)
        invalid["constraints"]["conditions"][0]["expected"] = expected
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
