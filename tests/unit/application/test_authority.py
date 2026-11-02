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


def _restricted_operator() -> AuthorityContext:
    return AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="verified-operator",
        roles=[PrincipalRole.OPERATOR],
        area_ids=["area-a"],
        device_ids=["light.one"],
        capabilities=["turn_on"],
        operations=["prepare_plan"],
    )


def test_default_plan_inherits_every_verified_resource_scope() -> None:
    verified = _restricted_operator()

    bound = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
        _plan(), verified, device_areas={"light.one": "area-a"}
    )

    assert bound.authority.tenant_id == "tenant-a"
    assert bound.authority.principal_id == "verified-operator"
    assert bound.authority.roles == [PrincipalRole.OPERATOR]
    assert bound.authority.area_ids == ["area-a"]
    assert bound.authority.device_ids == ["light.one"]
    assert bound.authority.capabilities == ["turn_on"]
    assert bound.authority.operations == ["prepare_plan"]


@pytest.mark.parametrize(
    ("field_name", "declared_values"),
    [
        ("area_ids", ["area-a", "area-b"]),
        ("device_ids", ["light.one", "light.two"]),
        ("capabilities", ["turn_on", "turn_off"]),
        ("operations", ["prepare_plan", "execute_plan"]),
    ],
)
def test_plan_cannot_broaden_verified_scope(field_name: str, declared_values: list[str]) -> None:
    declared = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
    ).model_copy(update={field_name: declared_values})
    plan = _plan().model_copy(update={"authority": declared})

    with pytest.raises(DomainError) as error:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            plan, _restricted_operator(), device_areas={"light.one": "area-a"}
        )

    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_plan_cannot_introduce_operation_wildcard() -> None:
    declared = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        operations=["*"]
    )
    plan = _plan().model_copy(update={"authority": declared})

    with pytest.raises(DomainError) as error:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            plan, _restricted_operator(), device_areas={"light.one": "area-a"}
        )

    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_valid_plan_narrowing_is_preserved() -> None:
    declared = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        area_ids=["area-a"],
        device_ids=["light.one"],
        capabilities=["turn_on"],
        operations=["prepare_plan"],
    )
    plan = _plan().model_copy(update={"authority": declared})

    bound = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
        plan, _restricted_operator(), device_areas={"light.one": "area-a"}
    )

    assert bound.authority.area_ids == ["area-a"]
    assert bound.authority.device_ids == ["light.one"]
    assert bound.authority.capabilities == ["turn_on"]
    assert bound.authority.operations == ["prepare_plan"]


def test_area_scoped_binding_fails_closed_without_device_mapping() -> None:
    with pytest.raises(DomainError) as error:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            _plan(), _restricted_operator()
        )

    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_area_scoped_binding_rejects_out_of_area_command() -> None:
    with pytest.raises(DomainError) as error:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            _plan(), _restricted_operator(), device_areas={"light.one": "area-b"}
        )

    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE


def test_explicit_foreign_tenant_same_household_rejected_before_rebinding() -> None:
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="shared-home")
    verified = AuthorityContext(
        tenant_id="tenant-a",
        household_id="shared-home",
        household_ids=["shared-home"],
        principal_id="operator",
        roles=[PrincipalRole.OPERATOR],
    )
    foreign = _plan().model_copy(
        update={
            "authority": AuthorityContext(
                tenant_id="tenant-b", household_id="shared-home", household_ids=["shared-home"]
            )
        }
    )
    with pytest.raises(DomainError) as error:
        policy.bind_plan(foreign, verified)
    assert error.value.code is ErrorCode.INSUFFICIENT_SCOPE
    inherited = policy.bind_plan(_plan(), verified)
    assert inherited.authority.tenant_id == "tenant-a"
