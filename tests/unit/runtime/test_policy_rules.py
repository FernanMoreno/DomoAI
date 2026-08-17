from domoai.config.policy_loader import choose_policy, load_policies
from domoai.domain.models import PolicyAction


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
