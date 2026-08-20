"""Host-neutral orchestration for the portable home-energy skill."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import Field, ValidationError

from domoai.domain.models import Plan, StrictModel
from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.scenario import OptimizationScenario
from domoai.skills.validator import V1_OPERATION_BINDINGS, V2_OPERATION_BINDINGS


class WorkflowStatus(StrEnum):
    """Host-facing outcome status for one ephemeral workflow run."""

    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkflowStage(StrEnum):
    """Observable stage reached by the workflow."""

    STARTED = "started"
    CONTEXT_READY = "context_ready"
    PROPOSAL_READY = "proposal_ready"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkflowDiagnostic(StrictModel):
    """Sanitized, stage-specific workflow diagnostic."""

    stage: WorkflowStage
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(StrictModel):
    """Explicit host/operator decision at the approval boundary."""

    approved: bool
    approved_by: str | None = None
    validation_digest: str | None = None
    reason: str | None = None
    operator_token: str | None = None


class EnergySkillRequest(StrictModel):
    """Strict orchestration input; this is not a new canonical domain schema."""

    scenario: OptimizationScenario
    devices: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    accept_stale_assumption: bool = False


class SkillRunResult(StrictModel):
    """Ephemeral workflow evidence; runtime outcomes remain authoritative."""

    run_id: str = Field(min_length=1)
    status: WorkflowStatus
    stage: WorkflowStage
    stage_history: list[WorkflowStage] = Field(min_length=1)
    completed_operations: list[str] = Field(default_factory=list)
    runtime_revision: str | None = None
    plan_id: str | None = None
    validation_digest: str | None = None
    plan_ids: list[str] = Field(default_factory=list)
    validation_digests: list[str] = Field(default_factory=list)
    scheduled_plan_ids: list[str] = Field(default_factory=list)
    bundle: list[dict[str, Any]] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None
    explanation: dict[str, Any] | None = None
    diagnostics: list[WorkflowDiagnostic] = Field(default_factory=list)


class SkillToolRouter(Protocol):
    """Single MCP-role router supplied by a compatible host."""

    def call(
        self, role: str, tool: str, arguments: dict[str, Any]
    ) -> Awaitable[Mapping[str, Any]]: ...

    def current_revision(self, role: str) -> str | None: ...


class ApprovalPort(Protocol):
    """Host/operator approval boundary supplied by a compatible host."""

    def request_approval(
        self, plan: dict[str, Any], explanation: dict[str, Any]
    ) -> Awaitable[ApprovalDecision | None]: ...


class _WorkflowFailure(Exception):
    def __init__(
        self,
        *,
        status: WorkflowStatus,
        stage: WorkflowStage,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.stage = stage
        self.code = code
        self.message = message
        self.details = details or {}


class EnergySkillWorkflow:
    """Coordinate existing MCP contracts without becoming another runtime."""

    def __init__(
        self,
        router: SkillToolRouter,
        approval: ApprovalPort,
        *,
        operation_bindings: Mapping[str, tuple[str, str, str]] | None = None,
    ) -> None:
        selected_bindings = dict(operation_bindings or V2_OPERATION_BINDINGS)
        if selected_bindings not in (V1_OPERATION_BINDINGS, V2_OPERATION_BINDINGS):
            raise ValueError("workflow bindings must match the portable v1 or v2 routes")
        self.router = router
        self.approval = approval
        self._operation_bindings = selected_bindings
        self._requires_energy_context = selected_bindings == V2_OPERATION_BINDINGS
        self._runs: dict[str, SkillRunResult] = {}
        self._consumed_runs: dict[str, SkillRunResult] = {}

    async def run(self, request: EnergySkillRequest | Mapping[str, Any]) -> SkillRunResult:
        run_id = uuid.uuid4().hex
        history = [WorkflowStage.STARTED]
        completed: list[str] = []

        try:
            parsed_request = EnergySkillRequest.model_validate(request)
        except (TypeError, ValueError, ValidationError) as error:
            return self._store(
                self._result(
                    run_id=run_id,
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.STARTED,
                    history=history,
                    diagnostics=[
                        WorkflowDiagnostic(
                            stage=WorkflowStage.STARTED,
                            code="invalid_workflow_input",
                            message="Workflow input does not satisfy its strict contract",
                            details=self._validation_details(error),
                        )
                    ],
                )
            )

        runtime_revision: str | None = None
        proposal: dict[str, Any] | None = None
        explanation: dict[str, Any] | None = None
        plan_id: str | None = None
        validation_digest: str | None = None

        try:
            discovery = await self._call(
                "discover_devices",
                {"refresh": False},
                stage=WorkflowStage.STARTED,
            )
            discovery_revision = self._required_string(
                discovery, "runtime_revision", "discovery"
            )
            runtime_revision = discovery_revision
            self._check_devices(discovery, parsed_request)
            completed.append("discover_devices")

            required_devices = self._required_devices(parsed_request)
            state = await self._call(
                "get_state",
                {
                    "devices": required_devices,
                    "capabilities": parsed_request.capabilities or None,
                    "allow_stale": True,
                },
                stage=WorkflowStage.STARTED,
            )
            self._check_state(state, required_devices, parsed_request.accept_stale_assumption)
            completed.append("get_state")

            optimization_scenario = parsed_request.scenario
            if self._requires_energy_context:
                energy_context_response = await self._call(
                    "get_energy_context",
                    {"horizon": parsed_request.scenario.horizon.model_dump(mode="json")},
                    stage=WorkflowStage.CONTEXT_READY,
                )
                energy_context = self._check_energy_context(
                    energy_context_response,
                    parsed_request.scenario,
                    discovery_revision,
                )
                optimization_scenario = parsed_request.scenario.model_copy(
                    update={"energy_context": energy_context}
                )
                completed.append("get_energy_context")
            self._transition(history, WorkflowStage.CONTEXT_READY)

            proposal = await self._call(
                "optimize_scenario",
                {
                    "scenario": optimization_scenario.model_dump(mode="json"),
                    "validate_proposal": True,
                },
                stage=WorkflowStage.CONTEXT_READY,
            )
            self._check_proposal(proposal, parsed_request.scenario)
            proposal_revision = proposal.get("runtime_revision")
            if proposal_revision is not None and proposal_revision != runtime_revision:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.PROPOSAL_READY,
                    code="runtime_revision_changed",
                    message="Optimization and discovery revisions do not match",
                )
            completed.append("optimize_scenario")
            self._transition(history, WorkflowStage.PROPOSAL_READY)

            bundle: list[dict[str, Any]] = []
            first_confirming_plan_dict: dict[str, Any] | None = None
            for member_plan in self._required_plans(proposal):
                validation_response = await self._call(
                    "validate_plan",
                    {"plan": member_plan.model_dump(mode="json"), "mode": "preview"},
                    stage=WorkflowStage.PROPOSAL_READY,
                )
                validation = self._check_validation(validation_response)
                validation_revision = self._required_string(
                    validation, "runtime_revision", "plan validation"
                )
                if not validation_revision.startswith(f"{discovery_revision}:"):
                    raise _WorkflowFailure(
                        status=WorkflowStatus.BLOCKED,
                        stage=WorkflowStage.VALIDATED,
                        code="runtime_revision_changed",
                        message="Runtime revision changed between state and plan validation",
                    )
                runtime_revision = validation_revision
                member_plan_id = self._required_string(
                    validation_response.get("plan"), "id", "validated plan"
                )
                member_digest = self._required_string(
                    validation, "digest", "plan validation"
                )
                member_requires_confirmation = validation.get(
                    "status"
                ) == "requires_confirmation" or (
                    validation_response.get("plan", {}).get("status")
                    == "requires_confirmation"
                )
                if member_requires_confirmation and first_confirming_plan_dict is None:
                    first_confirming_plan_dict = validation_response["plan"]
                bundle.append(
                    {
                        "plan_id": member_plan_id,
                        "validation_digest": member_digest,
                        "requires_confirmation": member_requires_confirmation,
                        "execute_at": member_plan.execute_at.isoformat()
                        if member_plan.execute_at is not None
                        else None,
                    }
                )
            completed.append("validate_plan")
            self._transition(history, WorkflowStage.VALIDATED)

            plan_id = bundle[0]["plan_id"]
            validation_digest = bundle[0]["validation_digest"]

            explanation = await self._call(
                "explain_solution",
                {"result": proposal},
                stage=WorkflowStage.VALIDATED,
            )
            self._check_explanation(explanation, parsed_request.scenario.id)
            completed.append("explain_solution")

            requires_confirmation = any(entry["requires_confirmation"] for entry in bundle)
            if requires_confirmation:
                self._transition(history, WorkflowStage.AWAITING_APPROVAL)
                assert first_confirming_plan_dict is not None
                decision = await self.approval.request_approval(
                    first_confirming_plan_dict, explanation
                )
                completed.append("operator_approval")
                if decision is None:
                    return self._store(
                        self._result(
                            run_id=run_id,
                            status=WorkflowStatus.AWAITING_APPROVAL,
                            stage=WorkflowStage.AWAITING_APPROVAL,
                            history=history,
                            completed=completed,
                            runtime_revision=runtime_revision,
                            plan_id=plan_id,
                            validation_digest=validation_digest,
                            bundle=bundle,
                            proposal=proposal,
                            explanation=explanation,
                        )
                    )
                if not decision.approved:
                    return self._store(
                        self._result(
                            run_id=run_id,
                            status=WorkflowStatus.CANCELLED,
                            stage=WorkflowStage.CANCELLED,
                            history=self._with_stage(history, WorkflowStage.CANCELLED),
                            completed=completed,
                            runtime_revision=runtime_revision,
                            plan_id=plan_id,
                            validation_digest=validation_digest,
                            bundle=bundle,
                            proposal=proposal,
                            explanation=explanation,
                            diagnostics=[
                                WorkflowDiagnostic(
                                    stage=WorkflowStage.AWAITING_APPROVAL,
                                    code="approval_declined",
                                    message="Operator declined the validated plan",
                                )
                            ],
                        )
                    )
                self._validate_approval(
                    decision, validation_digest, WorkflowStage.AWAITING_APPROVAL
                )
                return await self._execute(
                    run_id=run_id,
                    history=history,
                    completed=completed,
                    runtime_revision=runtime_revision,
                    bundle=bundle,
                    proposal=proposal,
                    explanation=explanation,
                    approval=decision,
                )

            return await self._execute(
                run_id=run_id,
                history=history,
                completed=completed,
                runtime_revision=runtime_revision,
                bundle=bundle,
                proposal=proposal,
                explanation=explanation,
                approval=None,
            )
        except _WorkflowFailure as failure:
            return self._store(
                self._result(
                    run_id=run_id,
                    status=failure.status,
                    stage=failure.stage,
                    history=self._with_stage(history, failure.stage),
                    completed=completed,
                    runtime_revision=runtime_revision,
                    plan_id=plan_id,
                    validation_digest=validation_digest,
                    proposal=proposal,
                    explanation=explanation,
                    diagnostics=[
                        WorkflowDiagnostic(
                            stage=failure.stage,
                            code=failure.code,
                            message=failure.message,
                            details=failure.details,
                        )
                    ],
                )
            )

    async def resume(
        self, pending: SkillRunResult, approval: ApprovalDecision | Mapping[str, Any] | None
    ) -> SkillRunResult:
        stored = self._runs.get(pending.run_id)
        if pending.run_id in self._consumed_runs:
            return self._consumed_runs[pending.run_id]
        if stored is None or stored.status is not WorkflowStatus.AWAITING_APPROVAL:
            return self._store(
                self._resume_failure(
                    pending, "invalid_resume", "Workflow run is not awaiting approval"
                )
            )
        if approval is None:
            return stored
        try:
            decision = ApprovalDecision.model_validate(approval)
        except (TypeError, ValueError, ValidationError):
            return self._store(
                self._resume_failure(
                    stored,
                    "invalid_approval",
                    "Approval does not satisfy the strict approval contract",
                )
            )
        if not decision.approved:
            result = self._resume_failure(
                stored,
                "approval_declined",
                "Operator declined the validated plan",
                status=WorkflowStatus.CANCELLED,
                stage=WorkflowStage.CANCELLED,
            )
            return self._store(result)
        try:
            self._validate_approval(
                decision, stored.validation_digest, WorkflowStage.AWAITING_APPROVAL
            )
            current_revision = self.router.current_revision("mcp")
            if current_revision is None:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.AWAITING_APPROVAL,
                    code="runtime_revision_unavailable",
                    message="Current Domotics runtime revision is unavailable",
                )
            if current_revision != stored.runtime_revision:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.AWAITING_APPROVAL,
                    code="runtime_revision_changed",
                    message="Runtime revision changed after plan validation",
                )
            return await self._execute(
                run_id=stored.run_id,
                history=list(stored.stage_history),
                completed=list(stored.completed_operations),
                runtime_revision=stored.runtime_revision,
                bundle=list(stored.bundle),
                proposal=stored.proposal,
                explanation=stored.explanation,
                approval=decision,
            )
        except _WorkflowFailure as failure:
            return self._store(
                self._resume_failure(
                    stored,
                    failure.code,
                    failure.message,
                    status=failure.status,
                    stage=failure.stage,
                    details=failure.details,
                )
            )

    async def _execute(
        self,
        *,
        run_id: str,
        history: list[WorkflowStage],
        completed: list[str],
        runtime_revision: str | None,
        bundle: list[dict[str, Any]],
        proposal: dict[str, Any] | None,
        explanation: dict[str, Any] | None,
        approval: ApprovalDecision | None,
    ) -> SkillRunResult:
        if not bundle or not runtime_revision:
            return self._store(
                self._resume_failure(
                    self._result(
                        run_id=run_id,
                        status=WorkflowStatus.BLOCKED,
                        stage=WorkflowStage.VALIDATED,
                        history=history,
                        completed=completed,
                        runtime_revision=runtime_revision,
                        bundle=bundle,
                        proposal=proposal,
                        explanation=explanation,
                    ),
                    "missing_execution_boundary",
                    "A validated plan bundle and runtime revision are required",
                )
            )
        try:
            current_revision = self.router.current_revision("mcp")
            if current_revision is None:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.VALIDATED,
                    code="runtime_revision_unavailable",
                    message="Current Domotics runtime revision is unavailable",
                )
            if current_revision != runtime_revision:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.VALIDATED,
                    code="runtime_revision_changed",
                    message="Runtime revision changed before execution",
                )
            self._transition(history, WorkflowStage.EXECUTING)
            now = datetime.now(UTC)
            scheduled_plan_ids: list[str] = []
            outcomes: list[Any] = []
            for entry in bundle:
                member_plan_id = entry["plan_id"]
                member_digest = entry["validation_digest"]
                approval_id: str | None = None
                if entry["requires_confirmation"]:
                    if approval is None:
                        raise _WorkflowFailure(
                            status=WorkflowStatus.BLOCKED,
                            stage=WorkflowStage.EXECUTING,
                            code="approval_required",
                            message="A bundle member requires confirmation but none was granted",
                        )
                    approval_response = await self._call_tool(
                        "mcp",
                        "request_approval",
                        {
                            "plan_id": member_plan_id,
                            "validation_digest": member_digest,
                            "approved_by": approval.approved_by or "operator",
                            "operator_token": approval.operator_token or "",
                        },
                        stage=WorkflowStage.EXECUTING,
                    )
                    approval_id = self._required_string(
                        approval_response, "approval_id", "approval issuance"
                    )
                execute_at = entry["execute_at"]
                member_due_now = execute_at is None or datetime.fromisoformat(execute_at) <= now
                if member_due_now:
                    arguments: dict[str, Any] = {
                        "plan_id": member_plan_id,
                        "validation_digest": member_digest,
                        "dry_run": False,
                    }
                    if approval_id is not None:
                        arguments["approval_id"] = approval_id
                    response = await self._call(
                        "execute_plan", arguments, stage=WorkflowStage.EXECUTING
                    )
                    member_outcomes = response.get("outcomes")
                    if not isinstance(member_outcomes, list) or not member_outcomes:
                        raise _WorkflowFailure(
                            status=WorkflowStatus.FAILED,
                            stage=WorkflowStage.FAILED,
                            code="malformed_execution_result",
                            message="Domotics execution returned no terminal outcomes",
                        )
                    if not all(
                        isinstance(outcome, dict)
                        and outcome.get("status") == "confirmed_success"
                        for outcome in member_outcomes
                    ):
                        raise _WorkflowFailure(
                            status=WorkflowStatus.FAILED,
                            stage=WorkflowStage.FAILED,
                            code="execution_not_confirmed",
                            message="Domotics returned a non-confirmed execution outcome",
                            details={"outcome_count": len(member_outcomes)},
                        )
                    outcomes.extend(member_outcomes)
                    completed.append("execute_plan")
                else:
                    schedule_arguments: dict[str, Any] = {
                        "plan_id": member_plan_id,
                        "validation_digest": member_digest,
                        "execute_at": execute_at,
                    }
                    if approval_id is not None:
                        schedule_arguments["approval_id"] = approval_id
                    await self._call_tool(
                        "mcp", "schedule_plan", schedule_arguments, stage=WorkflowStage.EXECUTING
                    )
                    scheduled_plan_ids.append(member_plan_id)
                    completed.append("schedule_plan")
            if not outcomes and not scheduled_plan_ids:
                raise _WorkflowFailure(
                    status=WorkflowStatus.FAILED,
                    stage=WorkflowStage.FAILED,
                    code="malformed_execution_result",
                    message="Domotics execution returned no terminal outcomes",
                )
            result = self._result(
                run_id=run_id,
                status=WorkflowStatus.COMPLETED,
                stage=WorkflowStage.COMPLETED,
                history=self._with_stage(history, WorkflowStage.COMPLETED),
                completed=completed,
                runtime_revision=runtime_revision,
                bundle=bundle,
                scheduled_plan_ids=scheduled_plan_ids,
                proposal=proposal,
                explanation=explanation,
            )
            return self._consume(result)
        except _WorkflowFailure as failure:
            result = self._result(
                run_id=run_id,
                status=failure.status,
                stage=failure.stage,
                history=self._with_stage(history, failure.stage),
                completed=completed,
                runtime_revision=runtime_revision,
                bundle=bundle,
                proposal=proposal,
                explanation=explanation,
                diagnostics=[
                    WorkflowDiagnostic(
                        stage=failure.stage,
                        code=failure.code,
                        message=failure.message,
                        details=failure.details,
                    )
                ],
            )
            return self._consume(result)

    async def _call(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        stage: WorkflowStage,
    ) -> dict[str, Any]:
        provider, tool, _mode = self._operation_bindings[operation]
        return await self._call_tool(provider, tool, arguments, stage=stage)

    async def _call_tool(
        self,
        provider: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        stage: WorkflowStage,
    ) -> dict[str, Any]:
        try:
            raw = await self.router.call(provider, tool, arguments)
        except Exception as error:
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=stage,
                code="provider_unavailable",
                message="A required semantic provider could not process the operation",
                details={"provider": provider, "tool": tool, "error_type": type(error).__name__},
            ) from error
        if not isinstance(raw, Mapping):
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=stage,
                code="malformed_response",
                message="A semantic provider returned a malformed response",
                details={"provider": provider, "tool": tool},
            )
        response = dict(raw)
        if "error" in response:
            error_value = response.get("error")
            error_code = (
                str(error_value.get("code"))
                if isinstance(error_value, Mapping) and error_value.get("code")
                else "provider_error"
            )
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=stage,
                code=error_code,
                message="A semantic provider rejected the workflow operation",
                details={"provider": provider, "tool": tool},
            )
        return response

    @staticmethod
    def _required_devices(request: EnergySkillRequest) -> list[str]:
        devices = request.devices or [load.device_id for load in request.scenario.loads]
        return list(dict.fromkeys(devices))

    @staticmethod
    def _check_devices(response: dict[str, Any], request: EnergySkillRequest) -> None:
        devices = response.get("devices")
        if not isinstance(devices, list):
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=WorkflowStage.STARTED,
                code="malformed_response",
                message="Device discovery returned no structured device list",
            )
        discovered = {
            item.get("id") for item in devices if isinstance(item, Mapping) and item.get("id")
        }
        missing = [
            device_id
            for device_id in EnergySkillWorkflow._required_devices(request)
            if device_id not in discovered
        ]
        if missing:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.STARTED,
                code="missing_device",
                message="A required device was not present in semantic discovery",
                details={"count": len(missing)},
            )

    @staticmethod
    def _check_state(
        response: dict[str, Any], required_devices: list[str], accept_stale: bool
    ) -> None:
        states = response.get("states")
        if not isinstance(states, list):
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=WorkflowStage.CONTEXT_READY,
                code="malformed_response",
                message="State read returned no structured state list",
            )

        seen_devices: set[str] = set()
        for item in states:
            if not isinstance(item, Mapping):
                raise _WorkflowFailure(
                    status=WorkflowStatus.FAILED,
                    stage=WorkflowStage.CONTEXT_READY,
                    code="malformed_response",
                    message="State read contained a malformed snapshot",
                )
            device_id = item.get("device_id")
            status = item.get("status")
            if not isinstance(device_id, str) or not isinstance(status, str):
                raise _WorkflowFailure(
                    status=WorkflowStatus.FAILED,
                    stage=WorkflowStage.CONTEXT_READY,
                    code="malformed_response",
                    message="State snapshot omitted required freshness fields",
                )
            if device_id in required_devices:
                seen_devices.add(device_id)
            if status == "invalid":
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.CONTEXT_READY,
                    code="invalid_state",
                    message="Required state is invalid",
                )
            if status in {"stale", "unavailable"} and not accept_stale:
                raise _WorkflowFailure(
                    status=WorkflowStatus.BLOCKED,
                    stage=WorkflowStage.CONTEXT_READY,
                    code="stale_state" if status == "stale" else "unavailable_state",
                    message="Required state is stale or unavailable",
                )
        missing = [device_id for device_id in required_devices if device_id not in seen_devices]
        if missing:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.CONTEXT_READY,
                code="missing_state",
                message="Required device state was not available",
                details={"count": len(missing)},
            )

    @staticmethod
    def _check_energy_context(
        response: dict[str, Any],
        scenario: OptimizationScenario,
        discovery_revision: str,
    ) -> EnergyContext:
        runtime_revision = response.get("runtime_revision")
        if runtime_revision != discovery_revision:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.CONTEXT_READY,
                code="stale_energy_context",
                message="Energy context revision does not match discovery",
            )
        try:
            context_payload = response.get("context")
            if not isinstance(context_payload, Mapping):
                raise ValueError("energy context is missing")
            context = EnergyContext.model_validate(context_payload)
            if context.horizon != scenario.horizon:
                raise ValueError("energy context horizon does not match scenario")
            return context
        except (TypeError, ValueError, ValidationError) as error:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.CONTEXT_READY,
                code="invalid_energy_context",
                message="Energy context is incomplete or invalid",
            ) from error

    @staticmethod
    def _check_proposal(response: dict[str, Any], scenario: OptimizationScenario) -> None:
        status = response.get("status")
        if status not in {"optimal", "feasible"}:
            safe_status = str(status) if isinstance(status, str) else "unknown"
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.CONTEXT_READY,
                code=f"{safe_status}_proposal",
                message="Optimizer did not produce an executable proposal",
            )
        try:
            result_scenario_id = response.get("scenario_id")
            if result_scenario_id is not None and result_scenario_id != scenario.id:
                raise ValueError("scenario id mismatch")
            plan = response.get("plan")
            if not isinstance(plan, Mapping):
                raise ValueError("proposal plan is missing")
            Plan.model_validate(plan)
        except (TypeError, ValueError, ValidationError) as error:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="invalid_proposal",
                message="Optimizer proposal does not satisfy the plan contract",
            ) from error

    @staticmethod
    def _required_plan(response: dict[str, Any]) -> Plan:
        try:
            plan = response.get("plan")
            if not isinstance(plan, Mapping):
                raise ValueError("proposal plan is missing")
            return Plan.model_validate(plan)
        except (TypeError, ValueError, ValidationError) as error:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="invalid_proposal",
                message="Optimizer proposal does not contain a valid plan",
            ) from error

    @staticmethod
    def _required_plans(response: dict[str, Any]) -> list[Plan]:
        plans_field = response.get("plans")
        raw_plans: list[Any]
        if isinstance(plans_field, list) and plans_field:
            raw_plans = plans_field
        else:
            raw_plans = [response.get("plan")]
        try:
            parsed: list[Plan] = []
            for raw_plan in raw_plans:
                if not isinstance(raw_plan, Mapping):
                    raise ValueError("proposal plan is missing")
                parsed.append(Plan.model_validate(raw_plan))
            return parsed
        except (TypeError, ValueError, ValidationError) as error:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="invalid_proposal",
                message="Optimizer proposal does not contain a valid plan bundle",
            ) from error

    @staticmethod
    def _check_validation(response: dict[str, Any]) -> Mapping[str, Any]:
        validation = response.get("validation")
        plan = response.get("plan")
        if not isinstance(validation, Mapping) or not isinstance(plan, Mapping):
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="malformed_response",
                message="Domotics plan validation returned incomplete structured content",
            )
        status = validation.get("status")
        if status not in {"valid", "requires_confirmation"}:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="invalid_plan",
                message="Domotics rejected the optimization proposal",
            )
        return validation

    @staticmethod
    def _check_explanation(response: dict[str, Any], scenario_id: str) -> None:
        if response.get("scenario_id") != scenario_id or not isinstance(
            response.get("summary"), str
        ):
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=WorkflowStage.VALIDATED,
                code="malformed_response",
                message="OR-Tools explanation did not satisfy its structured contract",
            )

    @staticmethod
    def _validate_approval(
        decision: ApprovalDecision,
        validation_digest: str | None,
        stage: WorkflowStage,
    ) -> None:
        if not decision.approved:
            raise _WorkflowFailure(
                status=WorkflowStatus.CANCELLED,
                stage=WorkflowStage.CANCELLED,
                code="approval_declined",
                message="Operator declined the validated plan",
            )
        if not decision.approved_by or decision.validation_digest != validation_digest:
            raise _WorkflowFailure(
                status=WorkflowStatus.BLOCKED,
                stage=stage,
                code="approval_mismatch",
                message="Approval does not match the validated plan digest",
            )

    @staticmethod
    def _required_string(value: object, key: str, subject: str) -> str:
        if not isinstance(value, Mapping) or not isinstance(value.get(key), str) or not value[key]:
            raise _WorkflowFailure(
                status=WorkflowStatus.FAILED,
                stage=WorkflowStage.PROPOSAL_READY,
                code="malformed_response",
                message=f"{subject} omitted a required string field",
            )
        return cast(str, value[key])

    @staticmethod
    def _transition(history: list[WorkflowStage], stage: WorkflowStage) -> None:
        if history[-1] != stage:
            history.append(stage)

    @staticmethod
    def _with_stage(history: list[WorkflowStage], stage: WorkflowStage) -> list[WorkflowStage]:
        result = list(history)
        EnergySkillWorkflow._transition(result, stage)
        return result

    @staticmethod
    def _validation_details(error: Exception) -> dict[str, Any]:
        if isinstance(error, ValidationError):
            return {
                "field_count": len(error.errors()),
                "fields": [str(item.get("loc", ())) for item in error.errors()],
            }
        return {"error_type": type(error).__name__}

    @staticmethod
    def _result(
        *,
        run_id: str,
        status: WorkflowStatus,
        stage: WorkflowStage,
        history: list[WorkflowStage],
        completed: list[str] | None = None,
        runtime_revision: str | None = None,
        plan_id: str | None = None,
        validation_digest: str | None = None,
        bundle: list[dict[str, Any]] | None = None,
        scheduled_plan_ids: list[str] | None = None,
        proposal: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
        diagnostics: list[WorkflowDiagnostic] | None = None,
    ) -> SkillRunResult:
        resolved_bundle = list(bundle or [])
        resolved_plan_id = plan_id or (
            resolved_bundle[0]["plan_id"] if resolved_bundle else None
        )
        resolved_digest = validation_digest or (
            resolved_bundle[0]["validation_digest"] if resolved_bundle else None
        )
        return SkillRunResult(
            run_id=run_id,
            status=status,
            stage=stage,
            stage_history=EnergySkillWorkflow._with_stage(history, stage),
            completed_operations=list(completed or []),
            runtime_revision=runtime_revision,
            plan_id=resolved_plan_id,
            validation_digest=resolved_digest,
            plan_ids=[entry["plan_id"] for entry in resolved_bundle],
            validation_digests=[entry["validation_digest"] for entry in resolved_bundle],
            scheduled_plan_ids=list(scheduled_plan_ids or []),
            bundle=resolved_bundle,
            proposal=proposal,
            explanation=explanation,
            diagnostics=list(diagnostics or []),
        )

    def _store(self, result: SkillRunResult) -> SkillRunResult:
        self._runs[result.run_id] = result
        return result

    def _consume(self, result: SkillRunResult) -> SkillRunResult:
        self._runs[result.run_id] = result
        self._consumed_runs[result.run_id] = result
        return result

    @staticmethod
    def _resume_failure(
        pending: SkillRunResult,
        code: str,
        message: str,
        *,
        status: WorkflowStatus = WorkflowStatus.BLOCKED,
        stage: WorkflowStage = WorkflowStage.AWAITING_APPROVAL,
        details: dict[str, Any] | None = None,
    ) -> SkillRunResult:
        return EnergySkillWorkflow._result(
            run_id=pending.run_id,
            status=status,
            stage=stage,
            history=list(pending.stage_history),
            completed=list(pending.completed_operations),
            runtime_revision=pending.runtime_revision,
            plan_id=pending.plan_id,
            validation_digest=pending.validation_digest,
            bundle=list(pending.bundle),
            proposal=pending.proposal,
            explanation=pending.explanation,
            diagnostics=[
                WorkflowDiagnostic(
                    stage=stage,
                    code=code,
                    message=message,
                    details=details or {},
                )
            ],
        )
