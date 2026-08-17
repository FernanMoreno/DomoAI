import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, PlanStatus, Policy, PolicyAction, RiskClass
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_duplicate_idempotency_keys_make_plan_invalid() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-duplicate-1",
        commands=[
            Command(
                id="command-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="same-intent",
            ),
            Command(
                id="command-2",
                device_id=device_id,
                command="turn_off",
                idempotency_key="same-intent",
            ),
        ],
    )

    validated = PlanService(registry, state_store, PolicyEngine([]), AuditLog()).validate(plan)

    assert validated.status is PlanStatus.VALIDATED
    assert validated.validation is not None
    assert any(error.code == "duplicate_command" for error in validated.validation.errors)


@pytest.mark.asyncio
async def test_policy_conditions_select_deny_and_risk_confirmation() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    policies = [
        Policy(
            id="deny-brightness-over-80",
            target={"capability": "brightness"},
            conditions={"value_min": 81},
            action=PolicyAction.DENY,
            priority=10,
        ),
        Policy(
            id="confirm-switch",
            target={"device_id": switch_id},
            action=PolicyAction.CONFIRM,
            priority=10,
        ),
    ]
    service = PlanService(registry, state_store, PolicyEngine(policies), AuditLog())

    denied = service.validate(
        Plan(
            id="plan-policy-denied",
            commands=[
                Command(
                    id="command-policy-denied",
                    device_id=light_id,
                    command="set_brightness",
                    value=90,
                    idempotency_key="intent-policy-denied",
                )
            ],
        )
    )
    confirmed = service.validate(
        Plan(
            id="plan-policy-confirmed",
            commands=[
                Command(
                    id="command-policy-confirmed",
                    device_id=switch_id,
                    command="turn_on",
                    risk_class=RiskClass.SAFE,
                    idempotency_key="intent-policy-confirmed",
                )
            ],
        )
    )

    assert denied.status is PlanStatus.VALIDATED
    assert denied.policy_decisions[0].action is PolicyAction.DENY
    assert confirmed.status is PlanStatus.REQUIRES_CONFIRMATION
    assert confirmed.policy_decisions[0].action is PolicyAction.CONFIRM
