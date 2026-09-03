from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.models import (
    Approval,
    AvailabilityStatus,
    BundleMemberCommit,
    Capability,
    CapabilityKind,
    Command,
    Device,
    DeviceType,
    Plan,
    Precondition,
    RiskClass,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


def test_device_keeps_canonical_identity_and_source_reference() -> None:
    source = SourceRef(adapter_id="home_assistant", external_id="light.living_room")
    capability = Capability(
        name="brightness",
        kind=CapabilityKind.INTEGER,
        unit="%",
        readable=True,
        writable=True,
        minimum=0,
        maximum=100,
        commands=["set_brightness"],
    )

    device = Device(
        id="living_room.main_light",
        type=DeviceType.LIGHT,
        name="Main light",
        protocol="home_assistant",
        capabilities=[capability],
        source_refs=[source],
    )

    assert device.id == "living_room.main_light"
    assert device.source_refs[0].external_id == "light.living_room"
    assert device.availability is AvailabilityStatus.UNKNOWN


def test_capability_rejects_inverted_numeric_range() -> None:
    with pytest.raises(ValidationError, match="minimum"):
        Capability(
            name="brightness",
            kind=CapabilityKind.INTEGER,
            unit="%",
            readable=True,
            writable=True,
            minimum=100,
            maximum=0,
            commands=["set_brightness"],
        )


def test_capability_accepts_one_sided_numeric_bounds() -> None:
    capability = Capability(
        name="temperature",
        kind=CapabilityKind.NUMBER,
        readable=True,
        writable=True,
        maximum=32,
        commands=["set_temperature"],
    )

    assert capability.minimum is None
    assert capability.maximum == 32


def test_state_snapshot_requires_matching_observation_order() -> None:
    with pytest.raises(ValidationError, match="observed_at"):
        StateSnapshot(
            device_id="living_room.main_light",
            capability="brightness",
            value=50,
            observed_at=datetime(2026, 8, 15, 10, 1, tzinfo=UTC),
            received_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="light.living_room"),
        )


def test_plan_rejects_more_than_fifty_commands() -> None:
    commands = [
        Command(
            id=f"command-{index}",
            device_id="living_room.main_light",
            command="turn_on",
            risk_class=RiskClass.SAFE,
            idempotency_key=f"intent-{index}",
        )
        for index in range(51)
    ]

    with pytest.raises(ValidationError, match="50"):
        Plan(id="plan-1", commands=commands)


def _single_command() -> list[Command]:
    return [
        Command(
            id="command-1",
            device_id="living_room.main_light",
            command="turn_on",
            idempotency_key="intent-1",
        )
    ]


def test_plan_rejects_naive_execute_at() -> None:
    with pytest.raises(ValidationError, match="execute_at"):
        Plan(
            id="plan-naive-execute-at",
            execute_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_rejects_naive_expires_at() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        Plan(
            id="plan-naive-expires-at",
            expires_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="created_at"):
        Plan(
            id="plan-naive-created-at",
            created_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_accepts_aware_timestamps() -> None:
    plan = Plan(
        id="plan-aware-timestamps",
        created_at=datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 15, 11, 0, 0, tzinfo=UTC),
        execute_at=datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC),
        commands=_single_command(),
    )
    assert plan.execute_at is not None


def test_plan_accepts_omitted_optional_timestamps() -> None:
    plan = Plan(id="plan-omitted-timestamps", commands=_single_command())
    assert plan.execute_at is None
    assert plan.expires_at is None


def test_command_accepts_preconditions_within_the_limit() -> None:
    Command(
        id="command-preconditions-limit",
        device_id="living_room.main_light",
        command="set_brightness",
        value=50,
        idempotency_key="intent-preconditions-limit",
        preconditions=[
            Precondition(device_id="living_room.main_light", capability="power", expected=True)
            for _ in range(16)
        ],
    )


def test_command_rejects_preconditions_beyond_the_limit() -> None:
    with pytest.raises(ValidationError):
        Command(
            id="command-preconditions-over-limit",
            device_id="living_room.main_light",
            command="set_brightness",
            value=50,
            idempotency_key="intent-preconditions-over-limit",
            preconditions=[
                Precondition(
                    device_id="living_room.main_light", capability="power", expected=True
                )
                for _ in range(17)
            ],
        )


def test_approval_persists_scope_bundle_and_lifetime() -> None:
    approval = Approval(
        status="approved",
        approved_by="operator",
        approved_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        validation_digest="sha256:validation",
        bundle_digest="sha256:bundle",
        recurrence_digest=None,
        expires_at=datetime(2026, 8, 15, 10, 5, tzinfo=UTC),
    )

    assert approval.bundle_digest == "sha256:bundle"
    assert approval.expires_at is not None


def test_approval_id_round_trips_through_serialization() -> None:
    approval = Approval(
        status="approved",
        approved_by="operator",
        approved_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        validation_digest="sha256:validation",
        approval_id="approval-round-trip-1",
    )

    restored = Approval.model_validate(approval.model_dump())

    assert restored.approval_id == "approval-round-trip-1"


def test_approval_without_identifier_still_parses_as_legacy_evidence() -> None:
    legacy_payload = {
        "status": "approved",
        "approved_by": "operator",
        "approved_at": datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        "validation_digest": "sha256:validation",
    }

    approval = Approval.model_validate(legacy_payload)

    assert approval.approval_id is None


def test_bundle_member_normalizes_multiple_predecessors() -> None:
    member = BundleMemberCommit(
        plan_id="p2",
        validation_digest="sha256:v2",
        predecessor_plan_ids=["p0", "p1"],
    )

    assert member.all_predecessor_plan_ids == ["p0", "p1"]
