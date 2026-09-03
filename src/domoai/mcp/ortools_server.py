"""Proposal-only MCP server for the OR-Tools optimization boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.optimization_service import OptimizationService
from domoai.application.optimization_worker import OptimizationWorker
from domoai.application.plan_service import PlanService
from domoai.domain.models import ErrorDetail, StrictModel
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.errors import error_envelope
from domoai.mcp.request_context import with_request_principal
from domoai.optimizer.ports import (
    BoundedOptimizerWorkerPort,
    OptimizationResult,
    OptimizationStatus,
    build_result,
)
from domoai.optimizer.scenario import (
    MAX_HORIZON_SLOTS,
    OptimizationScenario,
    validate_executable_scenario,
)
from domoai.optimizer.scenario import (
    validate_scenario as validate_scenario_model,
)
from domoai.runtime.registry import DeviceRegistry


class OptimizationExplanation(StrictModel):
    schema_version: str = "v1"
    scenario_id: str
    status: OptimizationStatus
    solver: str
    summary: str
    objective_values: dict[str, float]
    constraint_summary: dict[str, Any]
    diagnostics: list[ErrorDetail]
    proposal: dict[str, Any] | None = None


@dataclass
class OrtoolsMcpContext:
    registry: DeviceRegistry
    plan_service: PlanService
    optimization_service: OptimizationService
    optimization_worker: BoundedOptimizerWorkerPort | None = None
    max_horizon_slots: int = MAX_HORIZON_SLOTS

    @property
    def runtime_revision(self) -> str:
        return self.plan_service.current_revision


def explain_result(result: OptimizationResult) -> OptimizationExplanation:
    hard_satisfied = result.constraint_summary.get("hard_satisfied")
    if result.plan is not None and result.status in {
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.FEASIBLE_HIERARCHY,
    }:
        if hard_satisfied is True:
            summary = f"A {result.status.value} proposal satisfies all declared hard constraints."
        else:
            summary = (
                f"A {result.status.value} proposal was produced without complete "
                "hard-constraint evidence."
            )
        bundle = result.plans or [result.plan]
        proposal = {
            "plan_id": result.plan.id,
            "status": result.plan.status.value,
            "commands": [command.model_dump(mode="json") for command in result.plan.commands],
            "members": [
                {
                    "plan_id": member.id,
                    "execute_at": member.execute_at.isoformat()
                    if member.execute_at is not None
                    else None,
                    "status": member.status.value,
                    "commands": [command.model_dump(mode="json") for command in member.commands],
                }
                for member in bundle
            ],
        }
    elif result.status is OptimizationStatus.NO_ACTION_REQUIRED:
        summary = (
            "Optimization is valid and all declared constraints are satisfied; "
            "no physical action is required."
        )
        proposal = None
    else:
        summary = f"No proposal was produced because the result is {result.status.value}."
        proposal = None
    return OptimizationExplanation(
        scenario_id=result.scenario_id,
        status=result.status,
        solver=result.solver,
        summary=summary,
        objective_values=result.objective_values,
        constraint_summary=result.constraint_summary,
        diagnostics=result.diagnostics,
        proposal=proposal,
    )


def register_ortools_tools(server: FastMCP, context: OrtoolsMcpContext) -> FastMCP:
    ensure_fastmcp_settings_ready()
    read_annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    # Fallback for a context built without a pre-wired worker (e.g. ad-hoc
    # test contexts). The production path (mcp/stdio.py
    # build_configured_server) always pre-supplies one registered with
    # RuntimeComposition.close(), so this fallback's worker is intentionally
    # unowned/best-effort -- see DomoticsMcpContext.blocking_worker.
    worker = context.optimization_worker or OptimizationWorker(context.optimization_service)

    @server.tool(
        name="validate_scenario",
        description="Validate an optimization scenario against canonical devices and capabilities.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def validate_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = OptimizationScenario.model_validate(scenario)
            diagnostics = validate_scenario_model(
                parsed, context.registry, max_horizon_slots=context.max_horizon_slots
            )
            return {
                "schema_version": "v1",
                "scenario_id": parsed.id,
                "runtime_revision": context.runtime_revision,
                "valid": not diagnostics,
                "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="optimize_scenario",
        description="Compute a deterministic proposal without executing physical commands.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def optimize_scenario(
        scenario: dict[str, Any], validate_proposal: bool = True
    ) -> dict[str, Any]:
        try:
            parsed = OptimizationScenario.model_validate(scenario)
            if parsed.ev_loads:
                diagnostics = validate_executable_scenario(
                    parsed,
                    context.registry,
                    max_horizon_slots=context.max_horizon_slots,
                )
                if diagnostics:
                    return {
                        "schema_version": "v1",
                        **build_result(
                            scenario_id=parsed.id,
                            status=OptimizationStatus.INVALID,
                            diagnostics=[item.model_dump(mode="json") for item in diagnostics],
                        ).model_dump(mode="json"),
                    }
            result = await worker.optimize(parsed)
            if validate_proposal:
                result = context.optimization_service.validate_proposal(result)
            return result.model_dump(mode="json")
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="explain_solution",
        description="Explain a typed optimization result without changing runtime state.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def explain_solution(result: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = OptimizationResult.model_validate(result)
            return explain_result(parsed).model_dump(mode="json")
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    return server


def create_ortools_server(context: OrtoolsMcpContext) -> FastMCP:
    """Create the internal optimizer-only factory for focused contract tests."""

    ensure_fastmcp_settings_ready()
    return register_ortools_tools(
        FastMCP(
            "DomoAI OR-Tools Optimization",
            instructions=(
                "Proposal-only optimization tools. Physical execution belongs "
                "to the Domotics runtime."
            ),
        ),
        context,
    )
