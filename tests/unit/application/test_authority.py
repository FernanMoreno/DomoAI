import pytest

from domoai.application.authority import AuthorityPolicy
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AuthorityContext, Command, Plan, PrincipalRole


def _plan() -> Plan:
    return Plan(
        id="plan-authority",
        commands=[
            Command(
                id="command-authority",
                device_id="light.one",
                command="turn_on",
                idempotency_key="authority-intent",
            )
        ],
    )


def test_viewer_can_read_only_the_granted_household() -> None:
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    viewer = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="viewer",
        roles=[PrincipalRole.VIEWER],
    )

    policy.authorize(viewer, operation="get_state", device_ids=("light.one",))
    with pytest.raises(DomainError) as error:
        policy.authorize(viewer, operation="execute_plan")
    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_operator_plan_is_bound_to_verified_principal_not_caller_fields() -> None:
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    operator = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a", "home-b"],
        principal_id="operator",
        roles=[PrincipalRole.OPERATOR],
    )

    bound = policy.bind_plan(_plan(), operator, operation="prepare_plan")

    assert bound.authority.principal_id == "operator"
    assert bound.authority.household_id == "home-a"
    assert bound.authority.roles == [PrincipalRole.OPERATOR]


def test_target_household_outside_acl_is_rejected() -> None:
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    operator = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="operator",
        roles=[PrincipalRole.OPERATOR],
    )
    foreign = _plan().model_copy(
        update={
            "authority": AuthorityContext(
                tenant_id="tenant-a",
                household_id="home-b",
                household_ids=["home-b"],
            )
        }
    )

    with pytest.raises(DomainError) as error:
        policy.bind_plan(foreign, operator)
    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_second_household_in_token_cannot_target_this_runtime() -> None:
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    operator = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a", "home-b"],
        principal_id="operator",
        roles=[PrincipalRole.OPERATOR],
    )
    foreign = _plan().model_copy(
        update={
            "authority": AuthorityContext(
                tenant_id="tenant-a",
                household_id="home-b",
                household_ids=["home-b"],
            )
        }
    )

    with pytest.raises(DomainError) as error:
        policy.bind_plan(foreign, operator)
    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE
