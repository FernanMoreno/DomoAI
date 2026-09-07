from datetime import UTC, datetime, timedelta

from domoai.application.authority import AuthorityPolicy
from domoai.application.federation import FederationBoundary
from domoai.domain.federation import (
    FederationIntent,
    FederationIntentStatus,
    sign_federation_intent,
)

SECRET = b"federation-test-secret"
TEST_CLOCK = type(
    "Clock",
    (),
    {"now": lambda _self: datetime(2026, 9, 4, 10, 30, tzinfo=UTC)},
)()


def _intent(**updates) -> FederationIntent:
    base = FederationIntent(
        intent_id="intent-1",
        source_tenant_id="tenant-a",
        source_household_id="home-source",
        target_tenant_id="tenant-a",
        target_household_id="home-target",
        principal_id="operator-source",
        operation="execute_plan",
        plan_digest="sha256:plan",
        idempotency_key="idempotency-1",
        issued_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
        expires_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
        signature="0" * 64,
    )
    return sign_federation_intent(base.model_copy(update=updates), SECRET)


def test_valid_federated_proposal_is_accepted_but_not_completed_automatically() -> None:
    boundary = FederationBoundary(
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-target"),
        shared_secret=SECRET,
        clock=TEST_CLOCK,
    )

    result = boundary.submit(_intent())

    assert result.status is FederationIntentStatus.ACCEPTED
    assert boundary.complete("intent-1").status is FederationIntentStatus.COMPLETED


def test_invalid_or_foreign_federated_proposals_are_rejected() -> None:
    boundary = FederationBoundary(
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-target"),
        shared_secret=SECRET,
        clock=TEST_CLOCK,
    )

    assert (
        boundary.submit(_intent().model_copy(update={"signature": "1" * 64})).status
        is FederationIntentStatus.REJECTED
    )
    assert (
        boundary.submit(_intent(target_household_id="home-other")).status
        is FederationIntentStatus.REJECTED
    )
    assert (
        boundary.submit(_intent(expires_at=TEST_CLOCK.now() - timedelta(minutes=1))).status
        is FederationIntentStatus.REJECTED
    )


def test_federated_idempotency_does_not_replay_a_physical_operation() -> None:
    boundary = FederationBoundary(
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-target"),
        shared_secret=SECRET,
        clock=TEST_CLOCK,
    )

    first = boundary.submit(_intent())
    duplicate = boundary.submit(_intent())
    conflict = boundary.submit(_intent(intent_id="intent-2"))

    assert first.status is FederationIntentStatus.ACCEPTED
    assert duplicate.status is FederationIntentStatus.ACCEPTED
    assert conflict.status is FederationIntentStatus.REJECTED


def test_federated_replay_cannot_change_a_signed_intent_body() -> None:
    boundary = FederationBoundary(
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-target"),
        shared_secret=SECRET,
        clock=TEST_CLOCK,
    )

    assert boundary.submit(_intent()).status is FederationIntentStatus.ACCEPTED
    changed = _intent(plan_digest="sha256:other")

    assert boundary.submit(changed).status is FederationIntentStatus.REJECTED
