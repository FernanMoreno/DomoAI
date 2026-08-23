from domoai.config.policy_loader import choose_policy, load_policies
from domoai.domain.models import (
    Capability,
    CapabilityKind,
    Command,
    Device,
    DeviceType,
    PolicyAction,
    RiskClass,
    SourceRef,
)
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.risk_classifier import RiskClassifier, RiskOverride


def test_policy_loader_orders_higher_priority_first() -> None:
    policies = load_policies(
        {
            "policies": [
                {"id": "fallback", "target": {}, "action": "deny", "priority": 1},
                {
                    "id": "living-room",
                    "target": {"area_id": "living_room"},
                    "action": "allow",
                    "priority": 10,
                },
            ]
        }
    )

    assert [policy.id for policy in policies] == ["living-room", "fallback"]


def test_policy_choice_uses_the_first_matching_rule() -> None:
    policies = load_policies(
        {
            "policies": [
                {
                    "id": "sensitive",
                    "target": {"device_id": "front_door.lock"},
                    "action": "confirm",
                    "priority": 100,
                },
                {"id": "default", "target": {}, "action": "allow", "priority": 1},
            ]
        }
    )

    decision = choose_policy(policies, device_id="front_door.lock", area_id=None)

    assert decision.policy_id == "sensitive"
    assert decision.action is PolicyAction.CONFIRM


def test_policy_choice_can_match_command_within_one_capability() -> None:
    policies = load_policies(
        {
            "policies": [
                {
                    "id": "garage-close-allow",
                    "target": {"device_id": "cover.garage_main", "command": "close"},
                    "action": "allow",
                    "priority": 100,
                },
                {
                    "id": "garage-open-confirm",
                    "target": {"device_id": "cover.garage_main", "command": "open"},
                    "action": "confirm",
                    "priority": 100,
                },
            ]
        }
    )

    close = choose_policy(
        policies,
        device_id="cover.garage_main",
        area_id=None,
        capability="position",
        command="close",
    )
    open_ = choose_policy(
        policies,
        device_id="cover.garage_main",
        area_id=None,
        capability="position",
        command="open",
    )

    assert close.policy_id == "garage-close-allow"
    assert close.action is PolicyAction.ALLOW
    assert open_.policy_id == "garage-open-confirm"
    assert open_.action is PolicyAction.CONFIRM


def _device(device_id: str, device_type: DeviceType) -> Device:
    return Device(
        id=device_id,
        type=device_type,
        name=device_id,
        protocol="fixture",
        source_refs=[SourceRef(adapter_id="fixture", external_id=device_id)],
        capabilities=[
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["open"],
            )
        ],
    )


def _command(*, risk_class: RiskClass = RiskClass.SAFE) -> Command:
    return Command(
        id="cmd-1",
        device_id="cover.garage_main",
        command="open",
        risk_class=risk_class,
        idempotency_key="key-1",
    )


def test_caller_supplied_safe_cannot_downgrade_classifier_restricted() -> None:
    classifier = RiskClassifier(
        overrides=(RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.RESTRICTED),)
    )
    engine = PolicyEngine([], classifier)
    device = _device("cover.garage_main", DeviceType.COVER)

    decision = engine.evaluate(_command(risk_class=RiskClass.SAFE), device, "power")

    assert decision.action is PolicyAction.CONFIRM
    assert "explicit operator confirmation" in decision.reason


def test_caller_supplied_restricted_is_kept_even_when_classifier_says_safe() -> None:
    engine = PolicyEngine([], RiskClassifier())
    device = _device("light.kitchen", DeviceType.LIGHT)

    decision = engine.evaluate(_command(risk_class=RiskClass.RESTRICTED), device, "power")

    assert decision.action is PolicyAction.CONFIRM


def test_matching_allow_policy_cannot_downgrade_effective_risk_below_confirm() -> None:
    classifier = RiskClassifier(
        overrides=(RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.RESTRICTED),)
    )
    policies = load_policies(
        {
            "policies": [
                {
                    "id": "allow-garage",
                    "target": {"device_id": "cover.garage_main"},
                    "action": "allow",
                    "priority": 100,
                }
            ]
        }
    )
    engine = PolicyEngine(policies, classifier)
    device = _device("cover.garage_main", DeviceType.COVER)

    decision = engine.evaluate(_command(risk_class=RiskClass.SAFE), device, "power")

    assert decision.action is PolicyAction.CONFIRM


def test_policy_engine_passes_command_to_policy_context() -> None:
    policies = load_policies(
        {
            "policies": [
                {
                    "id": "garage-open-deny",
                    "target": {"device_id": "cover.garage_main", "command": "open"},
                    "action": "deny",
                    "priority": 100,
                },
                {
                    "id": "garage-close-allow",
                    "target": {"device_id": "cover.garage_main", "command": "close"},
                    "action": "allow",
                    "priority": 100,
                },
            ]
        }
    )
    engine = PolicyEngine(
        policies,
        RiskClassifier(
            overrides=(RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.SAFE),)
        ),
    )
    device = _device("cover.garage_main", DeviceType.COVER)

    decision = engine.evaluate(_command(), device, "position")

    assert decision.policy_id == "garage-open-deny"
    assert decision.action is PolicyAction.DENY
