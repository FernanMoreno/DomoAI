import asyncio
import json
from pathlib import Path

from domoai.application.authority import AuthorityPolicy
from domoai.domain.models import AuthorityContext, Command, Plan, PrincipalRole
from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.token_lifecycle import TokenFileManager


def test_verified_token_claims_replace_caller_supplied_plan_authority(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    issued = TokenFileManager(token_file).rotate(
        "planner-a",
        scopes=["read", "plan"],
        tenant_id="tenant-a",
        household_ids=["home-a", "home-b"],
        device_ids=["light.one"],
    )
    token = asyncio.run(StaticBearerTokenVerifier.from_file(token_file).verify_token(issued.token))
    assert token is not None

    caller_declared = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="attacker",
        roles=[PrincipalRole.OWNER],
    )
    plan = Plan(
        id="contract-plan",
        authority=caller_declared,
        commands=[
            Command(
                id="contract-command",
                device_id="light.one",
                command="turn_on",
                idempotency_key="contract-intent",
            )
        ],
    )
    claims = token.claims["authority"]
    bound = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
        plan, AuthorityContext.model_validate(claims)
    )

    assert bound.authority.principal_id == "planner-a"
    assert bound.authority.roles == [PrincipalRole.PLANNER]
    assert bound.authority.tenant_id == "tenant-a"
    assert bound.authority.household_ids == ["home-a", "home-b"]
    import pytest

    from domoai.domain.errors import DomainError

    foreign = plan.model_copy(
        update={"authority": caller_declared.model_copy(update={"tenant_id": "attacker-tenant"})}
    )
    original = foreign.model_dump(mode="json")
    with pytest.raises(DomainError):
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").bind_plan(
            foreign, AuthorityContext.model_validate(claims)
        )
    assert foreign.model_dump(mode="json") == original


def test_v1_contract_serializes_authority_on_plan_and_does_not_serialize_bearer() -> None:
    payload = Plan(
        id="schema-plan",
        commands=[
            Command(
                id="schema-command",
                device_id="light.one",
                command="turn_on",
                idempotency_key="schema-intent",
            )
        ],
    ).model_dump(mode="json")

    serialized = json.dumps(payload, sort_keys=True)
    assert "authority" in payload
    assert payload["authority"]["household_id"] == "default"
    assert "token" not in serialized


def test_device_operator_cannot_administer_connections() -> None:
    import httpx
    from mcp.server.auth.provider import AccessToken

    from domoai.management.api import create_management_app
    from tests.integration.test_management_api import StubManager

    async def scenario() -> None:
        manager = StubManager()
        token = AccessToken(
            token="restricted-token",
            client_id="resident",
            scopes=["mutate"],
            claims={
                "authority": AuthorityContext(
                    tenant_id="tenant-a",
                    household_id="home-a",
                    household_ids=["home-a"],
                    principal_id="resident",
                    roles=[PrincipalRole.OPERATOR],
                    device_ids=["light.one"],
                ).model_dump(mode="json")
            },
        )

        async def verify(value: str) -> AccessToken | None:
            return token if value == token.token else None

        app = create_management_app(
            manager, auth_verifier=verify, authority_policy=AuthorityPolicy("tenant-a", "home-a")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post(
                f"/api/v1/connections/{manager.profile.id}/disable",
                headers={"Authorization": "Bearer restricted-token"},
            )
        assert response.status_code == 403
        assert manager.mutation_calls == 0

    asyncio.run(scenario())
