"""Tenant/household/role authorization at the application boundary."""

from __future__ import annotations

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
    }
)
_PLAN_OPERATIONS = frozenset(
    {"prepare_command", "prepare_plan", "validate_command", "validate_plan"}
)
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
    }
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
        if operation in _READ_OPERATIONS:
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
        self, plan: Plan, authority: AuthorityContext, *, operation: str = "prepare_plan"
    ) -> Plan:
        """Bind a caller plan to the verified principal without trusting it."""

        declared = plan.authority
        legacy_default = declared == AuthorityContext()
        target_household = self.household_id if legacy_default else declared.household_id
        self.authorize(
            authority,
            operation=operation,
            target_household_id=target_household,
            device_ids=tuple(command.device_id for command in plan.commands),
            capabilities=tuple(command.command for command in plan.commands),
        )
        bound = authority.model_copy(
            update={
                "household_id": target_household,
                "household_ids": sorted(set(authority.household_ids)),
                "area_ids": declared.area_ids,
                "device_ids": declared.device_ids,
                "capabilities": declared.capabilities,
            }
        )
        return plan.model_copy(update={"authority": bound})

    @staticmethod
    def _deny(operation: str, reason: str) -> None:
        raise DomainError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "The authenticated principal is not authorized for this target",
            details={"operation": operation, "reason": reason},
        )
