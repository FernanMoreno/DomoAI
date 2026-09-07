"""Executable qualification matrix for the complete deterministic twin."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.local_automation import LocalAutomationEngine
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.privacy import PrivacyService
from domoai.application.scheduler import Scheduler
from domoai.domain.automation import (
    AutomationConsent,
    AutomationRule,
    AutomationRuleStatus,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.digital_twin import (
    DigitalTwinCheck,
    DigitalTwinCheckStatus,
    DigitalTwinCoverage,
    DigitalTwinEvidence,
)
from domoai.domain.models import (
    AuthorityContext,
    Command,
    CommandPostcondition,
    Plan,
    PrincipalRole,
    SourceRef,
    StateChangedEvent,
)
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.lab.virtual_plant import VirtualHomePlant
from domoai.lab.virtual_protocols import VirtualProtocolAdapter, build_virtual_protocol_adapters
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.product import build_product_summary
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.persistence.repositories import AutomationRuleRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

REQUIRED_ADAPTERS = frozenset(
    {"fixture", "home_assistant", "knx", "modbus", "matter", "zigbee2mqtt"}
)
REQUIRED_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "cover",
        "climate",
        "environment",
        "power",
        "battery",
        "ev",
        "water",
        "solar",
    }
)
REQUIRED_CHECKS = frozenset(
    {
        "discovery",
        "normalization",
        "routing",
        "readback",
        "faults",
        "idempotency",
        "audit",
        "recovery",
        "scheduler",
        "automation",
        "optimization",
        "product",
        "privacy",
        "identity",
    }
)


class DigitalTwinQualificationRunner:
    """Run production discovery and adapter boundaries against a virtual plant."""

    def __init__(self, *, seed: int = 187, plant: VirtualHomePlant | None = None) -> None:
        self.seed = seed
        self.plant = plant or VirtualHomePlant.default(seed=seed)

    async def run(self) -> DigitalTwinEvidence:
        adapters = build_virtual_protocol_adapters(self.plant)
        registry = DeviceRegistry()
        composite = CompositeAdapter(adapters, registry=registry)
        state_store = StateStore(clock=self.plant.clock)
        audit = AuditLog(clock=self.plant.clock)
        checks: list[DigitalTwinCheck] = []

        try:
            await composite.connect()
            discovery = await DiscoveryService(composite, registry, state_store, audit).refresh()
            plan_service = PlanService(
                registry,
                state_store,
                PolicyEngine([]),
                audit,
                clock=self.plant.clock,
                authorized_actuator_commands={
                    "modbus.battery": frozenset(
                        {"charge_battery", "discharge_battery", "stop_battery"}
                    ),
                    "modbus.ev": frozenset({"charge_ev", "stop_ev"}),
                },
            )
            executor = PlanExecutor(composite, plan_service, audit, clock=self.plant.clock)
            checks.append(self._passed("discovery", devices=len(discovery.devices)))

            actual_adapters = {adapter.adapter_id for adapter in adapters}
            self._append_coverage_check(
                checks,
                "normalization",
                actual_adapters == REQUIRED_ADAPTERS
                and bool(discovery.devices)
                and len({device.id for device in discovery.devices}) == len(discovery.devices),
                code="adapter_or_identity_coverage_missing",
                details={"adapters": sorted(actual_adapters), "devices": len(discovery.devices)},
            )

            actual_domains = {device.domain for device in self.plant.devices}
            self._append_coverage_check(
                checks,
                "identity",
                REQUIRED_DOMAINS <= actual_domains,
                code="domain_coverage_missing",
                details={"missing": sorted(REQUIRED_DOMAINS - actual_domains)},
            )

            route_count = 0
            readback_count = 0
            for adapter in adapters:
                adapter_devices = [
                    device
                    for device in self.plant.devices
                    if device.adapter_id == adapter.adapter_id
                ]
                for device in adapter_devices:
                    states = await adapter.read_state(
                        [SourceRef(adapter_id=adapter.adapter_id, external_id=device.source_id)]
                    )
                    if len(states) != len(device.capabilities):
                        continue
                    for command, capability in device.commands.items():
                        route_count += 1
                        value = _command_value(capability)
                        expected_value = _expected_command_value(
                            device.state.get(capability), command, value
                        )
                        intent = Command(
                            id=f"{self.seed}:{adapter.adapter_id}:{device.source_id}:{command}",
                            device_id=device.device_id,
                            command=command,
                            value=value,
                            idempotency_key=(
                                f"{self.seed}:{adapter.adapter_id}:{device.source_id}:{command}"
                            ),
                            postconditions=[
                                CommandPostcondition(
                                    capability=capability,
                                    expected=None if command == "toggle" else expected_value,
                                    verification=(
                                        "toggle_transition" if command == "toggle" else "equals"
                                    ),
                                )
                            ],
                        )
                        validated = plan_service.validate(
                            Plan(
                                id=f"{self.seed}:matrix:{adapter.adapter_id}:{device.source_id}:{command}",
                                commands=[intent],
                            )
                        )
                        if validated.status.value == "requires_confirmation":
                            approvals = ApprovalStore(clock=self.plant.clock)
                            grant = approvals.issue_attended_local(
                                validated,
                                operator_id="digital-twin",
                                session_id=f"twin:{self.seed}",
                            )
                            validated = plan_service.approve(
                                validated,
                                grant=approvals.consume(grant.approval_id, validated),
                            )
                        summary = await executor.execute(validated)
                        if summary.outcomes[0].status.value != "confirmed_success":
                            continue
                        refreshed = await adapter.read_state(
                            [SourceRef(adapter_id=adapter.adapter_id, external_id=device.source_id)]
                        )
                        if any(
                            snapshot.capability == capability and snapshot.value == expected_value
                            for snapshot in refreshed
                        ):
                            readback_count += 1

            self._append_coverage_check(
                checks,
                "routing",
                route_count > 0,
                code="no_writable_routes",
                details={"routes_exercised": route_count},
            )
            self._append_coverage_check(
                checks,
                "readback",
                readback_count == route_count,
                code="readback_incomplete",
                details={"routes": route_count, "readbacks": readback_count},
            )

            await self._run_fault_checks(adapters, checks)
            idempotency_before = self.plant.write_count
            first = self.plant.command(
                "fixture",
                "fixture.light",
                "turn_on",
                idempotency_key=f"{self.seed}:qualification-idempotency",
            )
            duplicate = self.plant.command(
                "fixture",
                "fixture.light",
                "turn_on",
                idempotency_key=f"{self.seed}:qualification-idempotency",
            )
            self._append_coverage_check(
                checks,
                "idempotency",
                self.plant.write_count == idempotency_before + 1
                and duplicate.revision == first.revision,
                code="duplicate_command_mutated_plant",
                details={"writes_added": self.plant.write_count - idempotency_before},
            )
            recovered_state = StateStore(clock=self.plant.clock)
            recovered_state.restore_metadata(state_store.export_metadata())
            persisted_snapshots = await state_store.all()
            recovered_state.load_persisted(list(persisted_snapshots))
            source_snapshot = state_store.peek("fixture.switch", "power")
            recovered_snapshot = recovered_state.peek("fixture.switch", "power")
            self._append_coverage_check(
                checks,
                "recovery",
                source_snapshot is not None
                and recovered_snapshot is not None
                and recovered_snapshot.value == source_snapshot.value
                and recovered_snapshot.status.value == "stale",
                code="state_restart_recovery_lost_snapshot",
                details={"snapshots": len(persisted_snapshots)},
            )
            await self._run_cross_cutting_checks(
                composite,
                registry,
                state_store,
                plan_service,
                executor,
                audit,
                checks,
            )
            audit_event_types = {event.event_type for event in audit.events}
            required_audit_event_types = {
                "discovery_completed",
                "plan_execution_started",
                "plan_execution_completed",
            }
            self._append_coverage_check(
                checks,
                "audit",
                required_audit_event_types <= audit_event_types,
                code="audit_chain_incomplete",
                details={
                    "events": len(audit.events),
                    "event_types": sorted(audit_event_types),
                    "missing": sorted(required_audit_event_types - audit_event_types),
                },
            )

        except Exception as error:
            checks.append(
                DigitalTwinCheck(
                    check_id="runner",
                    status=DigitalTwinCheckStatus.FAILED,
                    code="runner_failed",
                    message=f"{type(error).__name__}: {str(error)[:180]}",
                )
            )
        finally:
            await composite.disconnect()

        checks = sorted(checks, key=lambda check: check.check_id)
        actual_check_ids = {check.check_id for check in checks}
        missing_checks = sorted(REQUIRED_CHECKS - actual_check_ids)
        if missing_checks:
            checks.append(
                DigitalTwinCheck(
                    check_id="coverage",
                    status=DigitalTwinCheckStatus.FAILED,
                    code="required_check_missing",
                    message="Required qualification checks were not produced",
                    details={"missing": missing_checks},
                )
            )
        return DigitalTwinEvidence(
            run_id=f"twin-{self.seed}",
            seed=self.seed,
            plant_digest=self.plant.initial_digest,
            trace_digest=self.plant.trace_digest(),
            coverage=DigitalTwinCoverage(
                adapters=sorted(REQUIRED_ADAPTERS),
                domains=sorted(REQUIRED_DOMAINS),
                checks=sorted(REQUIRED_CHECKS),
            ),
            checks=sorted(checks, key=lambda check: check.check_id),
            invariant_violations=self.plant.invariant_violations(),
        )

    async def _run_fault_checks(
        self, adapters: Iterable[VirtualProtocolAdapter], checks: list[DigitalTwinCheck]
    ) -> None:
        fixture = next(adapter for adapter in adapters if adapter.adapter_id == "fixture")
        self.plant.set_fault("fixture", "fixture.light", "unavailable")
        snapshots = await fixture.read_state(
            [SourceRef(adapter_id="fixture", external_id="fixture.light")]
        )
        unavailable = bool(snapshots) and all(
            snapshot.status.value == "unavailable" for snapshot in snapshots
        )
        self.plant.set_fault("fixture", "fixture.light", None)
        self.plant.set_fault("fixture", "fixture.light", "stale")
        stale_snapshots = await fixture.read_state(
            [SourceRef(adapter_id="fixture", external_id="fixture.light")]
        )
        stale = bool(stale_snapshots) and all(
            snapshot.status.value == "stale" for snapshot in stale_snapshots
        )
        self.plant.set_fault("fixture", "fixture.light", None)

        rejected: dict[str, bool] = {}
        for fault in ("rejected", "partial_failure"):
            self.plant.set_fault("fixture", "fixture.light", fault)
            result = await fixture.execute(
                Command(
                    id=f"{self.seed}:fault:{fault}",
                    device_id="fixture.light",
                    command="turn_on",
                    idempotency_key=f"{self.seed}:fault:{fault}",
                )
            )
            rejected[fault] = not result.accepted
            self.plant.set_fault("fixture", "fixture.light", None)

        self.plant.set_fault("fixture", "fixture.light", "delayed")
        delayed = self.plant.command(
            "fixture",
            "fixture.light",
            "turn_on",
            idempotency_key=f"{self.seed}:fault:delayed",
        )
        delayed_recorded = delayed.available and any(
            entry["operation"] == "command_delayed" for entry in self.plant.trace
        )
        self.plant.set_fault("fixture", "fixture.light", None)
        # Exercise event ordering on a capability that is independent of the
        # command assertions above, keeping the expected final value explicit.
        event_applied = self.plant.deliver_event(
            "fixture",
            "fixture.light",
            "brightness",
            75,
            observed_at=self.plant.clock.now(),
            revision=100,
        )
        event_discarded = not self.plant.deliver_event(
            "fixture",
            "fixture.light",
            "brightness",
            25,
            observed_at=self.plant.clock.now(),
            revision=99,
        )
        self._append_coverage_check(
            checks,
            "faults",
            unavailable
            and stale
            and all(rejected.values())
            and delayed_recorded
            and event_applied
            and event_discarded,
            code="fault_not_propagated_or_ordered",
            details={
                "unavailable_states": len(snapshots),
                "stale_states": len(stale_snapshots),
                "rejected": rejected,
                "event_ordering": event_discarded,
            },
        )

    async def _run_cross_cutting_checks(
        self,
        composite: CompositeAdapter,
        registry: DeviceRegistry,
        state_store: StateStore,
        plan_service: Any,
        executor: Any,
        audit: AuditLog,
        checks: list[DigitalTwinCheck],
    ) -> None:
        """Exercise product services against the same plant, not placeholders."""

        database: SQLiteDatabase | None = None
        with tempfile.TemporaryDirectory(prefix="domoai-twin-") as directory:
            database = SQLiteDatabase(
                Path(directory) / "qualification.sqlite3", clock=self.plant.clock
            )
            await database.initialize()
            try:
                scheduler = Scheduler(
                    executor,
                    ScheduledPlanRepository(database, clock=self.plant.clock),
                    audit,
                    clock=self.plant.clock,
                )
                scheduled = plan_service.validate(
                    Plan(
                        id=f"twin-scheduler-{self.seed}",
                        execute_at=self.plant.clock.now(),
                        commands=[
                            Command(
                                id=f"twin-scheduler-command-{self.seed}",
                                device_id="fixture.switch",
                                command="turn_on",
                                idempotency_key=f"twin-scheduler-key-{self.seed}",
                            )
                        ],
                    )
                )
                await scheduler.schedule(scheduled)
                scheduler_results = await scheduler.run_due()
                self._append_coverage_check(
                    checks,
                    "scheduler",
                    scheduler_results == [{"plan_id": scheduled.id, "outcome": "executed"}],
                    code="scheduler_did_not_close_loop",
                    details={"result": scheduler_results},
                )

                automation_repository = AutomationRuleRepository(database, clock=self.plant.clock)
                automation = LocalAutomationEngine(
                    automation_repository,
                    plan_service,
                    executor,
                    audit,
                    state_store=state_store,
                    clock=self.plant.clock,
                )
                automation_rule = AutomationRule(
                    id=f"twin-automation-{self.seed}",
                    name="Digital twin switch response",
                    trigger=AutomationTrigger(
                        type="state_changed",
                        device_id="fixture.switch",
                        capability="power",
                        expected=True,
                    ),
                    plan_template=Plan(
                        id=f"twin-automation-template-{self.seed}",
                        commands=[
                            Command(
                                id=f"twin-automation-command-{self.seed}",
                                device_id="fixture.switch",
                                command="turn_off",
                                idempotency_key=f"twin-automation-key-{self.seed}",
                            )
                        ],
                    ),
                    scope="digital-twin",
                    status=AutomationRuleStatus.ENABLED,
                )
                automation_consent = AutomationConsent(
                    approval_id=f"twin-automation-approval-{self.seed}",
                    principal_id="digital-twin",
                    scope=automation_rule.scope,
                    rule_digest=automation_rule_digest(automation_rule),
                    approved_at=self.plant.clock.now(),
                    expires_at=self.plant.clock.now() + timedelta(hours=1),
                )
                await automation.register(automation_rule, automation_consent)
                evaluations = await automation.handle_state_event(
                    StateChangedEvent(
                        source_adapter_id="fixture",
                        external_id="fixture.switch",
                        device_id="fixture.switch",
                        capability="power",
                        value=True,
                        available=True,
                        occurred_at=self.plant.clock.now(),
                    )
                )
                self._append_coverage_check(
                    checks,
                    "automation",
                    len(evaluations) == 1 and evaluations[0].status == "executed",
                    code="automation_did_not_execute_on_twin_event",
                    details={"evaluations": len(evaluations)},
                )

                optimizer = OptimizationService(registry, plan_service, CpSatOptimizer(registry))
                horizon = Horizon(
                    start=self.plant.clock.now(),
                    end=self.plant.clock.now() + timedelta(hours=1),
                    resolution_minutes=15,
                    timezone="UTC",
                )
                optimization_result = optimizer.optimize(
                    OptimizationScenario(
                        id=f"twin-optimization-{self.seed}",
                        horizon=horizon,
                        loads=[
                            Load(
                                id=f"twin-load-{self.seed}",
                                device_id="fixture.light",
                                capability="brightness",
                                command="set_brightness",
                                value=50,
                                power=100,
                                power_unit="W",
                            )
                        ],
                        constraints=[Constraint(type="max_house_power", value=500, unit="W")],
                    )
                )
                product_summary = build_product_summary(optimization_result)
                validated_proposal = optimizer.validate_proposal(optimization_result)
                optimization_ok = (
                    optimization_result.plan is not None
                    and product_summary.proposal_id == optimization_result.plan.id
                    and validated_proposal.plan is not None
                )
                self._append_coverage_check(
                    checks,
                    "optimization",
                    optimization_ok,
                    code="optimizer_did_not_produce_validated_proposal",
                    details={"status": optimization_result.status.value},
                )
                self._append_coverage_check(
                    checks,
                    "product",
                    product_summary.next_step == "validate_and_request_approval_before_execution",
                    code="product_summary_missing_safe_next_step",
                    details={"next_step": product_summary.next_step},
                )

                privacy = PrivacyService(_TwinPrivacyStore(), audit=audit.append)
                authority = AuthorityContext(
                    principal_id="digital-twin",
                    roles=[PrincipalRole.SERVICE],
                )
                policy = HouseholdDataPolicy(
                    authority=authority,
                    exportable_categories=[PrivacyCategory.PLANS],
                    deletable_categories=[PrivacyCategory.PLANS],
                    immutable_categories=[PrivacyCategory.AUDIT],
                    retention_days=90,
                )
                exported = await privacy.export(
                    policy,
                    authority,
                    categories=[PrivacyCategory.PLANS],
                )
                self._append_coverage_check(
                    checks,
                    "privacy",
                    exported.record_count == 1
                    and "token" not in json.dumps(exported.model_dump(mode="json")),
                    code="privacy_export_not_scoped_or_sanitized",
                    details={"records": exported.record_count},
                )
            finally:
                await database.close()

    @staticmethod
    def _passed(check_id: str, **details: Any) -> DigitalTwinCheck:
        return DigitalTwinCheck(check_id=check_id, details=details)

    @classmethod
    def _append_coverage_check(
        cls,
        checks: list[DigitalTwinCheck],
        check_id: str,
        passed: bool,
        *,
        code: str,
        details: dict[str, Any],
    ) -> None:
        checks.append(
            cls._passed(check_id, **details)
            if passed
            else DigitalTwinCheck(
                check_id=check_id,
                status=DigitalTwinCheckStatus.FAILED,
                code=code,
                message="Digital-twin qualification check failed",
                details=details,
            )
        )


def _command_value(capability: str) -> bool | int | float | str | None:
    if capability == "brightness":
        return 50
    if capability == "target_temperature":
        return 21
    if capability in {"position"}:
        return 50
    if capability in {"battery.power", "ev_charging", "thermal.hvac_power"}:
        return 1.0
    if capability == "thermal.hvac_mode":
        return "heat"
    return None


def _expected_command_value(
    current: bool | int | float | str | None,
    command: str,
    value: bool | int | float | str | None,
) -> bool | int | float | str | None:
    if command == "turn_on":
        return True
    if command == "turn_off":
        return False
    if command == "toggle":
        return not bool(current)
    if command == "open":
        return 100
    if command == "close":
        return 0
    if command in {"stop", "stop_battery", "stop_ev"}:
        return 0.0
    if command == "discharge_battery" and isinstance(value, (int, float)):
        return -abs(value)
    return value


class _TwinPrivacyStore:
    """Small deterministic store used to exercise the real privacy service."""

    async def export_category(
        self, category: PrivacyCategory, household_id: str
    ) -> list[dict[str, Any]]:
        if category is not PrivacyCategory.PLANS:
            return []
        return [
            {
                "household_id": household_id,
                "plan_id": "twin-privacy-plan",
                "token": "must-not-leak",
            }
        ]

    async def delete_category(self, category: PrivacyCategory, household_id: str) -> int:
        return 1 if category is PrivacyCategory.PLANS and household_id else 0


def render_markdown(evidence: DigitalTwinEvidence) -> str:
    """Render only typed, already-sanitized evidence fields for operators."""

    lines = [
        "# Digital twin qualification",
        "",
        f"- Status: `{evidence.status.value}`",
        f"- Scope: `{evidence.scope}`",
        f"- Run: `{evidence.run_id}`",
        f"- Seed: `{evidence.seed}`",
        f"- Plant digest: `{evidence.plant_digest}`",
        f"- Trace digest: `{evidence.trace_digest}`",
        "",
        "## Coverage",
        "",
        "| Surface | Items |",
        "|---|---|",
        f"| Adapters | {', '.join(evidence.coverage.adapters)} |",
        f"| Domains | {', '.join(evidence.coverage.domains)} |",
        f"| Checks | {', '.join(evidence.coverage.checks)} |",
        "",
        "## Checks",
        "",
        "| Check | Status | Code | Details |",
        "|---|---|---|---|",
    ]
    for check in evidence.checks:
        details = json.dumps(check.details, sort_keys=True, separators=(",", ":"))
        lines.append(
            f"| `{check.check_id}` | `{check.status.value}` | `{check.code or ''}` | `{details}` |"
        )
    lines.extend(["", "## Invariant violations", ""])
    if evidence.invariant_violations:
        lines.extend(f"- `{violation}`" for violation in evidence.invariant_violations)
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "This report proves the software closed loop on a deterministic virtual "
            "plant. It is not physical commissioning or HIL evidence.",
            "",
        ]
    )
    return "\n".join(lines)


async def run_digital_twin(*, seed: int = 187) -> DigitalTwinEvidence:
    return await DigitalTwinQualificationRunner(seed=seed).run()


__all__ = [
    "REQUIRED_ADAPTERS",
    "REQUIRED_CHECKS",
    "REQUIRED_DOMAINS",
    "DigitalTwinQualificationRunner",
    "render_markdown",
    "run_digital_twin",
]
