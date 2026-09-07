from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    AdapterSnapshot,
    Capability,
    CapabilityKind,
    Command,
    Plan,
    PlanStatus,
    Policy,
    PolicyAction,
    Precondition,
    RiskClass,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_service(policies: list[Policy] | None = None) -> PlanService:
    adapter, service = await build_service_with_adapter(policies)
    return service


async def build_service_with_adapter(
    policies: list[Policy] | None = None,
) -> tuple[SimulatedHomeAdapter, PlanService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    return adapter, PlanService(registry, state_store, PolicyEngine(policies or []), AuditLog())


def _semantic_service(capability: Capability) -> PlanService:
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "fixture.semantic",
                    "device_id": "fixture-semantic-1",
                    "canonical_id": "fixture.semantic",
                    "name": "Semantic fixture",
                    "domain": "fixture",
                    "semantic_type": "sensor",
                    "capabilities": [capability.model_dump(mode="python")],
                    "identity_keys": ["fixture:semantic"],
                    "connections": ["fixture:semantic"],
                    "available": True,
                }
            ],
            source_states=[],
        ),
        "fixture",
    )
    return PlanService(registry, StateStore(), PolicyEngine([]), AuditLog())


def _semantic_command(*, value: object, unit: str | None = None) -> Command:
    return Command(
        id="semantic-command-1",
        device_id="fixture.semantic",
        command="set_value",
        value=value,
        unit=unit,
        idempotency_key="semantic-intent-1",
    )


async def _confirmation_context() -> tuple[FixedClock, PlanService, Plan]:
    initial = datetime(2026, 8, 25, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit, clock=clock).refresh()
    service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    validated = service.validate(
        Plan(
            id="approval-evidence-plan",
            commands=[
                Command(
                    id="approval-evidence-command",
                    device_id=device_id,
                    command="open",
                    risk_class=RiskClass.CONFIRM,
                    idempotency_key="approval-evidence-intent",
                )
            ],
        )
    )
    assert validated.status is PlanStatus.REQUIRES_CONFIRMATION
    grant = ApprovalStore(
        clock=clock, operator_token="operator", allow_legacy_token=True
    ).issue(validated, approved_by="operator", operator_token="operator")
    return clock, service, service.approve(validated, grant=grant)


@pytest.mark.asyncio
async def test_assert_executable_rejects_an_expired_persisted_approval() -> None:
    clock, service, approved = await _confirmation_context()
    assert approved.approval is not None
    assert approved.approval.expires_at is not None

    clock.set(approved.approval.expires_at + timedelta(seconds=1))

    with pytest.raises(DomainError) as error:
        service.assert_executable(approved)

    assert error.value.code is ErrorCode.APPROVAL_ASSERTION_EXPIRED


@pytest.mark.asyncio
async def test_assert_executable_rejects_changed_execution_window_evidence() -> None:
    _clock, service, approved = await _confirmation_context()
    assert approved.approval is not None
    tampered = approved.model_copy(
        update={
            "approval": approved.approval.model_copy(
                update={"window_digest": "sha256:tampered-window"}
            )
        }
    )

    with pytest.raises(DomainError) as error:
        service.assert_executable(tampered)

    assert error.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_legacy_approved_evidence_without_approval_id_fails_closed() -> None:
    _clock, service, approved = await _confirmation_context()
    payload = approved.model_dump(mode="python")
    payload["approval"].pop("approval_id", None)
    legacy = Plan.model_validate(payload)

    with pytest.raises(DomainError) as error:
        service.assert_executable(legacy)

    assert error.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_assert_executable_rejects_standing_approval_as_one_shot_authority() -> None:
    _clock, service, approved = await _confirmation_context()
    assert approved.approval is not None
    standing = approved.model_copy(
        update={
            "approval": approved.approval.model_copy(
                update={
                    "scope": "recurrence",
                    "recurrence_digest": "sha256:standing-rule",
                }
            )
        }
    )

    with pytest.raises(DomainError) as error:
        service.assert_executable(standing)

    assert error.value.code is ErrorCode.APPROVAL_REQUIRED


def test_validate_plan_rejects_unit_kind_bounds_step_and_writable_mismatches() -> None:
    cases = [
        (
            Capability(
                name="temperature",
                kind=CapabilityKind.NUMBER,
                unit="°C",
                readable=True,
                writable=True,
                commands=["set_value"],
            ),
            _semantic_command(value=22, unit="°F"),
            "invalid_capability",
        ),
        (
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["set_value"],
            ),
            _semantic_command(value=1),
            "invalid_command_value",
        ),
        (
            Capability(
                name="temperature",
                kind=CapabilityKind.NUMBER,
                readable=True,
                writable=True,
                maximum=32,
                commands=["set_value"],
            ),
            _semantic_command(value=33),
            "value_out_of_range",
        ),
        (
            Capability(
                name="temperature",
                kind=CapabilityKind.NUMBER,
                readable=True,
                writable=True,
                minimum=0,
                maximum=10,
                constraints={"step": 0.5},
                commands=["set_value"],
            ),
            _semantic_command(value=1.2),
            "invalid_command_value",
        ),
        (
            Capability(
                name="mode",
                kind=CapabilityKind.ENUM,
                readable=True,
                writable=True,
                enum_values=["auto", "manual"],
                commands=["set_value"],
            ),
            _semantic_command(value="unsupported"),
            "invalid_command_value",
        ),
        (
            Capability(
                name="power",
                kind=CapabilityKind.NUMBER,
                readable=True,
                writable=True,
                commands=["set_value"],
            ),
            _semantic_command(value=float("nan")),
            "invalid_command_value",
        ),
        (
            Capability(
                name="count",
                kind=CapabilityKind.INTEGER,
                readable=True,
                writable=True,
                commands=["set_value"],
            ),
            _semantic_command(value=1.2),
            "invalid_command_value",
        ),
        (
            Capability(
                name="temperature",
                kind=CapabilityKind.NUMBER,
                readable=True,
                writable=False,
                commands=["set_value"],
            ),
            _semantic_command(value=1),
            "invalid_capability",
        ),
    ]

    for capability, command, expected_code in cases:
        validated = _semantic_service(capability).validate(
            Plan(id=f"semantic-{expected_code}", commands=[command])
        )
        assert validated.validation is not None
        assert any(error.code == expected_code for error in validated.validation.errors)


def test_create_plan_and_validate_plan_fill_only_the_canonical_unit() -> None:
    service = _semantic_service(
        Capability(
            name="temperature",
            kind=CapabilityKind.NUMBER,
            unit="°C",
            readable=True,
            writable=True,
            commands=["set_value"],
        )
    )

    created = service.create_plan("semantic-normalized", [_semantic_command(value=22)])

    assert created.commands[0].unit == "°C"
    validated = service.validate(created)
    assert validated.commands[0].unit == "°C"


@pytest.mark.asyncio
async def test_create_plan_normalizes_declared_capability_unit() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    command = Command(
        id="command-normalized-1",
        device_id=device_id,
        command="set_brightness",
        value=60,
        idempotency_key="intent-normalized-1",
    )

    plan = service.create_plan("plan-normalized-1", [command])

    assert plan.commands[0].unit == "%"
    assert plan.expires_at is not None
    assert plan.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_create_plan_rejects_incompatible_unit() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    command = Command(
        id="command-unit-1",
        device_id=device_id,
        command="set_brightness",
        value=60,
        unit="W",
        idempotency_key="intent-unit-1",
    )

    with pytest.raises(DomainError, match="unit"):
        service.create_plan("plan-unit-1", [command])


def test_energy_binding_must_match_the_authorized_provider_route() -> None:
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "battery.route",
                    "device_id": "physical-battery-1",
                    "canonical_id": "energy.battery",
                    "name": "Battery",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": True,
                            "commands": ["charge_battery"],
                        }
                    ],
                    "identity_keys": ["fixture:battery"],
                    "connections": ["fixture:battery"],
                    "available": True,
                }
            ],
            source_states=[],
        ),
        "wrong_provider",
    )
    service = PlanService(
        registry,
        StateStore(),
        PolicyEngine([]),
        AuditLog(),
        authorized_actuator_commands={"energy.battery": frozenset({"charge_battery"})},
        authorized_actuator_providers={"energy.battery": "authorized_provider"},
    )

    result = service.validate_command_semantics(
        Command(
            id="provider-route-check",
            device_id="energy.battery",
            command="charge_battery",
            value=1.0,
            unit="kW",
            idempotency_key="provider-route-check-key",
        )
    )

    assert any(
        error.code == ErrorCode.ACTUATOR_AUTHORIZATION_REQUIRED for error in result.errors
    )


@pytest.mark.asyncio
async def test_definition_digest_changes_when_plan_intent_changes() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = Plan(
        id="plan-definition-digest-1",
        commands=[
            Command(
                id="command-definition-digest-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-definition-digest-1",
            )
        ],
    )

    validated = service.validate(plan)
    changed = service.validate(
        plan.model_copy(
            update={
                "commands": [
                    plan.commands[0].model_copy(update={"value": 61}),
                ]
            }
        )
    )

    assert validated.definition_digest is not None
    assert changed.definition_digest is not None
    assert validated.definition_digest != changed.definition_digest

    expiry_shifted = service.validate(
        plan.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(hours=2)})
    )
    assert expiry_shifted.definition_digest == validated.definition_digest


@pytest.mark.asyncio
async def test_validation_digest_changes_when_execution_window_changes() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    first = service.validate(
        Plan(
            id="plan-window-digest-1",
            execute_at=datetime(2026, 8, 23, 13, tzinfo=UTC),
            commands=[
                Command(
                    id="command-window-digest-1",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key="intent-window-digest-1",
                )
            ],
        )
    )
    second = service.validate(
        first.model_copy(
            update={
                "execution_window": first.execution_window.model_copy(
                    update={"intended_at": datetime(2026, 8, 23, 14, tzinfo=UTC)}
                ),
                "execute_at": datetime(2026, 8, 23, 14, tzinfo=UTC),
                "validation": None,
                "definition_digest": None,
            }
        )
    )

    assert first.execution_window is not None
    assert second.execution_window is not None
    assert first.validation is not None
    assert second.validation is not None
    assert first.execution_window.digest != second.execution_window.digest
    assert first.validation.digest != second.validation.digest
    assert first.definition_digest != second.definition_digest


@pytest.mark.asyncio
async def test_policy_revision_change_invalidates_previous_validation() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "switch"
    )
    plan = service.create_plan(
        "plan-policy-revision-1",
        [
            Command(
                id="command-policy-revision-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-policy-revision-1",
            )
        ],
    )
    validated = service.validate(plan)
    service.policy_engine.policies.append(
        Policy(
            id="deny-policy-revision-1",
            target={"device_id": device_id},
            action=PolicyAction.DENY,
        )
    )

    with pytest.raises(DomainError, match="revision"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_unrelated_state_change_does_not_invalidate_plan() -> None:
    adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-unrelated-1",
        [
            Command(
                id="command-unrelated-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-unrelated-1",
            )
        ],
    )
    validated = service.validate(plan)

    unrelated_device_id = next(
        device.id for device in service.registry.devices if device.type.value == "switch"
    )
    await service.state_store.save(
        StateSnapshot(
            device_id=unrelated_device_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=unrelated_device_id),
        )
    )

    executor = PlanExecutor(adapter, service, AuditLog())
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "confirmed_success"


@pytest.mark.asyncio
async def test_explicit_precondition_state_change_invalidates_plan() -> None:
    adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-own-state-1",
        [
            Command(
                id="command-own-state-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-own-state-1",
                preconditions=[
                    Precondition(device_id=light_id, capability="power", expected=False)
                ],
            )
        ],
    )
    validated = service.validate(plan)

    await service.state_store.save(
        StateSnapshot(
            device_id=light_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=light_id),
        )
    )

    with pytest.raises(DomainError, match="revision"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_target_state_is_not_an_implicit_execution_dependency() -> None:
    _adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )

    validated = service.validate(
        service.create_plan(
            "plan-target-not-assumption",
            [
                Command(
                    id="command-target-not-assumption",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="intent-target-not-assumption",
                )
            ],
        )
    )

    assert validated.validation is not None
    assert validated.validation.dependencies is not None
    assert validated.validation.dependencies.state_versions == {}


@pytest.mark.asyncio
async def test_explicit_precondition_is_a_revision_dependency() -> None:
    _adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )

    validated = service.validate(
        service.create_plan(
            "plan-explicit-precondition-dependency",
            [
                Command(
                    id="command-explicit-precondition-dependency",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="intent-explicit-precondition-dependency",
                    preconditions=[
                        Precondition(device_id=light_id, capability="power", expected=False)
                    ],
                )
            ],
        )
    )

    assert validated.validation is not None
    assert validated.validation.dependencies is not None
    key = f"{light_id}::power"
    assert key in validated.validation.dependencies.state_versions
    assert validated.validation.dependencies.dependency_kinds[key] == "precondition"


@pytest.mark.asyncio
async def test_validating_unchanged_plan_twice_yields_identical_digest() -> None:
    service = await build_service()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-digest-1",
        [
            Command(
                id="command-digest-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-digest-1",
            )
        ],
    )

    first = service.validate(plan)
    second = service.validate(plan)

    assert first.validation is not None
    assert second.validation is not None
    assert first.validation.digest == second.validation.digest


@pytest.mark.asyncio
async def test_plan_expiry_uses_injected_clock() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    plan = service.create_plan(
        "plan-clock-expiry-1",
        [
            Command(
                id="command-clock-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-clock-expiry-1",
            )
        ],
    )
    assert plan.expires_at == initial + PlanService.DEFAULT_PLAN_TTL

    validated = service.validate(plan)
    assert validated.validation is not None
    assert validated.validation.validated_at == initial

    clock.set(initial + PlanService.DEFAULT_PLAN_TTL + timedelta(seconds=1))

    with pytest.raises(DomainError, match="expired"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_validate_assigns_default_expiry_when_mcp_plan_omits_it() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-mcp-default-expiry-1",
        commands=[
            Command(
                id="command-mcp-default-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-mcp-default-expiry-1",
            )
        ],
    )

    validated = service.validate(plan)

    assert validated.expires_at == initial + PlanService.VALIDATION_TTL
    clock.set(initial + PlanService.VALIDATION_TTL + timedelta(seconds=1))
    with pytest.raises(DomainError, match="expired"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_validate_keeps_future_plan_valid_through_execution_window() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    execute_at = initial + timedelta(hours=4)
    plan = Plan(
        id="plan-future-default-expiry-1",
        execute_at=execute_at,
        commands=[
            Command(
                id="command-future-default-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-future-default-expiry-1",
            )
        ],
    )

    validated = service.validate(plan)

    assert validated.expires_at == initial + PlanService.VALIDATION_TTL
    assert validated.expires_at != execute_at + PlanService.DEFAULT_PLAN_TTL
