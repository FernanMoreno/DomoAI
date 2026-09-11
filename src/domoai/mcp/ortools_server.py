"""Proposal-only MCP server for the OR-Tools optimization boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError, model_validator

from domoai.application.authority import AuthorityPolicy
from domoai.application.optimization_service import OptimizationService
from domoai.application.optimization_worker import OptimizationWorker
from domoai.application.plan_service import PlanService
from domoai.domain.models import AuthorityContext, ErrorDetail, Plan, StrictModel
from domoai.domain.product import (
    ScenarioComparison,
    ScenarioComparisonVariation,
    validate_accounting,
)
from domoai.mcp.auth import current_authority
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.errors import error_envelope
from domoai.mcp.request_context import with_request_principal
from domoai.optimizer.counterfactual import (
    CounterfactualAnalyzer,
    ScenarioComparisonRequest,
    VariationOutcome,
)
from domoai.optimizer.ports import (
    AlternativeEvidence,
    BoundedOptimizerWorkerPort,
    OptimizationResult,
    OptimizationStatus,
    build_result,
)
from domoai.optimizer.product import build_product_summary
from domoai.optimizer.scenario import (
    MAX_HORIZON_SLOTS,
    OptimizationScenario,
    scenario_definition_digest,
    validate_executable_scenario,
)
from domoai.optimizer.scenario import (
    validate_scenario as validate_scenario_model,
)
from domoai.runtime.registry import DeviceRegistry


class OptimizationExplanation(StrictModel):
    schema_version: str = "v1"
    scenario_id: str
    definition_digest: str | None = None
    status: OptimizationStatus
    solver: str
    summary: str
    objective_values: dict[str, float]
    constraint_summary: dict[str, Any]
    diagnostics: list[ErrorDetail]
    proposal: dict[str, Any] | None = None
    proposal_count: int = 0
    alternatives: list[dict[str, Any]] = []
    hard_constraints_satisfied: bool | None = None
    soft_violations: list[dict[str, Any]] = []
    forecast_confidence: str | None = None
    next_step: str = "revise_scenario_or_inputs"

    @model_validator(mode="after")
    def validate_evidence(self) -> OptimizationExplanation:
        validate_accounting(self.constraint_summary, self.objective_values)
        for alternative in self.alternatives:
            validate_accounting(
                alternative.get("constraint_effects", {}),
                alternative.get("objective_values", {}),
            )
        return self


@dataclass
class OrtoolsMcpContext:
    registry: DeviceRegistry
    plan_service: PlanService
    optimization_service: OptimizationService
    optimization_worker: BoundedOptimizerWorkerPort | None = None
    max_horizon_slots: int = MAX_HORIZON_SLOTS
    authority_policy: AuthorityPolicy = field(default_factory=AuthorityPolicy)

    @property
    def runtime_revision(self) -> str:
        return self.plan_service.current_revision


def _request_authority(context: OrtoolsMcpContext) -> AuthorityContext:
    return current_authority() or context.authority_policy.local_context()


def _authorize_resources(
    context: OrtoolsMcpContext,
    *,
    device_ids: tuple[str, ...],
    capabilities: tuple[str, ...],
) -> None:
    authority = _request_authority(context)
    policy = context.authority_policy
    policy.authorize(
        authority,
        operation="inspect",
        device_ids=device_ids,
        capabilities=capabilities,
    )
    if authority.area_ids and any(
        (device := context.registry.get(device_id)) is None
        or device.area_id not in authority.area_ids
        for device_id in device_ids
    ):
        # An empty area is deliberately passed to the policy so an active area
        # allowlist rejects missing and out-of-area canonical registry entries.
        policy.authorize(authority, operation="inspect", area_id="")


def _scenario_resources(scenario: OptimizationScenario) -> tuple[tuple[str, ...], tuple[str, ...]]:
    device_ids: list[str] = []
    capabilities: list[str] = []

    def add(device_id: str, *commands: str) -> None:
        device_ids.append(device_id)
        capabilities.extend(command for command in commands if command)

    for standard_load in scenario.loads:
        add(standard_load.device_id, standard_load.command, standard_load.end_command or "")
    for ev_load in scenario.ev_loads:
        add(ev_load.device_id, ev_load.command)
    for comfort_load in scenario.comfort_loads:
        add(comfort_load.device_id, comfort_load.command, comfort_load.end_command or "")
    energy_context = scenario.energy_context
    if energy_context is not None:
        for state in energy_context.ev_states:
            device_ids.append(state.device_id)
        if energy_context.battery is not None and energy_context.battery.actuator is not None:
            battery_actuator = energy_context.battery.actuator
            add(
                battery_actuator.device_id,
                battery_actuator.charge_command,
                battery_actuator.discharge_command,
                battery_actuator.stop_command,
            )
        if energy_context.thermal is not None and energy_context.thermal.actuator is not None:
            hvac_actuator = energy_context.thermal.actuator
            add(
                hvac_actuator.device_id,
                hvac_actuator.heat_command,
                hvac_actuator.cool_command,
                hvac_actuator.stop_command,
            )
    return tuple(dict.fromkeys(device_ids)), tuple(dict.fromkeys(capabilities))


def _authorize_scenario(context: OrtoolsMcpContext, scenario: OptimizationScenario) -> None:
    device_ids, capabilities = _scenario_resources(scenario)
    _authorize_resources(context, device_ids=device_ids, capabilities=capabilities)


def _plan_resources(plan: Plan) -> tuple[tuple[str, ...], tuple[str, ...]]:
    device_ids = tuple(command.device_id for command in plan.commands)
    capabilities = tuple(
        capability
        for command in plan.commands
        for capability in (
            command.command,
            *(postcondition.capability for postcondition in command.postconditions),
        )
    )
    return device_ids, capabilities


def _authorize_result(context: OrtoolsMcpContext, result: OptimizationResult) -> None:
    plans = list(result.plans)
    if result.plan is not None and result.plan not in plans:
        plans.insert(0, result.plan)
    if not plans:
        _authorize_resources(context, device_ids=(), capabilities=())
    for plan in plans:
        device_ids, capabilities = _plan_resources(plan)
        _authorize_resources(context, device_ids=device_ids, capabilities=capabilities)


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
    bundle = result.plans or ([result.plan] if result.plan is not None else [])
    soft_violations = result.constraint_summary.get("soft_violations", [])
    if not isinstance(soft_violations, list):
        soft_violations = []
    hard_effect = (
        hard_satisfied if isinstance(hard_satisfied, bool) else None
    )
    shared_constraint_effects: dict[str, Any] = {
        "hard_satisfied": hard_effect,
        "soft_violations": soft_violations[:16],
    }
    shared_objective_values = dict(list(result.objective_values.items())[:32])
    accounting = validate_accounting(result.constraint_summary, result.objective_values)
    if accounting is not None:
        # Fallback values are bounded together with their unit and comparison keys.
        accounting = accounting.model_copy(deep=True)
        accounting.objective_units = {
            key: unit
            for key, unit in accounting.objective_units.items()
            if key in shared_objective_values
        }
        if accounting.comparison is not None:
            accounting.comparison.diff_units = {
                key: unit
                for key, unit in accounting.comparison.diff_units.items()
                if key in shared_objective_values
            }
            accounting.comparison.omitted_objectives = {
                key: reason
                for key, reason in accounting.comparison.omitted_objectives.items()
                if key in shared_objective_values
            }
        shared_constraint_effects["accounting"] = accounting.model_dump(
            mode="json", exclude_unset=True
        )
    shared_forecast_assumptions = _forecast_assumptions(result)
    alternatives = []
    for item in bundle[:16]:
        evidence = result.alternative_evidence.get(item.id)
        if evidence is None:
            evidence = AlternativeEvidence(
                objective_values=shared_objective_values,
                constraint_effects=shared_constraint_effects,
                forecast_assumptions=shared_forecast_assumptions,
            )
        alternatives.append(
            {
                "plan_id": item.id,
                "status": item.status.value,
                "objective_values": evidence.objective_values,
                "constraint_effects": evidence.constraint_effects,
                "forecast_assumptions": evidence.forecast_assumptions,
            }
        )
    next_step = (
        "validate_and_request_approval_before_execution"
        if proposal is not None
        else (
            "no_physical_action_required"
            if result.status is OptimizationStatus.NO_ACTION_REQUIRED
            else "revise_scenario_or_inputs"
        )
    )
    forecast_confidence = result.constraint_summary.get("forecast_confidence")
    if not isinstance(forecast_confidence, str):
        forecast_confidence = None
    return OptimizationExplanation(
        scenario_id=result.scenario_id,
        definition_digest=result.definition_digest,
        status=result.status,
        solver=result.solver,
        summary=summary,
        objective_values=result.objective_values,
        constraint_summary=result.constraint_summary,
        diagnostics=result.diagnostics,
        proposal=proposal,
        proposal_count=len(bundle) if proposal is not None else 0,
        alternatives=alternatives,
        hard_constraints_satisfied=(
            hard_satisfied if isinstance(hard_satisfied, bool) else None
        ),
        soft_violations=soft_violations[:16],
        forecast_confidence=forecast_confidence,
        next_step=next_step,
    )


def _forecast_assumptions(result: OptimizationResult) -> dict[str, Any]:
    explicit = result.constraint_summary.get("forecast_assumptions")
    if isinstance(explicit, dict):
        return dict(list(explicit.items())[:16])
    assumptions: dict[str, Any] = {}
    confidence = result.constraint_summary.get("forecast_confidence")
    if confidence is not None:
        assumptions["confidence"] = confidence
    conservative = result.objective_values.get("conservative_mode_active")
    if conservative is not None:
        assumptions["conservative"] = conservative == 1.0
    return assumptions


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
            _authorize_scenario(context, parsed)
            diagnostics = validate_scenario_model(
                parsed, context.registry, max_horizon_slots=context.max_horizon_slots
            )
            return {
                "schema_version": "v1",
                "scenario_id": parsed.id,
                "definition_digest": scenario_definition_digest(parsed),
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
            _authorize_scenario(context, parsed)
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
                            definition_digest=scenario_definition_digest(parsed),
                            diagnostics=[item.model_dump(mode="json") for item in diagnostics],
                        ).model_dump(mode="json"),
                    }
            result = await worker.optimize(parsed)
            if result.definition_digest != scenario_definition_digest(parsed):
                result = result.model_copy(
                    update={"definition_digest": scenario_definition_digest(parsed)}
                )
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
            _authorize_result(context, parsed)
            return explain_result(parsed).model_dump(mode="json")
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="summarize_solution",
        description=(
            "Build a deterministic, read-only product summary from an optimization result."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def summarize_solution(result: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = OptimizationResult.model_validate(result)
            _authorize_result(context, parsed)
            return build_product_summary(parsed).model_dump(mode="json")
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="compare_scenarios",
        description=(
            "Compare one baseline and bounded named scenario variations without "
            "executing commands or changing runtime state."
        ),
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def compare_scenarios(
        baseline: dict[str, Any], variations: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        try:
            request = ScenarioComparisonRequest.model_validate(
                {"baseline": baseline, "variations": variations or {}}
            )
            parsed_scenarios: list[tuple[str | None, OptimizationScenario]] = [
                (None, request.baseline), *request.variations.items()
            ]
            invalid_baseline: OptimizationResult | None = None
            invalid_variations: dict[str, OptimizationResult] = {}
            for name, scenario in parsed_scenarios:
                _authorize_scenario(context, scenario)
                diagnostics = validate_scenario_model(
                    scenario, context.registry, max_horizon_slots=context.max_horizon_slots
                )
                if scenario.ev_loads and not diagnostics:
                    diagnostics = validate_executable_scenario(
                        scenario,
                        context.registry,
                        max_horizon_slots=context.max_horizon_slots,
                    )
                if diagnostics:
                    invalid_result = build_result(
                        scenario_id=scenario.id,
                        status=OptimizationStatus.INVALID,
                        definition_digest=scenario_definition_digest(scenario),
                        diagnostics=[item.model_dump(mode="json") for item in diagnostics],
                    )
                    if name is None:
                        invalid_baseline = invalid_result
                    else:
                        invalid_variations[name] = invalid_result

            comparison: dict[str, Any]
            if invalid_baseline is not None:
                comparison = {
                    "baseline": invalid_baseline,
                    "variations": {},
                }
            else:
                analyzer = CounterfactualAnalyzer(worker)
                safe_variations = {
                    name: scenario
                    for name, scenario in request.variations.items()
                    if name not in invalid_variations
                }
                comparison_result = await analyzer.compare_async(
                    request.baseline, safe_variations
                )
                comparison = {
                    "baseline": comparison_result.baseline,
                    "variations": comparison_result.variations,
                }
                for name, invalid_result in invalid_variations.items():
                    comparison["variations"][name] = VariationOutcome(result=invalid_result)

            baseline_result = comparison["baseline"]
            variation_results = comparison["variations"]
            return ScenarioComparison(
                runtime_revision=context.runtime_revision,
                baseline=build_product_summary(baseline_result),
                variations={
                    name: ScenarioComparisonVariation(
                        scenario_id=outcome.result.scenario_id,
                        status=outcome.result.status.value,
                        summary=build_product_summary(outcome.result),
                        diff=dict(outcome.diff),
                    )
                    for name, outcome in variation_results.items()
                },
            ).model_dump(mode="json")
        except (ValueError, ValidationError, TypeError) as error:
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
