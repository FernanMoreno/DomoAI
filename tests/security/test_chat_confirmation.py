"""T011-C8 RED: human confirmation through the MCP client chat (elicitation).

Freeze: .superpowers/sdd/tasks/p04-t011-c8-freeze.md (R1, R2, R3).
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitResult
from tests.contract.test_domotics_mcp_contract import (
    OPERATOR_TOKEN,
    _validated_plan_requiring_confirmation,
    build_confirmation_required_context,
    structured,
)

from domoai.domain.models import Plan
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.runtime.approval_store import ApprovalStore

pytestmark = pytest.mark.asyncio

_CODE = re.compile(r"Código de verificación: (\d{4})")


class Human:
    """A client callback standing in for the person (or for a careless auto-answering client)."""

    def __init__(
        self,
        action: str = "accept",
        approve: bool = True,
        code: str | None = None,
        delay: float = 0.0,
        hook: Any = None,
    ) -> None:
        self.action, self.approve, self.code, self.delay, self.hook = (
            action,
            approve,
            code,
            delay,
            hook,
        )
        self.messages: list[str] = []

    async def __call__(self, _ctx: Any, params: Any) -> ElicitResult:
        self.messages.append(params.message)
        if self.hook is not None:
            await self.hook()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.action != "accept":
            return ElicitResult(action=cast(Any, self.action))
        shown = _CODE.search(params.message)
        code = self.code if self.code is not None else (shown.group(1) if shown else "0000")
        return ElicitResult(action="accept", content={"aprobar": self.approve, "codigo": code})


@asynccontextmanager
async def client(server: Any, human: Human | None = None, *, token: bool = True):
    user = (
        AuthenticatedUser(
            AccessToken(
                token="opaque", client_id="client-a", scopes=["read", "mutate"], expires_at=None
            )
        )
        if token
        else None
    )
    reset = auth_context_var.set(user)
    try:
        async with create_connected_server_and_client_session(
            server, elicitation_callback=human
        ) as session:
            yield session
    finally:
        auth_context_var.reset(reset)


async def setup(*, chat: bool = True) -> tuple[DomoticsMcpContext, Any, dict[str, Any]]:
    context = await build_confirmation_required_context(approval_store=ApprovalStore())
    context.chat_confirmation = chat  # type: ignore[attr-defined]
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)
    return context, server, validated


def args(validated: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "plan_id": validated["plan"]["id"],
        "validation_digest": validated["validation"]["digest"],
        **extra,
    }


def reason(result: dict[str, Any]) -> str:
    assert result["error"]["code"] == "human_confirmation_unavailable", result
    return str(result["error"]["details"]["reason"])


async def executed(session: Any, validated: dict[str, Any], approval_id: str) -> dict[str, Any]:
    return structured(
        await session.call_tool("execute_plan", args(validated, approval_id=approval_id))
    )


async def test_an_approved_confirmation_issues_a_grant_that_executes_the_plan() -> None:
    context, server, validated = await setup()
    human = Human()
    adapter = context.facade.executor.adapter
    async with client(server, human) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
        assert result["approval_id"]
        message = human.messages[0]
        # An immediate plan still gets a Spanish summary that names every effect.
        assert "ejecución inmediata" in message and "50" in message
        assert "set_brightness" in message
        run = await executed(session, validated, result["approval_id"])
        assert "error" not in run, run
    assert adapter.calls, "the approved plan must reach the adapter"  # type: ignore[attr-defined]


async def test_the_issued_grant_is_attributed_single_use_and_bounded() -> None:
    context, server, validated = await setup()
    async with client(server, Human()) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
        grant = context.approval_store.get_grant(result["approval_id"])
        assert grant is not None
        assert grant.approved_by == "chat:client-a"
        assert grant.authentication_context == "mcp_elicitation"
        assert grant.plan_id == validated["plan"]["id"]
        assert grant.expires_at > context.approval_store.now()
        first = await executed(session, validated, result["approval_id"])
        assert "error" not in first, first
        assert context.approval_store.is_consumed(result["approval_id"])
        calls = len(context.facade.executor.adapter.calls)  # type: ignore[attr-defined]
        await executed(session, validated, result["approval_id"])
        # A replay must never reach the adapter a second time.
        assert len(context.facade.executor.adapter.calls) == calls  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "human,expected",
    [
        (Human(approve=False), "declined"),
        (Human(code="0000"), "declined"),
        (Human(action="decline"), "declined"),
        (Human(action="cancel"), "cancelled"),
    ],
)
async def test_anything_but_a_correct_yes_issues_nothing(human: Human, expected: str) -> None:
    context, server, validated = await setup()
    adapter = context.facade.executor.adapter
    async with client(server, human) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
        assert "approval_id" not in result and reason(result) == expected
        run = await executed(session, validated, "caller-fabricated-approval")
        assert run["error"]["code"] == "approval_required"
    assert adapter.calls == []  # type: ignore[attr-defined]


async def test_a_client_without_elicitation_fails_closed() -> None:
    _, server, validated = await setup()
    async with client(server, None) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert "approval_id" not in result and reason(result) == "unavailable"


async def test_without_an_authenticated_client_nothing_is_asked_or_issued() -> None:
    _, server, validated = await setup()
    human = Human()
    async with client(server, human, token=False) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert "approval_id" not in result and reason(result) == "unavailable"
    assert human.messages == []


@pytest.mark.parametrize("extra", [{"operator_token": OPERATOR_TOKEN}, {"bundle_digest": "x"}])
async def test_legacy_inputs_are_refused_before_anything_is_asked(extra: dict[str, Any]) -> None:
    _, server, validated = await setup()
    human = Human()
    async with client(server, human) as session:
        result = structured(await session.call_tool("request_approval", args(validated, **extra)))
    assert "approval_id" not in result
    assert result["error"]["code"] == "approval_assertion_required"
    assert human.messages == []


async def test_recurrence_digest_is_refused_in_chat_mode() -> None:
    _, server, validated = await setup()
    human = Human()
    async with client(server, human) as session:
        result = structured(
            await session.call_tool("request_approval", args(validated, recurrence_digest="y"))
        )
    assert result["error"]["code"] == "approval_assertion_required" and human.messages == []


async def test_a_plan_mutated_during_the_confirmation_is_never_approved() -> None:
    context, server, validated = await setup()
    plan_id = validated["plan"]["id"]

    async def mutate() -> None:
        plan = context.plans[plan_id]
        command = plan.commands[0].model_copy(update={"value": 60})
        context.plans[plan_id] = plan.model_copy(update={"commands": [command]})

    human = Human(hook=mutate)
    async with client(server, human) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert human.messages, "the person must have been asked about the original plan"
    assert "approval_id" not in result and "error" in result


async def test_the_verification_code_is_fresh_and_never_leaks_into_errors() -> None:
    _, server, validated = await setup()
    first, second = Human(approve=False), Human(approve=False)
    async with client(server, first) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    code = _CODE.search(first.messages[0]).group(1)  # type: ignore[union-attr]
    assert code not in str(result)
    async with client(server, second) as session:
        # A different confirmation (cooldown aside) must never reuse the previous code.
        await asyncio.sleep(0)
        await session.call_tool("request_approval", args(validated))
    if second.messages:
        assert _CODE.search(second.messages[0]).group(1) != code  # type: ignore[union-attr]


async def test_a_timeout_releases_the_lock_and_is_reported() -> None:
    context, server, validated = await setup()
    context.chat_confirmation_timeout_seconds = 0.2  # type: ignore[attr-defined]
    human = Human(delay=1.0)
    async with client(server, human) as session:
        slow = structured(await session.call_tool("request_approval", args(validated)))
        assert "approval_id" not in slow and reason(slow) == "timeout"
        human.delay = 0.0
        ok = structured(await session.call_tool("request_approval", args(validated)))
        assert ok["approval_id"]


async def test_only_one_confirmation_is_in_flight_per_household() -> None:
    _, server, validated = await setup()
    human = Human(delay=0.5)
    async with client(server, human) as session:
        results = await asyncio.gather(
            session.call_tool("request_approval", args(validated)),
            session.call_tool("request_approval", args(validated)),
        )
    payloads = [structured(result) for result in results]
    granted = [item for item in payloads if "approval_id" in item]
    refused = [item for item in payloads if "approval_id" not in item]
    assert len(granted) == 1 and len(refused) == 1 and reason(refused[0]) == "busy"
    assert len(human.messages) == 1


async def test_a_declined_confirmation_starts_a_cooldown() -> None:
    _, server, validated = await setup()
    human = Human(action="decline")
    async with client(server, human) as session:
        first = structured(await session.call_tool("request_approval", args(validated)))
        second = structured(await session.call_tool("request_approval", args(validated)))
    assert reason(first) == "declined" and reason(second) == "cooldown"
    assert len(human.messages) == 1


async def test_chat_mode_refuses_other_approval_providers() -> None:
    context = await build_confirmation_required_context()
    context.chat_confirmation = True  # type: ignore[attr-defined]
    context.operator_approval_assertion_provider = lambda *_: None
    with pytest.raises(ValueError):
        create_domotics_server(context)


async def test_the_tool_catalog_only_changes_with_the_flag() -> None:
    _, off, _ = await setup(chat=False)
    _, on, _ = await setup(chat=True)
    async with client(off, None) as session:
        names_off = {tool.name for tool in (await session.list_tools()).tools}
    async with client(on, None) as session:
        names_on = {tool.name for tool in (await session.list_tools()).tools}
    assert "revoke_approval" not in names_off
    assert names_on - names_off == {"revoke_approval"}
    assert names_off - names_on == set()


async def test_with_the_flag_off_the_legacy_behaviour_is_unchanged() -> None:
    _, server, validated = await setup(chat=False)
    human = Human()
    async with client(server, human) as session:
        result = structured(
            await session.call_tool("request_approval", args(validated, operator_token="nope"))
        )
    assert result["error"]["code"] == "operator_authentication_failed"
    assert human.messages == []


# --- the summary renderer ----------------------------------------------------------------------


def _summary_inputs(context: DomoticsMcpContext, validated: dict[str, Any]):
    plan = Plan.model_validate(validated["plan"])
    return plan, list(context.registry.devices), datetime.now(UTC)


async def test_the_summary_names_each_effect_and_the_schedule() -> None:
    from domoai.mcp.chat_confirmation import render_chat_summary

    context, _, validated = await setup()
    plan, devices, now = _summary_inputs(context, validated)
    text = render_chat_summary(plan, devices, now, "Europe/Madrid")
    assert "ejecución inmediata" in text and "set_brightness" in text and "50" in text
    scheduled = plan.model_copy(update={"execute_at": now + timedelta(hours=1)})
    assert "programada" in render_chat_summary(scheduled, devices, now, "Europe/Madrid")


async def test_the_summary_refuses_to_hide_or_alter_an_effect() -> None:
    from domoai.mcp.chat_confirmation import ChatSummaryUnavailable, render_chat_summary

    context, _, validated = await setup()
    plan, devices, now = _summary_inputs(context, validated)
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(plan, [], now, "Europe/Madrid")  # unknown device
    bidi = [device.model_copy(update={"name": "‮luz"}) for device in devices]
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(plan, bidi, now, "Europe/Madrid")
    long_name = [device.model_copy(update={"name": "x" * 121}) for device in devices]
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(plan, long_name, now, "Europe/Madrid")
    many = plan.model_copy(
        update={
            "commands": [
                plan.commands[0].model_copy(update={"id": f"c{index}"}) for index in range(11)
            ]
        }
    )
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(many, devices, now, "Europe/Madrid")
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(plan, devices, now, "Not/AZone")


# --- revoke_approval ---------------------------------------------------------------------------


@asynccontextmanager
async def durable(tmp_path: Path):
    from domoai.persistence.repositories import ApprovalGrantRepository
    from domoai.persistence.sqlite import SQLiteDatabase

    database = SQLiteDatabase(tmp_path / "chat.sqlite3")
    await database.initialize()
    try:
        yield ApprovalGrantRepository(database)
    finally:
        await database.close()


async def test_a_confirmed_revocation_blocks_the_grant(tmp_path: Path) -> None:
    from domoai.domain.errors import DomainError, ErrorCode

    async with durable(tmp_path) as repository:
        context = await build_confirmation_required_context(
            approval_store=ApprovalStore(persistence=repository)
        )
        context.chat_confirmation = True  # type: ignore[attr-defined]
        server = create_domotics_server(context)
        validated = await _validated_plan_requiring_confirmation(server, context)
        human = Human()
        async with client(server, human) as session:
            approved = structured(await session.call_tool("request_approval", args(validated)))
            revoked = structured(
                await session.call_tool("revoke_approval", {"approval_id": approved["approval_id"]})
            )
            assert revoked["status"] == "revocation_pending"
            assert revoked["approval_id"] == approved["approval_id"]
            with pytest.raises(DomainError) as caught:
                repository.assert_consent_active_sync(approved["approval_id"])
            assert caught.value.code == ErrorCode.CONSENT_REVOKED
            asked = len(human.messages)
            again = structured(
                await session.call_tool("revoke_approval", {"approval_id": approved["approval_id"]})
            )
            assert "error" in again and len(human.messages) == asked, "no dialog for a dead grant"


async def test_a_declined_revocation_changes_nothing(tmp_path: Path) -> None:
    async with durable(tmp_path) as repository:
        context = await build_confirmation_required_context(
            approval_store=ApprovalStore(persistence=repository)
        )
        context.chat_confirmation = True  # type: ignore[attr-defined]
        server = create_domotics_server(context)
        validated = await _validated_plan_requiring_confirmation(server, context)
        approver = Human()
        async with client(server, approver) as session:
            approved = structured(await session.call_tool("request_approval", args(validated)))
        async with client(server, Human(action="decline")) as session:
            refused = structured(
                await session.call_tool("revoke_approval", {"approval_id": approved["approval_id"]})
            )
        assert reason(refused) == "declined"
        repository.assert_consent_active_sync(approved["approval_id"])


async def test_revocation_needs_a_durable_store_and_a_known_grant() -> None:
    _, server, validated = await setup()
    human = Human()
    async with client(server, human) as session:
        approved = structured(await session.call_tool("request_approval", args(validated)))
        unavailable = structured(
            await session.call_tool("revoke_approval", {"approval_id": approved["approval_id"]})
        )
        unknown = structured(await session.call_tool("revoke_approval", {"approval_id": "nope"}))
    assert "error" in unavailable and "error" in unknown


# --- settings ----------------------------------------------------------------------------------


async def test_chat_confirmation_requires_a_transport_that_can_carry_elicitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domoai.config.settings import Settings

    assert Settings(mcp_chat_confirmation=True).mcp_json_response is False
    with pytest.raises(ValueError):
        Settings(mcp_chat_confirmation=True, mcp_json_response=True)
    assert Settings().mcp_chat_confirmation is False and Settings().mcp_json_response is True
    monkeypatch.setenv("DOMOAI_MCP_CHAT_CONFIRMATION", "true")
    monkeypatch.delenv("DOMOAI_MCP_JSON_RESPONSE", raising=False)
    derived = Settings.from_environment()
    assert derived.mcp_chat_confirmation is True and derived.mcp_json_response is False
    monkeypatch.setenv("DOMOAI_MCP_JSON_RESPONSE", "true")
    with pytest.raises(ValueError):
        Settings.from_environment()
    monkeypatch.delenv("DOMOAI_MCP_JSON_RESPONSE")
    with pytest.raises(ValueError):
        Settings(mcp_chat_confirmation=True, mcp_chat_confirmation_timeout_seconds=5)
