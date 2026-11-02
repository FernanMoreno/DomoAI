import asyncio
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.domain.errors import DomainError
from domoai.domain.models import (
    Command,
    Plan,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.persistence.repositories import ApprovalGrantRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalAssertion, ApprovalStore, DeliveryProtocolLimits
from tests.integration.test_household_consent import consent_fixture


def _plan() -> Plan:
    return Plan(
        id="durable-approval-plan",
        commands=[
            Command(
                id="durable-approval-command",
                device_id="cover.garage_main",
                command="open",
                idempotency_key="durable-approval-key",
            )
        ],
        status=PlanStatus.REQUIRES_CONFIRMATION,
        validation=ValidationResult(
            status=ValidationStatus.REQUIRES_CONFIRMATION,
            validated_at=datetime.now(UTC),
            runtime_revision="revision-1",
            digest="sha256:durable-approval",
        ),
        policy_decisions=[PolicyDecision(action=PolicyAction.CONFIRM, reason="test")],
    )


def _t007_issue(
    fixture,
    *,
    home_index=0,
    suffix="pending",
    store_index=0,
    validation_seconds=None,
    plan_changes=None,
):
    """Use the existing trusted-host assertion boundary, with an explicit clock."""
    home = fixture.homes[home_index]
    now = fixture.clock.now()
    plan = _plan().model_copy(
        update={
            "id": f"{home.agent.household_id}:{suffix}",
            "authority": home.agent,
            "commands": [home.command().model_copy(update={"id": suffix})],
            "validation": ValidationResult(
                status=ValidationStatus.REQUIRES_CONFIRMATION,
                validated_at=now,
                runtime_revision="revision-1",
                digest="sha256:" + "a" * 64,
                valid_until=(
                    now + timedelta(seconds=validation_seconds)
                    if validation_seconds is not None
                    else None
                ),
            ),
        }
    )
    if plan_changes is not None:
        plan = Plan.model_validate(plan.model_dump(mode="python") | plan_changes)
    grant = fixture.stores[store_index].issue_authenticated(
        plan,
        principal=home.resident_principal,
        assertion=ApprovalAssertion(
            principal=home.resident_principal,
            nonce=f"{plan.id}:nonce",
            approved_at=now,
            expires_at=now + timedelta(minutes=5),
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
        ),
    )
    return plan, grant


async def _t007_prepare_request(fixture, plan, grant, *, preference_seconds=60):
    """Storage-only protocol vectors; these bindings do not qualify a receiver or runtime."""
    from domoai.domain.coordination import FencingToken, LeaseScope, PhysicalIntent
    from tests.unit.persistence.test_approval_grant_repository import _t007_real_preferences

    preferences, preference_version, _ = await _t007_real_preferences(
        fixture, future_target=plan.commands[0].device_id, future_seconds=preference_seconds
    )
    repository = ApprovalGrantRepository(fixture.databases[0])
    control_version = repository.get_consent_control_sync(
        tenant_id="p04-tenant", household_id="p04-home-a"
    )
    fixture.databases[0].connection.rollback()
    now = fixture.clock.now()
    lease = FencingToken(
        scope=LeaseScope(
            tenant_id="p04-tenant", household_id="p04-home-a", deployment_id="storage"
        ),
        owner_id="issuer-a",
        epoch=7,
        lease_id="lease-a",
        issued_at=now,
        expires_at=now + timedelta(seconds=20),
    )
    return {
        "approval_id": grant.approval_id,
        "plan": plan,
        "command_id": plan.commands[0].id,
        "request_id": "prepare-a",
        "expected_control_version": control_version,
        "expected_preference_version": preference_version,
        "preference_snapshot": preferences.snapshot_in_transaction,
        "intent": PhysicalIntent(
            tenant_id="p04-tenant",
            household_id="p04-home-a",
            deployment_id="storage",
            idempotency_key=plan.commands[0].idempotency_key,
            plan_id=plan.id,
            command_id=plan.commands[0].id,
            fencing_epoch=7,
            owner_id="issuer-a",
            execution_attempt_id="attempt-a",
            lease_id="lease-a",
            created_at=now,
            updated_at=now,
        ),
        "lease": lease,
        "receiver_id": "storage-receiver-a",
        "route_id": "storage-route-a",
        "receiver_generation": "00000000-0000-4000-8000-000000000007",
        "definition_digest": "sha256:" + "a" * 64,
        "not_before": now,
        # Explicit test inputs, never a default or claim of receiver-qualified D_max.
        "protocol_limits": {
            "version": "test-v1",
            "d_max_seconds": 30.0,
            "clock_uncertainty_seconds": 0.1,
            "clock_observed_at": now,
        },
    }


def _t007_assert_permit(receipt, request, grant):
    """Check every storage identity and conservative time binding independently."""
    permit = receipt.permit
    assert receipt.status == "prepared"
    assert permit.permit_id
    assert permit.approval_id == grant.approval_id
    assert (permit.tenant_id, permit.household_id, permit.deployment_id) == (
        "p04-tenant",
        "p04-home-a",
        "storage",
    )
    assert permit.control_version == request["expected_control_version"]
    assert permit.preference_version == request["expected_preference_version"]
    assert str(permit.generation) == str(request["expected_control_version"].generation)
    assert (permit.receiver_id, permit.route_id, str(permit.receiver_generation)) == (
        "storage-receiver-a",
        "storage-route-a",
        "00000000-0000-4000-8000-000000000007",
    )
    assert (permit.plan_id, permit.command_id, permit.execution_attempt_id) == (
        request["plan"].id,
        request["command_id"],
        "attempt-a",
    )
    assert permit.idempotency_key == request["plan"].commands[0].idempotency_key
    assert (permit.owner_id, permit.fencing_epoch, permit.lease_id) == ("issuer-a", 7, "lease-a")
    assert permit.definition_digest == "sha256:" + "a" * 64
    assert permit.command_digest.startswith("sha256:") and len(permit.command_digest) == 71
    assert permit.protocol_version == "test-v1"
    assert permit.issued_at == datetime(2026, 9, 13, 12, tzinfo=UTC)
    assert permit.not_before == datetime(2026, 9, 13, 12, tzinfo=UTC)
    assert permit.issued_at < permit.not_after <= datetime(2026, 9, 13, 12, 0, 20, tzinfo=UTC)
    assert permit.not_after <= permit.issued_at + timedelta(seconds=30)
    assert permit.integrity


def _t007_assert_stored_intent(connection, receipt):
    """An unrelated counted row must not stand in for this permit's persisted claim."""
    from domoai.domain.coordination import PhysicalIntent

    row = connection.execute(
        "SELECT payload, status, fencing_epoch, owner_id, execution_attempt_id "
        "FROM physical_intents WHERE household_id=? AND idempotency_key=?",
        ("p04-home-a", "p04-home-a:intent"),
    ).fetchone()
    assert row is not None
    assert tuple(row[1:]) == ("dispatching", 7, "issuer-a", "attempt-a")
    intent = PhysicalIntent.model_validate_json(row[0])
    identity = (
        intent.tenant_id,
        intent.household_id,
        intent.deployment_id,
        intent.idempotency_key,
        intent.plan_id,
        intent.command_id,
        intent.execution_attempt_id,
        intent.owner_id,
        intent.fencing_epoch,
        intent.lease_id,
    )
    assert identity == (
        "p04-tenant",
        "p04-home-a",
        "storage",
        "p04-home-a:intent",
        "p04-home-a:pending",
        "pending",
        "attempt-a",
        "issuer-a",
        7,
        "lease-a",
    )
    assert intent.status.value == "dispatching"  # Claim is not receiver E.
    permit = receipt.permit
    assert identity == (
        permit.tenant_id,
        permit.household_id,
        permit.deployment_id,
        permit.idempotency_key,
        permit.plan_id,
        permit.command_id,
        permit.execution_attempt_id,
        permit.owner_id,
        permit.fencing_epoch,
        permit.lease_id,
    )
    assert receipt.status == "prepared"


def _t007_b_assert_no_delivery_authority(fixture, grant):
    """Observe committed state from the independent connection after rejection."""
    observer = fixture.databases[1].connection
    assert tuple(
        observer.execute(
            "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
            (grant.approval_id,),
        ).fetchone()
    ) == ("pending", None)
    assert observer.execute("SELECT COUNT(*) FROM physical_intents").fetchone()[0] == 0
    assert observer.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0] == 0
    observer.rollback()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_r1_original_plan_scope_and_expiry_bound_permit_and_retry(tmp_path, backend):
    """Break: an originally short-lived Plan receives the longer lease deadline."""
    async with consent_fixture(tmp_path, backend) as fixture:
        expiry = fixture.clock.now() + timedelta(seconds=2)
        plan, grant = _t007_issue(
            fixture,
            plan_changes={
                "expires_at": expiry,
                "deployment_id": "storage",
                "schema_version": "v2",
                "definition_digest": "sha256:" + "a" * 64,
            },
        )
        request = await _t007_prepare_request(fixture, plan, grant)
        receipt = fixture.stores[0].prepare_delivery(**request)
        assert receipt.permit.issued_at < receipt.permit.not_after <= expiry
        assert receipt.permit.deployment_id == plan.deployment_id == "storage"
        assert receipt.permit.definition_digest == plan.definition_digest == "sha256:" + "a" * 64
        fixture.clock.set(expiry + timedelta(seconds=1))
        assert fixture.stores[1].prepare_delivery(**request) == receipt
        await fixture.databases[1].close()
        await fixture.databases[1].initialize()
        restarted = ApprovalStore(
            clock=fixture.clock,
            persistence=ApprovalGrantRepository(fixture.databases[1]),
        )
        assert restarted.prepare_delivery(**request) == receipt


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("mutation", ["introduced", "extended", "removed", "expired", "elapsed"])
async def test_t007_b_r1_expired_or_altered_plan_lifetime_never_consumes(
    tmp_path, backend, mutation
):
    """Break: first prepare trusts a changed/removed original expiry or prepares after it."""
    async with consent_fixture(tmp_path, backend) as fixture:
        now = fixture.clock.now()
        expiry = (
            None
            if mutation == "introduced"
            else now + timedelta(seconds=-1 if mutation == "expired" else 2)
        )
        plan, grant = _t007_issue(fixture, plan_changes={"expires_at": expiry})
        if mutation == "elapsed":
            fixture.clock.set(now + timedelta(seconds=3))
        request = await _t007_prepare_request(fixture, plan, grant)
        if mutation in {"introduced", "extended", "removed"}:
            altered_expiry = {
                "introduced": now + timedelta(seconds=2),
                "extended": now + timedelta(seconds=60),
                "removed": None,
            }[mutation]
            request["plan"] = Plan.model_validate(
                plan.model_dump(mode="python") | {"expires_at": altered_expiry}
            )
        with pytest.raises(DomainError) as rejected:
            fixture.stores[0].prepare_delivery(**request)
        assert rejected.value.code == (
            "delivery_fence_unavailable"
            if mutation in {"expired", "elapsed"}
            else "consent_conflict"
        )
        _t007_b_assert_no_delivery_authority(fixture, grant)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("field", ["deployment", "definition"])
@pytest.mark.parametrize("mutation", ["introduced", "all-copies", "removed", "supplied-only"])
async def test_t007_b_r1_original_plan_scope_rejects_independent_and_joint_changes(
    tmp_path,
    backend,
    field,
    mutation,
):
    """Break: contradictory Plan identity or coordinated replacement evades issued evidence."""
    async with consent_fixture(tmp_path, backend) as fixture:
        changes = {}
        if mutation != "introduced":
            changes = (
                {"deployment_id": "storage", "schema_version": "v2"}
                if field == "deployment"
                else {"definition_digest": "sha256:" + "a" * 64}
            )
        plan, grant = _t007_issue(fixture, plan_changes=changes)
        request = await _t007_prepare_request(fixture, plan, grant)
        replacement = "other-deployment" if field == "deployment" else "sha256:" + "b" * 64
        if mutation != "supplied-only":
            value = None if mutation == "removed" else replacement
            changes = (
                {"deployment_id": value, "schema_version": "v1" if value is None else "v2"}
                if field == "deployment"
                else {"definition_digest": value}
            )
            request["plan"] = Plan.model_validate(plan.model_dump(mode="python") | changes)
        if mutation in {"all-copies", "supplied-only"}:
            if field == "deployment":
                request["intent"] = request["intent"].model_copy(
                    update={"deployment_id": replacement}
                )
                request["lease"] = request["lease"].model_copy(
                    update={
                        "scope": request["lease"].scope.model_copy(
                            update={"deployment_id": replacement}
                        )
                    }
                )
            else:
                request["definition_digest"] = replacement
        with pytest.raises(DomainError) as rejected:
            fixture.stores[0].prepare_delivery(**request)
        assert rejected.value.code == "consent_conflict"
        _t007_b_assert_no_delivery_authority(fixture, grant)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("corruption", ["projection", "consistent-status", "consistent-owner"])
async def test_t007_b_r1_prepare_rejects_actual_post_insert_intent_corruption(
    tmp_path,
    backend,
    corruption,
):
    """Break: prepare commits authority for a newly inserted non-dispatching or foreign intent."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        connection = fixture.databases[0].connection
        column, value = (
            ("owner_id", "wrong-owner")
            if corruption == "consistent-owner"
            else ("status", "unknown")
        )
        if backend == "sqlite":
            payload = (
                f", payload=json_set(payload, '$.{column}', '{value}')"
                if corruption != "projection"
                else ""
            )
            connection.execute(
                f"CREATE TRIGGER t007_r1_change AFTER INSERT ON physical_intents BEGIN "
                f"UPDATE physical_intents SET {column}='{value}'{payload} "
                "WHERE household_id=NEW.household_id AND idempotency_key=NEW.idempotency_key; END"
            )
        else:
            payload = (
                f"NEW.payload := jsonb_set(NEW.payload::jsonb, '{{{column}}}', "
                f"'\"{value}\"'::jsonb)::text; "
                if corruption != "projection"
                else ""
            )
            connection.execute(
                "CREATE FUNCTION t007_r1_change() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN NEW.{column} := '{value}'; {payload}RETURN NEW; END $$"
            )
            connection.execute(
                "CREATE TRIGGER t007_r1_change BEFORE INSERT ON physical_intents "
                "FOR EACH ROW EXECUTE FUNCTION t007_r1_change()"
            )
        connection.commit()
        with pytest.raises(DomainError) as rejected:
            fixture.stores[0].prepare_delivery(**request)
        assert rejected.value.code == "consent_conflict"
        _t007_b_assert_no_delivery_authority(fixture, grant)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_prepare_rejects_command_changed_before_first_prepare(tmp_path, backend):
    """Break: first prepare trusts unchanged validation digest over the issued command."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        altered = plan.model_copy(
            update={"commands": [plan.commands[0].model_copy(update={"command": "turn_off"})]}
        )
        with pytest.raises(DomainError) as conflict:
            fixture.stores[0].prepare_delivery(**(request | {"plan": altered}))
        assert conflict.value.code == "consent_conflict"
        assert ApprovalGrantRepository(fixture.databases[1]).is_pending_sync(grant.approval_id)
        assert (
            fixture.databases[1]
            .connection.execute("SELECT COUNT(*) FROM physical_intents")
            .fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_prepare_retry_keeps_bound_permit_and_rejects_changed_command(
    tmp_path: Path, backend: str
) -> None:
    """Break: successful storage prepare duplicates/extends a permit or accepts changed content."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        first = fixture.stores[0].prepare_delivery(**request)
        _t007_assert_permit(first, request, grant)
        _t007_assert_stored_intent(fixture.databases[0].connection, first)
        fixture.databases[0].connection.rollback()
        fixture.clock.set(fixture.clock.now() + timedelta(seconds=1))
        second = fixture.stores[1].prepare_delivery(**request)
        assert second == first
        assert second.permit.issued_at == datetime(2026, 9, 13, 12, tzinfo=UTC)
        assert second.permit.not_after == first.permit.not_after
        observer = ApprovalGrantRepository(fixture.databases[1])
        assert (
            observer.get_delivery_receipt_sync(request_id="prepare-a", household_id="p04-home-a")
            == first
        )
        assert (
            fixture.databases[1]
            .connection.execute("SELECT COUNT(*) FROM physical_intents")
            .fetchone()[0]
            == 1
        )
        _t007_assert_stored_intent(fixture.databases[1].connection, second)
        fixture.databases[1].connection.rollback()
        altered = plan.model_copy(
            update={"commands": [plan.commands[0].model_copy(update={"command": "turn_off"})]}
        )
        with pytest.raises(DomainError) as conflict:
            fixture.stores[1].prepare_delivery(**(request | {"plan": altered}))
        assert conflict.value.code == "consent_conflict"
        # Real store verifies integrity/binding; this is not receiver E or an adapter claim.
        assert observer.verify_delivery_permit_sync(first.permit, command=plan.commands[0]) is True
        different_command = plan.commands[0].model_copy(update={"command": "turn_off"})
        with pytest.raises(DomainError):
            observer.verify_delivery_permit_sync(first.permit, command=different_command)
        digest = first.permit.command_digest
        changed_digest = "sha256:" + ("0" if digest[7] != "0" else "1") + digest[8:]
        with pytest.raises(DomainError):
            observer.verify_delivery_permit_sync(
                first.permit.model_copy(update={"command_digest": changed_digest}),
                command=plan.commands[0],
            )
        tampered = first.permit.model_copy(
            update={"not_after": first.permit.not_after + timedelta(seconds=1)}
        )
        with pytest.raises(DomainError):
            observer.verify_delivery_permit_sync(tampered, command=plan.commands[0])
        await fixture.databases[1].close()
        await fixture.databases[1].initialize()
        restarted = ApprovalStore(
            clock=fixture.clock, persistence=ApprovalGrantRepository(fixture.databases[1])
        )
        assert restarted.prepare_delivery(**request) == first
        reopened_receipt = ApprovalGrantRepository(fixture.databases[1]).get_delivery_receipt_sync(
            request_id="prepare-a", household_id="p04-home-a"
        )
        assert reopened_receipt == first
        _t007_assert_stored_intent(fixture.databases[1].connection, reopened_receipt)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize(
    "invalid",
    [
        "limits-missing",
        "dmax-missing",
        "dmax-zero",
        "dmax-negative",
        "dmax-infinite",
        "clock-missing",
        "clock-negative",
        "clock-negative-model-copy",
        "clock-negative-model-construct",
        "clock-nan",
        "clock-rollback",
        "expired-lease",
        "not-before-beyond-max",
    ],
)
async def test_t007_b_invalid_protocol_time_cannot_create_new_delivery_authority(
    tmp_path: Path, backend: str, invalid: str
) -> None:
    """Break: one invalid/missing time guarantee issues a permit despite otherwise bound inputs."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        limits = request["protocol_limits"]
        if invalid == "limits-missing":
            request["protocol_limits"] = None
        elif invalid == "dmax-missing":
            limits.pop("d_max_seconds")
        elif invalid.startswith("dmax-"):
            limits["d_max_seconds"] = {
                "dmax-zero": 0,
                "dmax-negative": -1,
                "dmax-infinite": float("inf"),
            }[invalid]
        elif invalid == "clock-missing":
            limits.pop("clock_uncertainty_seconds")
        elif invalid == "clock-negative":
            limits["clock_uncertainty_seconds"] = -0.1
        elif invalid in {"clock-negative-model-copy", "clock-negative-model-construct"}:
            # A frozen model can still contain unchecked values from copy/construct.
            # Negative uncertainty would extend the lease bound beyond this start.
            if invalid == "clock-negative-model-copy":
                request["protocol_limits"] = DeliveryProtocolLimits.model_validate(
                    limits
                ).model_copy(update={"clock_uncertainty_seconds": -20.0})
            else:
                request["protocol_limits"] = DeliveryProtocolLimits.model_construct(
                    **(limits | {"clock_uncertainty_seconds": -20.0})
                )
            request["not_before"] = fixture.clock.now() + timedelta(seconds=20)
        elif invalid == "clock-nan":
            limits["clock_uncertainty_seconds"] = float("nan")
        elif invalid == "clock-rollback":
            fixture.clock.set(fixture.clock.now() - timedelta(seconds=1))
        elif invalid == "expired-lease":
            request["lease"] = request["lease"].model_copy(
                update={
                    "issued_at": fixture.clock.now() - timedelta(seconds=30),
                    "expires_at": fixture.clock.now() - timedelta(seconds=1),
                }
            )
        else:
            request["not_before"] = fixture.clock.now() + timedelta(seconds=40)
        with pytest.raises(DomainError) as unavailable:
            fixture.stores[0].prepare_delivery(**request)
        assert unavailable.value.code == "delivery_fence_unavailable"
        from domoai.domain.errors import ErrorCode

        assert isinstance(unavailable.value.code, ErrorCode)
        assert unavailable.value.as_detail().model_dump(mode="json") == {
            "code": "delivery_fence_unavailable",
            "message": unavailable.value.message,
            "field": None,
            "device_id": None,
            "capability": None,
            "retryable": False,
            "details": {},
        }
        observer = ApprovalGrantRepository(fixture.databases[1])
        assert observer.is_pending_sync(grant.approval_id) is True
        assert observer.reservation_status_sync(grant.approval_id) is None
        assert (
            observer.get_delivery_receipt_sync(request_id="prepare-a", household_id="p04-home-a")
            is None
        )
        assert (
            fixture.databases[1]
            .connection.execute("SELECT COUNT(*) FROM physical_intents")
            .fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize(
    "boundary,seconds",
    [
        ("dmax", 5),
        ("plan", 4),
        ("preference", 3),
        ("dmax-model", 5),
        ("plan-model", 4),
        ("preference-model", 3),
    ],
)
async def test_t007_b_shorter_protocol_plan_or_preference_boundary_limits_permit(
    tmp_path: Path, backend: str, boundary: str, seconds: int
) -> None:
    """Break: permit inherits a longer lease instead of the shorter D_max/plan/P02 boundary."""
    model_limits = boundary.endswith("-model")
    boundary = boundary.removesuffix("-model")
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture, validation_seconds=4 if boundary == "plan" else None)
        request = await _t007_prepare_request(
            fixture, plan, grant, preference_seconds=3 if boundary == "preference" else 60
        )
        if boundary == "dmax":
            request["protocol_limits"]["d_max_seconds"] = 5
        if model_limits:
            request["protocol_limits"] = DeliveryProtocolLimits.model_validate(
                request["protocol_limits"]
            )
        receipt = fixture.stores[0].prepare_delivery(**request)
        assert receipt.status == "prepared"
        assert (
            receipt.permit.issued_at
            < receipt.permit.not_after
            <= datetime(2026, 9, 13, 12, 0, seconds, tzinfo=UTC)
        )
        fixture.clock.set(fixture.clock.now() + timedelta(seconds=1))
        assert fixture.stores[1].prepare_delivery(**request) == receipt


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_retry_after_expiry_preserves_only_original_receipt(tmp_path, backend):
    """Break: historical retry renews authority or loses its original receipt after expiry."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        first = fixture.stores[0].prepare_delivery(**request)
        fixture.clock.set(fixture.clock.now() + timedelta(minutes=6))
        assert fixture.stores[1].prepare_delivery(**request) == first
        assert first.permit.not_after < fixture.clock.now()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_plan_execution_window_bounds_prepare(tmp_path, backend):
    """Break: permit exceeds its approved execution window despite a longer lease."""
    from domoai.domain.models import ExecutionWindow

    async with consent_fixture(tmp_path, backend) as fixture:
        plan = _plan().model_copy(
            update={
                "authority": fixture.homes[0].agent,
                "commands": [
                    Command(
                        id="vector-command",
                        device_id="lamp",
                        command="turn_on",
                        idempotency_key="vector-key",
                    )
                ],
                "execute_at": fixture.clock.now(),
                "execution_window": ExecutionWindow(
                    intended_at=fixture.clock.now(),
                    not_before=fixture.clock.now(),
                    not_after=fixture.clock.now() + timedelta(seconds=2),
                    timezone="UTC",
                ),
            }
        )
        grant = fixture.stores[0].issue_authenticated(
            plan,
            principal=fixture.homes[0].resident_principal,
            assertion=ApprovalAssertion(
                principal=fixture.homes[0].resident_principal,
                nonce="window-nonce",
                approved_at=fixture.clock.now(),
                expires_at=fixture.clock.now() + timedelta(minutes=5),
                plan_id=plan.id,
            ),
        )
        request = await _t007_prepare_request(fixture, plan, grant)
        receipt = fixture.stores[0].prepare_delivery(**request)
        assert receipt.permit.not_after <= fixture.clock.now() + timedelta(seconds=2)
        # Independent vector: compact sorted UTF-8 JSON of the ten literal command fields.
        assert receipt.permit.command_digest == (
            "sha256:784b13cc7193262dfa761c76fa431b4afc7e5be7620086af677161502eef82c2"
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_retry_rejects_inconsistent_durable_intent(tmp_path, backend):
    """Break: retry validates receipt while overlooking a conflicting real intent projection."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        receipt = fixture.stores[0].prepare_delivery(**request)
        fixture.databases[0].connection.execute(
            "UPDATE physical_intents SET owner_id='wrong-owner'"
        )
        fixture.databases[0].connection.commit()
        with pytest.raises(DomainError) as conflict:
            fixture.stores[1].prepare_delivery(**request)
        assert conflict.value.code == "consent_conflict"
        assert (
            ApprovalGrantRepository(fixture.databases[0]).get_delivery_receipt_sync(
                request_id="prepare-a", household_id="p04-home-a"
            )
            == receipt
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("first_operation", ["revoke", "prepare"])
async def test_t007_b_r_prepare_orders_keep_pending_inventory_without_confirmed_cutoff(
    tmp_path: Path, backend: str, first_operation: str
) -> None:
    """Break: R permits fresh issuance, or a prior permit disappears from pending R inventory."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        prepare = fixture.stores[1].prepare_delivery
        revoke = fixture.stores[0].revoke_consent
        before_head, resume = threading.Event(), threading.Event()

        def second_operation():
            before_head.set()
            if not resume.wait(15):
                raise TimeoutError("ordered contender was not resumed")
            if first_operation == "revoke":
                try:
                    return prepare(**request)
                except DomainError as error:
                    return error
            return revoke(
                grant.approval_id, authority=fixture.homes[0].resident, request_id="ordered-r"
            )

        worker = asyncio.create_task(asyncio.to_thread(second_operation))
        try:
            assert await asyncio.to_thread(before_head.wait, 10)
            if first_operation == "revoke":
                prepared = None
                revoked = revoke(
                    grant.approval_id, authority=fixture.homes[0].resident, request_id="ordered-r"
                )
            else:
                prepared = prepare(**request)
            resume.set()
            later = await asyncio.wait_for(worker, 10)
        finally:
            resume.set()
            await asyncio.wait_for(worker, 10)
        if first_operation == "prepare":
            revoked = later
        assert revoked.status == "revocation_pending"
        assert revoked.confirmed_at is None
        if prepared is None:
            assert isinstance(later, DomainError)
            assert later.code == "consent_revoked"
            assert revoked.permit_ids == []
            assert (
                fixture.databases[1]
                .connection.execute("SELECT COUNT(*) FROM physical_intents")
                .fetchone()[0]
                == 0
            )
        else:
            assert revoked.permit_ids == [prepared.permit.permit_id]
            assert revoked.routes == ["storage-route-a"]
            assert (
                ApprovalGrantRepository(fixture.databases[1]).get_delivery_receipt_sync(
                    request_id="prepare-a", household_id="p04-home-a"
                )
                == prepared
            )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_after_prepare_commit_independent_revoke_finishes_before_network_resume(
    tmp_path: Path, backend: str
) -> None:
    """Break: prepare retains a DB transaction/head lock while the sender waits before network."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        prepare = fixture.stores[0].prepare_delivery
        revoke = fixture.stores[1].revoke_consent
        committed, resume = threading.Event(), threading.Event()

        def sender():
            receipt = prepare(**request)
            committed.set()  # Exactly after prepare returned its committed receipt.
            if not resume.wait(15):
                raise TimeoutError("sender was not resumed")
            return receipt  # No receiver or network is simulated as evidence of E.

        worker = asyncio.create_task(asyncio.to_thread(sender))
        try:
            assert await asyncio.to_thread(committed.wait, 10)
            pending = await asyncio.wait_for(
                asyncio.to_thread(
                    revoke,
                    grant.approval_id,
                    authority=fixture.homes[0].resident,
                    request_id="while-network-paused",
                ),
                5,
            )
            assert pending.status == "revocation_pending"
            assert pending.confirmed_at is None
            observed = ApprovalGrantRepository(fixture.databases[1]).get_delivery_receipt_sync(
                request_id="prepare-a", household_id="p04-home-a"
            )
            assert pending.permit_ids == [observed.permit.permit_id]
            assert not worker.done()
        finally:
            resume.set()
            await asyncio.wait_for(worker, 10)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_a_revoke_blocks_warm_grant_and_survives_connection_restart(
    tmp_path: Path, backend: str
) -> None:
    """Break: R is cache-local, revives on release/restart, or revokes another home."""
    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = fixture.stores
        plan, grant = _t007_issue(fixture)
        other_plan, other_grant = _t007_issue(fixture, home_index=1)
        assert second.validate(grant.approval_id, plan) == grant
        assert second.reserve(grant.approval_id, plan, reservation_id="before-r") == grant
        receipt = first.revoke_consent(
            grant.approval_id,
            authority=fixture.homes[0].resident,
            request_id="revoke-a",
        )
        assert receipt.status == "revocation_pending"
        assert (
            first.revoke_consent(
                grant.approval_id,
                authority=fixture.homes[0].resident,
                request_id="revoke-a",
            )
            == receipt
        )
        for store in (first, second):
            with pytest.raises(DomainError) as rejected:
                store.validate(grant.approval_id, plan)
            assert rejected.value.code == "consent_revoked"
            with pytest.raises(DomainError) as rejected:
                store.consume(grant.approval_id, plan)
            assert rejected.value.code == "consent_revoked"
            with pytest.raises(DomainError) as rejected:
                store.reserve(grant.approval_id, plan, reservation_id="after-r")
            assert rejected.value.code == "consent_revoked"
        second.release_reservation("before-r", [grant.approval_id])
        await fixture.databases[1].close()
        await fixture.databases[1].initialize()
        restarted = ApprovalStore(
            clock=fixture.clock, persistence=ApprovalGrantRepository(fixture.databases[1])
        )
        with pytest.raises(DomainError) as rejected:
            restarted.validate(grant.approval_id, plan)
        assert rejected.value.code == "consent_revoked"
        assert restarted.validate(other_grant.approval_id, other_plan) == other_grant
        repository = ApprovalGrantRepository(fixture.databases[1])
        assert repository.get_consent_revocation_sync(grant.approval_id) == receipt
        assert (
            restarted.revoke_consent(
                grant.approval_id,
                authority=fixture.homes[0].resident,
                request_id="revoke-a",
            )
            == receipt
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_a_revoke_rejects_reserved_cache_in_separate_process_before_head(
    tmp_path: Path, backend: str
) -> None:
    """Break: a suspended issuer's cached reservation authorizes after another process R."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        database = fixture.databases[0]
        script = """
import asyncio, json, sys
from datetime import datetime
from pathlib import Path
from domoai.domain.errors import DomainError
from domoai.domain.models import Plan
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.repositories import ApprovalGrantRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock

async def main():
    data = json.loads(sys.stdin.readline())
    db = (SQLiteDatabase(Path(data['address'])) if data['backend'] == 'sqlite'
          else PostgresDatabase(data['address']))
    await db.initialize()
    try:
        store = ApprovalStore(clock=FixedClock(datetime.fromisoformat(data['now'])),
                              persistence=ApprovalGrantRepository(db))
        plan = Plan.model_validate(data['plan'])
        store.reserve(data['approval_id'], plan, reservation_id='process-r1')
        db.connection.rollback()
        print('before-head', flush=True)
        if sys.stdin.readline().strip() != 'resume':
            return
        try:
            store.reserve(data['approval_id'], plan, reservation_id='process-r1')
        except DomainError as error:
            print(json.dumps({'error': error.code}), flush=True)
        else:
            print(json.dumps({'error': None}), flush=True)
    finally:
        await db.close()

asyncio.run(main())
"""
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            worker.stdin.write(
                (
                    json.dumps(
                        {
                            "backend": backend,
                            "address": str(database.path) if backend == "sqlite" else database.dsn,
                            "now": fixture.clock.now().isoformat(),
                            "plan": plan.model_dump(mode="json"),
                            "approval_id": grant.approval_id,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await worker.stdin.drain()
            assert await asyncio.wait_for(worker.stdout.readline(), 30) == b"before-head\n"
            receipt = fixture.stores[0].revoke_consent(
                grant.approval_id,
                authority=fixture.homes[0].resident,
                request_id="process-revoke",
            )
            assert receipt.status == "revocation_pending"
            worker.stdin.write(b"resume\n")
            await worker.stdin.drain()
            result = json.loads(await asyncio.wait_for(worker.stdout.readline(), 10))
            assert result == {"error": "consent_revoked"}
            assert await asyncio.wait_for(worker.wait(), 10) == 0
        finally:
            if worker.returncode is None:
                worker.terminate()
            await asyncio.wait_for(worker.communicate(), 10)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_a_competing_nonce_writers_have_one_grant_receipt_and_no_loser_authority(
    tmp_path: Path, backend: str
) -> None:
    """Break: a shared nonce creates two grants/receipts or leaves a losing grant authoritative."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, base = _t007_issue(fixture)
        assertion = ApprovalAssertion(
            principal=fixture.homes[0].resident_principal,
            nonce="competing-nonce",
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            approved_at=fixture.clock.now(),
            expires_at=fixture.clock.now() + timedelta(minutes=2),
        )
        start = threading.Barrier(2, timeout=10)

        def issue(index):
            fixture.databases[index].connection.rollback()
            start.wait()  # Both writers enter before any issuance/head lock.
            try:
                return fixture.stores[index].issue_authenticated(
                    plan, principal=assertion.principal, assertion=assertion
                )
            except DomainError as error:
                return error

        outcomes = await asyncio.wait_for(
            asyncio.gather(asyncio.to_thread(issue, 0), asyncio.to_thread(issue, 1)), 20
        )
        successes = [outcome for outcome in outcomes if not isinstance(outcome, DomainError)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, DomainError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "approval_assertion_replayed"
        winner = successes[0]
        for database, store in zip(fixture.databases, fixture.stores, strict=True):
            database.connection.rollback()
            ids = {
                row[0]
                for row in database.connection.execute(
                    "SELECT approval_id FROM approval_grants"
                ).fetchall()
            }
            assert ids == {base.approval_id, winner.approval_id}
            # An orphan success hint must never escape a losing durable write.
            for cached_id in set(store._grants) - ids:
                with pytest.raises(DomainError):
                    store.validate(cached_id, plan)
            database.connection.rollback()
            with pytest.raises(DomainError) as replay:
                store.issue_authenticated(plan, principal=assertion.principal, assertion=assertion)
            assert replay.value.code == "approval_assertion_replayed"
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        receipt = first.get_issuance_receipt_sync(assertion_nonce="competing-nonce")
        assert receipt.approval_id == winner.approval_id
        assert second.get_issuance_receipt_sync(assertion_nonce="competing-nonce") == receipt


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_prepare_without_qualified_dependencies_cannot_consume_or_claim(
    tmp_path: Path, backend: str
) -> None:
    """Break: missing P02 snapshot/clock/D_max/receiver qualification defaults to permission."""
    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        other_plan, other_grant = _t007_issue(fixture, home_index=1)
        # T004 has no qualified receiver, clock bound, D_max or P02 helper.
        # Missing prepare is a RED precursor, never an expected AttributeError.
        with pytest.raises(DomainError) as unavailable:
            fixture.stores[0].prepare_delivery(
                grant.approval_id,
                plan,
                command_id=plan.commands[0].id,
                request_id="prepare-unavailable",
            )
        assert unavailable.value.code == "delivery_fence_unavailable"
        observer = ApprovalGrantRepository(fixture.databases[1])
        assert observer.is_pending_sync(grant.approval_id) is True
        assert observer.reservation_status_sync(grant.approval_id) is None
        assert (
            fixture.databases[1]
            .connection.execute("SELECT COUNT(*) FROM physical_intents")
            .fetchone()[0]
            == 0
        )
        assert fixture.stores[1].validate(other_grant.approval_id, other_plan) == other_grant


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_c_cached_r1_cannot_authorize_after_other_connection_reserves_r2(
    tmp_path: Path, backend: str
) -> None:
    """Break: reserve returns the cached R1 grant after durable ownership moved to R2."""
    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = fixture.stores
        plan, grant = _t007_issue(fixture)
        assert first.reserve(grant.approval_id, plan, reservation_id="r1") == grant
        second.release_reservation("r1", [grant.approval_id])
        assert second.reserve(grant.approval_id, plan, reservation_id="r2") == grant
        observer = ApprovalGrantRepository(fixture.databases[1])
        assert observer.reservation_status_sync(grant.approval_id) == ("r2", "reserved")
        fixture.databases[1].connection.rollback()

        with pytest.raises(DomainError, match="reserved"):
            first.reserve(grant.approval_id, plan, reservation_id="r1")

        assert observer.reservation_status_sync(grant.approval_id) == ("r2", "reserved")
        assert observer.is_consumed_sync(grant.approval_id) is False


@pytest.mark.asyncio
async def test_approval_store_restores_pending_grants_and_keeps_consumption_one_shot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-store.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(database),
    )
    plan = _plan()
    grant = store.issue(plan, approved_by="operator", operator_token="operator")
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restarted_store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(restarted_database),
    )

    assert restarted_store.consume(grant.approval_id, plan).approval_id == grant.approval_id
    with pytest.raises(DomainError):
        restarted_store.consume(grant.approval_id, plan)

    await restarted_database.close()


@pytest.mark.asyncio
async def test_approval_reservation_survives_restart_and_can_be_released(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-reservation.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(database),
    )
    plan = _plan()
    grant = store.issue(plan, approved_by="operator", operator_token="operator")
    store.reserve(grant.approval_id, plan, reservation_id="bundle-restart")
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restarted_store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(restarted_database),
    )

    with pytest.raises(DomainError, match="reserved"):
        restarted_store.consume(grant.approval_id, plan)
    restarted_store.release_reservation("bundle-restart")
    assert restarted_store.consume(grant.approval_id, plan).approval_id == grant.approval_id

    await restarted_database.close()


@pytest.mark.asyncio
async def test_released_durable_reservation_can_be_re_reserved_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-reservation-retry.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(database),
    )
    plan = _plan()
    grant = store.issue(plan, approved_by="operator", operator_token="operator")
    store.reserve(grant.approval_id, plan, reservation_id="bundle-failed")
    store.release_reservation("bundle-failed")
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restarted_store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(restarted_database),
    )

    assert (
        restarted_store.reserve(
            grant.approval_id,
            plan,
            reservation_id="bundle-retry",
        )
        == grant
    )
    restarted_store.commit_reservation("bundle-retry")
    with pytest.raises(DomainError, match="consumed"):
        restarted_store.consume(grant.approval_id, plan)

    await restarted_database.close()
