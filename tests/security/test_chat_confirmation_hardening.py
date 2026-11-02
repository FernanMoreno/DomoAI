"""Regressions for the C8 authority, display and cancellation boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.types import ElicitResult
from tests.contract.test_domotics_mcp_contract import structured
from tests.security.test_chat_confirmation import Human, args, client, setup

from domoai.mcp.chat_confirmation import ChatSummaryUnavailable, render_chat_summary
from domoai.mcp.domotics_server import _chat_dialog

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "content",
    [
        None,
        {"aprobar": "true", "codigo": "1234"},
        {"aprobar": True},
        {"aprobar": True, "codigo": "1234", "extra": "ignored?"},
    ],
)
async def test_malformed_client_content_never_issues_a_grant(content: Any) -> None:
    context, server, validated = await setup()

    async def malformed(_ctx: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content=content)

    async with client(server, malformed) as session:  # type: ignore[arg-type]
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert "approval_id" not in result
    assert result["error"]["code"] == "human_confirmation_unavailable"
    assert context.approval_store._grants == {}


async def test_undisplayed_command_mutation_invalidates_the_confirmation() -> None:
    context, server, validated = await setup()

    async def mutate() -> None:
        plan = context.plans[validated["plan"]["id"]]
        command = plan.commands[0].model_copy(update={"idempotency_key": "changed-intent"})
        context.plans[plan.id] = plan.model_copy(update={"commands": [command]})

    human = Human(hook=mutate)
    async with client(server, human) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert human.messages
    assert "approval_id" not in result and "error" in result
    assert context.approval_store._grants == {}


async def test_summary_uses_the_declared_command_capability_unit() -> None:
    context, _, validated = await setup()
    plan = context.plans[validated["plan"]["id"]]
    devices = []
    for device in context.registry.devices:
        capabilities = [
            capability.model_copy(update={"name": "unrelated-name", "unit": "%"})
            if plan.commands[0].command in capability.commands
            else capability
            for capability in device.capabilities
        ]
        devices.append(device.model_copy(update={"capabilities": capabilities}))
    summary = render_chat_summary(plan, devices, datetime.now(UTC), "Europe/Madrid")
    assert '"%"' in summary


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
async def test_summary_rejects_unicode_line_and_paragraph_separators(separator: str) -> None:
    context, _, validated = await setup()
    plan = context.plans[validated["plan"]["id"]]
    devices = [
        device.model_copy(update={"name": f"Lamp{separator}hidden effect"})
        for device in context.registry.devices
    ]
    with pytest.raises(ChatSummaryUnavailable):
        render_chat_summary(plan, devices, datetime.now(UTC), "Europe/Madrid")


async def test_cancelled_dialogue_is_audited_as_cancelled_and_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, _ = await setup()
    started = asyncio.Event()
    outcomes: list[str] = []

    async def wait_forever(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    def audit(**event: Any) -> None:
        outcomes.append(event["payload"]["outcome"])

    monkeypatch.setattr(context.chat_confirmation_gate, "confirm", wait_forever)
    monkeypatch.setattr(context.facade.plan_service.audit, "append", audit)
    scope = ("default", "default")

    async def dialog() -> None:
        async with context.chat_confirmation_gate.hold(scope):
            await _chat_dialog(context, None, "summary", scope=scope, subject_id="plan")

    task = asyncio.create_task(dialog())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outcomes == ["cancelled"]
    async with context.chat_confirmation_gate.hold(scope):
        pass
    assert context.approval_store._grants == {}


async def test_audit_sink_failure_after_dialogue_does_not_hide_an_issued_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, server, validated = await setup()

    async def fail_sink_after_authorization() -> None:
        def broken(**_event: Any) -> None:
            raise OSError("audit sink unavailable")

        monkeypatch.setattr(context.facade.plan_service.audit, "append", broken)

    async with client(server, Human(hook=fail_sink_after_authorization)) as session:
        result = structured(await session.call_tool("request_approval", args(validated)))
    assert result["approval_id"]
    assert context.approval_store.get_grant(result["approval_id"]) is not None


async def test_scoped_client_cannot_revoke_a_grant_whose_plan_is_missing(tmp_path: Any) -> None:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from mcp.shared.memory import create_connected_server_and_client_session
    from tests.contract.test_domotics_mcp_contract import (
        _validated_plan_requiring_confirmation,
        build_confirmation_required_context,
    )
    from tests.security.test_chat_confirmation import durable

    from domoai.domain.models import AuthorityContext, PrincipalRole
    from domoai.mcp.domotics_server import create_domotics_server
    from domoai.runtime.approval_store import ApprovalStore

    async with durable(tmp_path) as repository:
        context = await build_confirmation_required_context(
            approval_store=ApprovalStore(persistence=repository)
        )
        context.chat_confirmation = True
        server = create_domotics_server(context)
        validated = await _validated_plan_requiring_confirmation(server, context)
        async with client(server, Human()) as session:
            approved = structured(await session.call_tool("request_approval", args(validated)))
        context.plans.clear()
        authority = AuthorityContext(
            principal_id="scoped", roles=[PrincipalRole.OPERATOR], device_ids=["one-device"]
        )
        token = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="opaque",
                    client_id="scoped",
                    scopes=["read", "mutate"],
                    claims={"authority": authority.model_dump(mode="json")},
                )
            )
        )
        human = Human()
        try:
            async with create_connected_server_and_client_session(
                server, elicitation_callback=human
            ) as session:
                refused = structured(
                    await session.call_tool(
                        "revoke_approval", {"approval_id": approved["approval_id"]}
                    )
                )
        finally:
            auth_context_var.reset(token)
        assert refused["error"]["code"] == "insufficient_scope"
        assert human.messages == []
        repository.assert_consent_active_sync(approved["approval_id"])
