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
    assert bound.authority.household_ids == ["home-a", "home-b"]


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
