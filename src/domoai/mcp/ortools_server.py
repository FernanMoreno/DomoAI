"""Proposal-only MCP server for the OR-Tools optimization boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import ErrorDetail, StrictModel
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.errors import error_envelope
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import OptimizationScenario
from domoai.optimizer.scenario import validate_scenario as validate_scenario_model
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
    runtime_revision: str


def explain_result(result: OptimizationResult) -> OptimizationExplanation:
    hard_satisfied = result.constraint_summary.get("hard_satisfied")
    if result.plan is not None and result.status in {
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
    }:
        if hard_satisfied is True:
            summary = (
                f"A {result.status.value} proposal satisfies all declared hard constraints."
            )
        else:
            summary = (
                f"A {result.status.value} proposal was produced without complete "
                "hard-constraint evidence."
            )
        proposal = {
            "plan_id": result.plan.id,
            "status": result.plan.status.value,
            "commands": [command.model_dump(mode="json") for command in result.plan.commands],
        }
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

    @server.tool(
        name="validate_scenario",
        description="Validate an optimization scenario against canonical devices and capabilities.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def validate_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = OptimizationScenario.model_validate(scenario)
            diagnostics = validate_scenario_model(parsed, context.registry)
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
    async def optimize_scenario(
        scenario: dict[str, Any], validate_proposal: bool = True
    ) -> dict[str, Any]:
        try:
            parsed = OptimizationScenario.model_validate(scenario)
            result = context.optimization_service.optimize(parsed)
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
