import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP

from domoai.domain.errors import DomainError, ErrorCode
from domoai.mcp.domotics_server import _authorize_mutation
from domoai.mcp.request_context import with_request_principal
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import current_execution_principal


def test_valid_client_token_resolves_to_server_owned_identity(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"clients": [{
        "client_id": "codex",
        "token_hash": hashlib.sha256(b"codex-secret").hexdigest(),
        "scopes": ["home:read", "home:plan"],
    }]}))
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    result = asyncio.run(verifier.verify_token("codex-secret"))

    assert isinstance(result, AccessToken)
    assert result.client_id == "codex"
    assert result.scopes == ["home:read", "home:plan"]


def test_invalid_client_token_is_rejected_without_echoing_secret(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"clients": [{
        "client_id": "claude",
        "token_hash": hashlib.sha256(b"claude-secret").hexdigest(),
        "scopes": ["home:read"],
    }]}))
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    result = asyncio.run(verifier.verify_token("wrong-secret"))

    assert result is None
    assert "claude-secret" not in repr(verifier)


def test_client_token_file_rejects_non_hex_sha256_hash(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {"clients": [{"client_id": "bad", "token_hash": "g" * 64}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid MCP client token file"):
        StaticBearerTokenVerifier.from_file(token_file)


def test_client_scope_is_required_for_physical_mutations() -> None:
    from domoai.mcp.auth import require_client_scope

    read_only = AccessToken(token="opaque", client_id="codex", scopes=["read"])

    with pytest.raises(DomainError) as excinfo:
        require_client_scope(read_only, "mutate")

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SCOPE
    require_client_scope(AccessToken(token="opaque", client_id="codex", scopes=["*"]), "mutate")


def test_expired_and_disabled_client_tokens_are_rejected(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "expired",
                        "token_hash": hashlib.sha256(b"expired-secret").hexdigest(),
                        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    },
                    {
                        "client_id": "disabled",
                        "token_hash": hashlib.sha256(b"disabled-secret").hexdigest(),
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    assert asyncio.run(verifier.verify_token("expired-secret")) is None
    assert asyncio.run(verifier.verify_token("disabled-secret")) is None


@pytest.mark.asyncio
async def test_authenticated_tool_identity_is_scoped_without_leaking_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    server = FastMCP("identity-test")
    seen: list[str] = []

    @server.tool()
    @with_request_principal
    async def observe_identity() -> str:
        seen.append(current_execution_principal())
        return current_execution_principal()

    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: AccessToken(token="opaque", client_id="codex", scopes=["read"]),
    )

    result = await server.call_tool("observe_identity", {})

    assert result[1]["result"] == "codex"
    assert seen == ["codex"]
    assert current_execution_principal() == "local"


def test_denied_mutation_is_audited_without_persisting_the_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import mcp.server.auth.middleware.auth_context as auth_context

    audit = AuditLog()
    context = SimpleNamespace(
        facade=SimpleNamespace(plan_service=SimpleNamespace(audit=audit))
    )
    secret = "read-only-secret"
    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: AccessToken(token=secret, client_id="claude", scopes=["read"]),
    )

    with pytest.raises(DomainError) as excinfo:
        _authorize_mutation(context, operation="execute_plan", subject_id="plan-1")

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SCOPE
    event = audit.events[-1]
    assert event.event_type == "mcp_authorization_rejected"
    assert event.actor == "agent:claude"
    assert secret not in event.model_dump_json()
