"""Plan execution boundary with idempotency and audit outcomes."""

from __future__ import annotations

from datetime import UTC, datetime

from domoai.application.plan_service import PlanService
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Command,
    ErrorDetail,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    Precondition,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort, ExecutionOutcomePort, PlanRecordPort


class PlanExecutor:
    def __init__(
        self,
        adapter: AdapterPort,
        plan_service: PlanService,
        audit: AuditLog,
        *,
        plan_repository: PlanRecordPort | None = None,
        outcome_repository: ExecutionOutcomePort | None = None,
    ) -> None:
        self.adapter = adapter
        self.plan_service = plan_service
        self.audit = audit
        self.plan_repository = plan_repository
        self.outcome_repository = outcome_repository

    _NON_CLAIMABLE_STATUSES = {
        PlanStatus.EXECUTING,
        PlanStatus.COMPLETED,
        PlanStatus.PARTIALLY_FAILED,
        PlanStatus.FAILED,
        PlanStatus.UNKNOWN,
        PlanStatus.CANCELLED,
    }

    async def execute(self, plan: Plan) -> ExecutionSummary:
        if plan.execute_at is not None and plan.execute_at > datetime.now(UTC):
            raise DomainError(
                ErrorCode.NOT_YET_DUE,
                "Plan is not yet due; wait until its scheduled execution time",
            )
        if self.plan_repository is not None:
            persisted = await self.plan_repository.get(plan.id)
            if persisted is not None and persisted.status in self._NON_CLAIMABLE_STATUSES:
                raise DomainError(
                    ErrorCode.INVALID_TRANSITION,
                    "Plan is already executing or has reached a terminal status",
                )
        self.plan_service.assert_executable(plan)
        if self.plan_repository is not None:
            await self.plan_repository.save(
                plan.model_copy(update={"status": PlanStatus.EXECUTING})
            )
        outcomes: list[ExecutionOutcome] = []
        seen_keys: set[str] = set()
        for command in plan.commands:
            if command.idempotency_key in seen_keys:
                raise DomainError(
                    ErrorCode.DUPLICATE_COMMAND,
                    "Duplicate command idempotency key detected during execution",
                )
            seen_keys.add(command.idempotency_key)
            capability = self.plan_service.capability_for_command(command)
            before_state = (
                await self.plan_service.state_store.get(command.device_id, capability.name)
                if capability is not None
                else None
            )
            failed_preconditions = await self._failed_preconditions(command)
            if failed_preconditions:
                outcome = ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    status=ExecutionStatus.REJECTED,
                    before_state=before_state,
                    error=ErrorDetail(
                        code=ErrorCode.PRECONDITION_FAILED,
                        message=f"{len(failed_preconditions)} precondition(s) not satisfied",
                        retryable=True,
                        details={
                            "preconditions": [
                                item.model_dump(mode="json") for item in failed_preconditions
                            ]
                        },
                    ),
                )
            else:
                try:
                    acknowledgement = await self.adapter.execute(command)
                    after_state = None
                    status = ExecutionStatus.REJECTED
                    error: ErrorDetail | None = None
                    if acknowledgement.accepted:
                        try:
                            after_state = await self._readback(
                                command, capability.name if capability else None
                            )
                        except ConnectionError as readback_error:
                            status = ExecutionStatus.UNKNOWN
                            error = ErrorDetail(
                                code=ErrorCode.ADAPTER_UNAVAILABLE,
                                message=str(readback_error),
                                retryable=True,
                            )
                        else:
                            if self._postcondition_matches(
                                command, capability.name if capability else None, after_state
                            ):
                                status = ExecutionStatus.CONFIRMED_SUCCESS
                            else:
                                status = ExecutionStatus.UNKNOWN
                                error = ErrorDetail(
                                    code=ErrorCode.EXECUTION_FAILED,
                                    message=(
                                        "Adapter accepted the command but postcondition "
                                        "was not confirmed"
                                    ),
                                    retryable=True,
                                )
                    else:
                        error = ErrorDetail(
                            code=ErrorCode.EXECUTION_FAILED,
                            message=acknowledgement.message or "Adapter rejected command",
                            retryable=False,
                        )
                    outcome = ExecutionOutcome(
                        plan_id=plan.id,
                        command_id=command.id,
                        status=status,
                        adapter_ref=acknowledgement.source_ref,
                        before_state=before_state,
                        after_state=after_state,
                        error=error,
                    )
                except ConnectionError as error:
                    outcome = ExecutionOutcome(
                        plan_id=plan.id,
                        command_id=command.id,
                        status=ExecutionStatus.UNAVAILABLE,
                        before_state=before_state,
                        error=ErrorDetail(
                            code=ErrorCode.ADAPTER_UNAVAILABLE,
                            message=str(error),
                            retryable=True,
                        ),
                    )
            outcomes.append(outcome)
            if self.outcome_repository is not None:
                await self.outcome_repository.save(outcome)
            self.audit.append(
                event_type="command_execution_outcome",
                actor="runtime",
                subject_id=command.id,
                payload={"plan_id": plan.id, "status": outcome.status.value},
            )
        summary = ExecutionSummary(outcomes=outcomes)
        if self.plan_repository is not None:
            terminal_status = self._terminal_plan_status(outcomes)
            await self.plan_repository.save(
                plan.model_copy(update={"status": terminal_status, "execution": summary})
            )
        self.audit.append(
            event_type="plan_execution_completed",
            actor="runtime",
            subject_id=plan.id,
            payload={
                "status": self._terminal_plan_status(outcomes).value,
                "outcome_count": len(outcomes),
            },
        )
        return summary

    async def _failed_preconditions(self, command: Command) -> list[Precondition]:
        failed: list[Precondition] = []
        for precondition in command.preconditions:
            snapshot = await self.plan_service.state_store.get(
                precondition.device_id, precondition.capability
            )
            if snapshot is None or snapshot.value != precondition.expected:
                failed.append(precondition)
        return failed

    async def _readback(
        self, command: Command, capability_name: str | None
    ) -> StateSnapshot | None:
        if capability_name is None:
            return None
        device = self.plan_service.registry.get(command.device_id)
        if device is None:
            return None
        route = self.plan_service.registry.resolve_command_route(
            command.device_id, command.command
        ).route
        if route is None:
            return None
        snapshots = await self.adapter.read_state([route.source_ref])
        snapshot = next(
            (
                item
                for item in snapshots
                if item.capability == capability_name
                and item.source_ref.adapter_id == route.source_ref.adapter_id
                and item.source_ref.external_id == route.source_ref.external_id
            ),
            None,
        )
        if snapshot is None:
            return None
        normalized = snapshot.model_copy(update={"device_id": command.device_id})
        await self.plan_service.state_store.save(normalized)
        return normalized

    @staticmethod
    def _postcondition_matches(
        command: Command, capability_name: str | None, after_state: StateSnapshot | None
    ) -> bool:
        if after_state is None or after_state.status is not StateStatus.CURRENT:
            return False
        expected: object
        if command.command == "turn_on":
            expected = True
        elif command.command == "turn_off":
            expected = False
        elif command.command == "open":
            expected = 100
        elif command.command == "close":
            expected = 0
        elif command.command == "stop":
            return True
        elif command.command.startswith("set_"):
            expected = command.value
        else:
            return capability_name is not None
        return after_state.value == expected

    @staticmethod
    def _terminal_plan_status(outcomes: list[ExecutionOutcome]) -> PlanStatus:
        statuses = {outcome.status for outcome in outcomes}
        if statuses == {ExecutionStatus.CONFIRMED_SUCCESS}:
            return PlanStatus.COMPLETED
        if statuses & {ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE}:
            return PlanStatus.UNKNOWN
        if ExecutionStatus.CONFIRMED_SUCCESS in statuses:
            return PlanStatus.PARTIALLY_FAILED
        return PlanStatus.FAILED
