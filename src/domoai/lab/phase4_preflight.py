"""Run and render the non-authoritative Phase 4 qualification preflight."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from domoai.domain.digital_twin import DigitalTwinEvidence, DigitalTwinRunStatus
from domoai.domain.phase4_preflight import (
    PHYSICAL_GATE_IDS,
    Phase4Gate,
    Phase4GateKind,
    Phase4GateStatus,
    Phase4PreflightReport,
)
from domoai.lab.runner import FIXTURE_SMOKE_TESTS
from domoai.runtime.clock import Clock, SystemClock


class PreflightClock(Protocol):
    def now(self) -> datetime: ...


TwinRunner = Callable[[int], Awaitable[DigitalTwinEvidence]]
ProcessSmokeRunner = Callable[[], int]

_PHYSICAL_RESIDUALS = (
    "battery_hil",
    "live_protocol_commissioning",
    "external_provider",
)
_PHYSICAL_BLOCKERS = {
    "battery_hil": {
        "reason": "hardware_not_available",
        "spec": "133_battery_hil_certification",
    },
    "live_protocol_commissioning": {
        "reason": "physical_bus_not_available",
        "spec": "140_real_composition_tests",
    },
    "external_provider": {
        "reason": "independent_provider_not_deployed",
        "spec": "141_provider_contract_tests",
    },
}


def _twin_gate(evidence: DigitalTwinEvidence) -> Phase4Gate:
    failed = evidence.status is DigitalTwinRunStatus.FAILED
    failed_checks = [check.code for check in evidence.checks if check.code]
    details = {
        "run_id": evidence.run_id,
        "seed": evidence.seed,
        "plant_digest": evidence.plant_digest,
        "trace_digest": evidence.trace_digest,
        "adapters": evidence.coverage.adapters,
        "domains": evidence.coverage.domains,
        "checks": evidence.coverage.checks,
        "invariant_violations": evidence.invariant_violations,
        "failed_check_codes": failed_checks,
    }
    return Phase4Gate(
        gate_id="digital_twin",
        kind=Phase4GateKind.SOFTWARE,
        status=Phase4GateStatus.FAILED if failed else Phase4GateStatus.PASSED,
        evidence_scope="software",
        details=details,
        exit_code=1 if failed else 0,
    )


def _failed_twin_gate(error: Exception) -> Phase4Gate:
    return Phase4Gate(
        gate_id="digital_twin",
        kind=Phase4GateKind.SOFTWARE,
        status=Phase4GateStatus.FAILED,
        evidence_scope="software",
        details={"error_code": type(error).__name__},
        exit_code=1,
    )


def _process_gate(exit_code: int) -> Phase4Gate:
    status = Phase4GateStatus.PASSED if exit_code == 0 else Phase4GateStatus.FAILED
    return Phase4Gate(
        gate_id="process_lab",
        kind=Phase4GateKind.PROCESS_LAB,
        status=status,
        evidence_scope="process_lab",
        details={"tests": list(FIXTURE_SMOKE_TESTS)},
        exit_code=exit_code,
    )


def _failed_process_gate(error: Exception) -> Phase4Gate:
    return Phase4Gate(
        gate_id="process_lab",
        kind=Phase4GateKind.PROCESS_LAB,
        status=Phase4GateStatus.FAILED,
        evidence_scope="process_lab",
        details={"error_code": type(error).__name__, "tests": list(FIXTURE_SMOKE_TESTS)},
        exit_code=1,
    )


def _blocked_physical_gates() -> list[Phase4Gate]:
    return [
        Phase4Gate(
            gate_id=gate_id,
            kind=Phase4GateKind.PHYSICAL,
            status=Phase4GateStatus.BLOCKED,
            evidence_scope="external_blocked",
            details=_PHYSICAL_BLOCKERS[gate_id],
        )
        for gate_id in sorted(PHYSICAL_GATE_IDS)
    ]


async def run_phase4_preflight(
    *,
    seed: int,
    run_twin: TwinRunner,
    run_process_smoke: ProcessSmokeRunner,
    clock: PreflightClock | Clock | None = None,
) -> Phase4PreflightReport:
    """Run software gates and explicitly block unavailable physical gates."""

    if seed <= 0:
        raise ValueError("preflight seed must be positive")
    runtime_clock = clock or SystemClock()
    started_at = runtime_clock.now().astimezone(UTC)
    try:
        twin_gate = _twin_gate(await run_twin(seed))
    except Exception as error:
        twin_gate = _failed_twin_gate(error)
    try:
        process_gate = _process_gate(run_process_smoke())
    except Exception as error:
        process_gate = _failed_process_gate(error)
    completed_at = runtime_clock.now().astimezone(UTC)
    if completed_at <= started_at:
        raise ValueError("preflight clock must advance between start and completion")
    return Phase4PreflightReport(
        run_id=f"phase4-preflight-{seed}-{started_at.strftime('%Y%m%dT%H%M%SZ')}",
        seed=seed,
        started_at=started_at,
        completed_at=completed_at,
        software_gates=[twin_gate, process_gate],
        physical_gates=_blocked_physical_gates(),
        residuals=list(_PHYSICAL_RESIDUALS),
    )


def render_preflight_json(report: Phase4PreflightReport) -> str:
    """Render stable machine-readable evidence from a validated report."""

    return report.canonical_json() + "\n"


def render_preflight_markdown(report: Phase4PreflightReport) -> str:
    """Render sanitized operator evidence without raw runner output."""

    lines = [
        "# Phase 4 qualification preflight",
        "",
        f"- Status: `{report.status.value}`",
        f"- Environment: `{report.qualification_environment}`",
        f"- Run: `{report.run_id}`",
        f"- Seed: `{report.seed}`",
        f"- Started: `{report.started_at.isoformat()}`",
        f"- Completed: `{report.completed_at.isoformat()}`",
        "",
        "## Software gates",
        "",
        "| Gate | Status | Exit code | Details |",
        "|---|---|---:|---|",
    ]
    for gate in report.software_gates:
        details = json.dumps(gate.details, sort_keys=True, separators=(",", ":"))
        lines.append(
            f"| `{gate.gate_id}` | `{gate.status.value}` | "
            f"`{gate.exit_code if gate.exit_code is not None else ''}` | `{details}` |"
        )
    lines.extend(
        [
            "",
            "## Physical gates",
            "",
            "| Gate | Status | Evidence scope | Reason |",
            "|---|---|---|---|",
        ]
    )
    for gate in report.physical_gates:
        reason = str(gate.details.get("reason", ""))
        lines.append(
            f"| `{gate.gate_id}` | `{gate.status.value}` | "
            f"`{gate.evidence_scope}` | `{reason}` |"
        )
    lines.extend(["", "## Residuals", ""])
    lines.extend(f"- `{residual}`" for residual in report.residuals)
    lines.extend(
        [
            "",
            "This report proves local software/process behavior only. It is not "
            "physical commissioning or HIL evidence and cannot enable production readiness.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "render_preflight_json",
    "render_preflight_markdown",
    "run_phase4_preflight",
]
