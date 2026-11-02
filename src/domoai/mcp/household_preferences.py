"""Semantic MCP surface over the single authorized application service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.household_preferences import HouseholdPreferenceService
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.household_preferences import (
    PreferenceHistoryRead,
    PreferenceModel,
    PreferenceMutation,
    PreferenceRead,
    PreferenceRestore,
    PreferenceSlotMutation,
)
from domoai.domain.models import AuthorityContext
from domoai.mcp.auth import current_access_token, current_authority, require_client_scope
from domoai.mcp.errors import error_envelope
from domoai.mcp.request_context import with_request_principal


def register_household_preference_tools(
    server: FastMCP, service: HouseholdPreferenceService
) -> None:
    async def invoke[Request: PreferenceModel](
        method: Callable[[Request, AuthorityContext], Awaitable[PreferenceModel]],
        request: Request,
        scope: str,
    ) -> dict[str, Any]:
        try:
            require_client_scope(current_access_token(), scope)
            authority = current_authority() or service.policy.local_context()
            return (await method(request, authority)).model_dump(mode="json")
        except (DomainError, ValidationError) as error:
            return error_envelope(error)
        except Exception:
            return error_envelope(
                DomainError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    "Preference storage is unavailable",
                    retryable=True,
                )
            )

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    @server.tool(annotations=read, structured_output=True)
    @with_request_principal
    async def get_household_preferences(request: PreferenceRead) -> dict[str, Any]:
        """Read household preferences; this does not dispatch or approve effects."""
        return await invoke(service.read, request, "read")

    @server.tool(annotations=mutate, structured_output=True)
    @with_request_principal
    async def set_household_preference(request: PreferenceMutation) -> dict[str, Any]:
        """Set typed intent with an expected profile version and idempotency key."""
        return await invoke(service.set, request, "mutate")

    @server.tool(annotations=mutate, structured_output=True)
    @with_request_principal
    async def delete_household_preference(request: PreferenceSlotMutation) -> dict[str, Any]:
        """Remove an effective slot while retaining restricted history."""
        return await invoke(service.delete, request, "mutate")

    @server.tool(annotations=mutate, structured_output=True)
    @with_request_principal
    async def restore_household_preference(request: PreferenceRestore) -> dict[str, Any]:
        """Restore retained history as a new CAS transition with explicit validity."""
        return await invoke(service.restore, request, "mutate")

    @server.tool(annotations=read, structured_output=True)
    @with_request_principal
    async def get_household_preference_history(request: PreferenceHistoryRead) -> dict[str, Any]:
        """Read bounded preference history with owner or service authority."""
        return await invoke(service.history, request, "read")

    @server.tool(annotations=mutate, structured_output=True)
    @with_request_principal
    async def erase_household_preference(request: PreferenceSlotMutation) -> dict[str, Any]:
        """Erase retained preference payloads; existing backups remain separate copies."""
        return await invoke(service.erase, request, "mutate")
