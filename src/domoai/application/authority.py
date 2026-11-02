"""Tenant/household/role authorization at the application boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AuthorityContext, Plan, PrincipalRole

_READ_OPERATIONS = frozenset(
    {
        "discover_devices",
        "get_state",
        "get_history",
        "get_energy_context",
        "list",
        "inspect",
        "preview",
        "get_household_preferences",
    }
)
_PLAN_OPERATIONS = frozenset(
    {"prepare_command", "prepare_plan", "validate_command", "validate_plan"}
)
_RESIDENT_CONSENT_OPERATIONS = frozenset({"review_consent", "approve_consent", "revoke_consent"})
_OPERATOR_OPERATIONS = frozenset(
    {
        "request_approval",
        "execute",
        "schedule",
        "cancel",
        "reschedule",
        "standing_automation",
        "execute_plan",
        "schedule_plan",
        "cancel_scheduled_plan",
        "reschedule_plan",
        "commit_or_schedule_bundle",
        "schedule_recurring_plan",
        "cancel_recurring_schedule",
        "create_local_automation_rule",
        "update_local_automation_rule",
        "set_local_automation_status",
        "set_household_preference",
        "delete_household_preference",
    }
)

_PREFERENCE_ADMIN_OPERATIONS = frozenset({
    "get_household_preference_history",
    "restore_household_preference",
    "erase_household_preference",
})
_PREFERENCE_OPERATIONS = _PREFERENCE_ADMIN_OPERATIONS | {
    "get_household_preferences", "set_household_preference", "delete_household_preference",
}


def roles_from_scopes(scopes: Sequence[str]) -> list[PrincipalRole]:
    """Derive the least role needed by a legacy scope-only token."""

    if "*" in scopes:
        return [PrincipalRole.OWNER]
    if "mutate" in scopes or "home:write" in scopes or "manage" in scopes:
        return [PrincipalRole.OPERATOR]
    if "plan" in scopes or "home:plan" in scopes:
        return [PrincipalRole.PLANNER]
    return [PrincipalRole.VIEWER]


def authority_from_token_claims(
    *, client_id: str, scopes: Sequence[str], claims: Mapping[str, object]
) -> AuthorityContext:
    """Decode verified authority claims without coupling transports together."""

    raw_authority = claims.get("authority")
    if isinstance(raw_authority, dict):
        try:
            return AuthorityContext.model_validate(raw_authority)
        except (TypeError, ValueError):
            pass
    return AuthorityContext(
        principal_id=client_id,
        roles=roles_from_scopes(scopes),
    )


@dataclass(frozen=True)
class AuthorityPolicy:
    """Evaluate a verified principal against one runtime deployment.

    This policy is intentionally local. A token may identify several
    households, but every individual authority entity still stores one target
    household and is checked against this deployment's tenant.
    """

    tenant_id: str = "default"
    household_id: str = "default"

    def local_context(self) -> AuthorityContext:
        if self.tenant_id == "default" and self.household_id == "default":
            return AuthorityContext()
        return AuthorityContext(
            tenant_id=self.tenant_id,
            household_id=self.household_id,
            household_ids=[self.household_id],
            principal_id="local",
            roles=[PrincipalRole.SERVICE],
        )

    def authorize(
        self,
        authority: AuthorityContext,
        *,
        operation: str,
        target_household_id: str | None = None,
        area_id: str | None = None,
        device_ids: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
    ) -> None:
        target_household = self.household_id if target_household_id is None else target_household_id
        if target_household != self.household_id:
            self._deny(operation, "target household is not this deployment")
        if authority.tenant_id != self.tenant_id:
            self._deny(operation, "tenant is not authorized")
        if target_household not in authority.household_ids:
            self._deny(operation, "household is not authorized")
        if operation in _PREFERENCE_OPERATIONS and (
            authority.household_id != target_household
            or authority.area_ids or authority.device_ids or authority.capabilities
        ):
            self._deny(operation, "preferences require whole-household authority")
        if operation == "manage_connections" and (
            authority.area_ids or authority.device_ids or authority.capabilities
        ):
            self._deny(operation, "connection administration requires whole-household authority")
        if authority.area_ids and area_id is not None and area_id not in authority.area_ids:
            self._deny(operation, "area is not authorized")
        if authority.device_ids and any(
            device_id not in authority.device_ids for device_id in device_ids
        ):
            self._deny(operation, "device is not authorized")
        if authority.capabilities and any(
            capability not in authority.capabilities for capability in capabilities
        ):
            self._deny(operation, "capability is not authorized")

        roles = set(authority.roles)
        if operation in _RESIDENT_CONSENT_OPERATIONS:
            allowed = (
                bool(roles & {PrincipalRole.OPERATOR, PrincipalRole.OWNER})
                and operation in authority.operations
            )
        elif operation in _READ_OPERATIONS:
            allowed = bool(
                roles
                & {
                    PrincipalRole.VIEWER,
                    PrincipalRole.PLANNER,
                    PrincipalRole.OPERATOR,
                    PrincipalRole.OWNER,
                    PrincipalRole.SERVICE,
                }
            )
        elif operation in _PLAN_OPERATIONS:
            allowed = bool(
                roles
                & {
                    PrincipalRole.PLANNER,
                    PrincipalRole.OPERATOR,
                    PrincipalRole.OWNER,
                    PrincipalRole.SERVICE,
                }
            )
        elif operation in _OPERATOR_OPERATIONS:
            allowed = bool(
                roles
                & {
                    PrincipalRole.OPERATOR,
                    PrincipalRole.OWNER,
                    PrincipalRole.SERVICE,
                }
            )
        elif operation in _PREFERENCE_ADMIN_OPERATIONS:
            allowed = bool(roles & {PrincipalRole.OWNER, PrincipalRole.SERVICE})
        else:
            allowed = PrincipalRole.OWNER in roles or PrincipalRole.SERVICE in roles
        if (
            authority.operations
            and operation not in authority.operations
            and "*" not in authority.operations
        ):
            allowed = False
        if not allowed:
            self._deny(operation, "role or operation is not authorized")

    def bind_plan(
        self,
        plan: Plan,
        authority: AuthorityContext,
        *,
        operation: str = "prepare_plan",
        device_areas: Mapping[str, str | None] | None = None,
    ) -> Plan:
        """Bind a caller plan to the verified principal without trusting it."""

        declared = plan.authority
        legacy_default = declared == AuthorityContext()
        if not legacy_default and declared.tenant_id != authority.tenant_id:
            self._deny(operation, "plan tenant is not authorized")
        target_household = self.household_id if legacy_default else declared.household_id
        effective = authority.model_copy(
            update={
                "household_id": target_household,
                # Household selection is validated below, but membership is
                # always the verified token membership. A plan may select the
                # local target; it cannot rewrite the token's household ACL.
                "household_ids": list(authority.household_ids),
                "area_ids": self._narrow_scope(
                    authority.area_ids,
                    declared.area_ids,
                    field_name="area_ids",
                    operation=operation,
                ),
                "device_ids": self._narrow_scope(
                    authority.device_ids,
                    declared.device_ids,
                    field_name="device_ids",
                    operation=operation,
                ),
                "capabilities": self._narrow_scope(
                    authority.capabilities,
                    declared.capabilities,
                    field_name="capabilities",
                    operation=operation,
                ),
                "operations": self._narrow_scope(
                    authority.operations,
                    declared.operations,
                    field_name="operations",
                    operation=operation,
                ),
            }
        )
        command_device_ids = tuple(command.device_id for command in plan.commands)
        command_capabilities = tuple(command.command for command in plan.commands)
        self.authorize(
            effective,
            operation=operation,
            target_household_id=target_household,
            device_ids=command_device_ids,
            capabilities=command_capabilities,
        )
        if effective.area_ids:
            if device_areas is None:
                self._deny(operation, "device area is not known")
            assert device_areas is not None
            for device_id in command_device_ids:
                area_id = device_areas.get(device_id)
                if area_id is None or area_id not in effective.area_ids:
                    self._deny(operation, "area is not authorized")
        return plan.model_copy(update={"authority": effective})

    @classmethod
    def _narrow_scope(
        cls,
        verified: list[str],
        declared: list[str],
        *,
        field_name: str,
        operation: str,
    ) -> list[str]:
        """Return a caller narrowing without allowing it to broaden claims."""

        if not declared:
            return list(verified)
        if not verified:
            return list(declared)
        if field_name == "operations":
            if "*" in verified:
                return list(declared)
            if "*" in declared:
                cls._deny(operation, "operation scope is broader than verified claims")
        if not set(declared).issubset(verified):
            cls._deny(operation, f"{field_name} scope is broader than verified claims")
        return list(declared)

    @staticmethod
    def _deny(operation: str, reason: str) -> None:
        raise DomainError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "The authenticated principal is not authorized for this target",
            details={"operation": operation, "reason": reason},
        )
