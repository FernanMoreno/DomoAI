from mcp.server.auth.provider import AccessToken

from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.management.auth import (
    ManagementPrincipal,
    authority_operation_for_scope,
    required_management_scope,
)


def test_management_principal_preserves_verified_access_token_claims() -> None:
    authority = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="client-a",
        roles=[PrincipalRole.PLANNER],
    )
    token = AccessToken(
        token="management-secret",
        client_id="client-a",
        scopes=["read", "plan"],
        claims={"authority": authority.model_dump(mode="json")},
    )

    principal = ManagementPrincipal.from_access_token(token)

    assert principal.client_id == "client-a"
    assert principal.scopes == frozenset({"read", "plan"})
    assert principal.authority == authority
    assert principal.allows("read") is True
    assert principal.allows("manage") is False
    assert "management-secret" not in repr(principal)


def test_management_route_policy_is_explicit_and_fails_closed() -> None:
    read_routes = (
        ("GET", "/api/v1/connections"),
        ("POST", "/api/v1/connections/discovery"),
        ("POST", "/api/v1/mqtt/discovery/preview"),
        ("POST", "/api/v1/knx/ets/preview"),
        ("POST", "/api/v1/home-assistant/oauth/discover"),
        ("POST", "/api/v1/connections/11111111-1111-1111-1111-111111111111/validate"),
    )
    for method, path in read_routes:
        assert required_management_scope(method, path) == "read"

    assert required_management_scope("POST", "/api/v1/connections") == "manage"
    assert required_management_scope("PATCH", "/api/v1/connections/one") == "manage"
    assert required_management_scope("DELETE", "/api/v1/connections/one") == "manage"
    assert required_management_scope("POST", "/api/v1/unknown") == "manage"
    assert authority_operation_for_scope("read") == "list"
    assert authority_operation_for_scope("manage") == "manage_connections"
