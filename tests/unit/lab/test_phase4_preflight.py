from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.digital_twin import DigitalTwinCoverage, DigitalTwinEvidence
from domoai.domain.phase4_preflight import Phase4PreflightStatus
from domoai.lab.phase4_preflight import (
    render_preflight_markdown,
    run_phase4_preflight,
)


class _SequenceClock:
    def __init__(self) -> None:
        self._current = datetime(2026, 9, 6, 12, tzinfo=UTC)

    def now(self) -> datetime:
        value = self._current
        self._current += timedelta(seconds=1)
        return value


def _twin() -> DigitalTwinEvidence:
    return DigitalTwinEvidence(
        run_id="twin-187",
        seed=187,
        plant_digest="a" * 64,
        trace_digest="b" * 64,
        coverage=DigitalTwinCoverage(
            adapters=["fixture"], domains=["energy"], checks=["readback"]
        ),
    )


@pytest.mark.asyncio
async def test_preflight_reports_software_pass_and_external_blocks() -> None:
    async def run_twin(seed: int) -> DigitalTwinEvidence:
        assert seed == 187
        return _twin()

    report = await run_phase4_preflight(
        seed=187,
        run_twin=run_twin,
        run_process_smoke=lambda: 0,
        clock=_SequenceClock(),
    )

    assert report.status is Phase4PreflightStatus.BLOCKED_EXTERNAL_DEPENDENCY
    assert [gate.gate_id for gate in report.software_gates] == [
        "digital_twin",
        "process_lab",
    ]
    assert all(gate.status.value == "blocked_external_dependency" for gate in report.physical_gates)
    assert report.software_gates[0].details["plant_digest"] == "a" * 64


@pytest.mark.asyncio
async def test_preflight_fails_closed_when_a_software_gate_fails() -> None:
    async def failing_twin(seed: int) -> DigitalTwinEvidence:
        del seed
        raise RuntimeError("fixture failure")

    report = await run_phase4_preflight(
        seed=187,
        run_twin=failing_twin,
        run_process_smoke=lambda: 9,
        clock=_SequenceClock(),
    )

    assert report.status is Phase4PreflightStatus.FAILED
    assert report.software_gates[0].status.value == "failed"
    assert report.software_gates[1].status.value == "failed"
    assert report.software_gates[0].details == {"error_code": "RuntimeError"}
    assert report.software_gates[1].exit_code == 9


@pytest.mark.asyncio
async def test_preflight_markdown_is_sanitized_and_explicitly_non_hardware() -> None:
    report = await run_phase4_preflight(
        seed=187,
        run_twin=lambda seed: _async_twin(seed),
        run_process_smoke=lambda: 0,
        clock=_SequenceClock(),
    )

    markdown = render_preflight_markdown(report)

    assert "# Phase 4 qualification preflight" in markdown
    assert "blocked_external_dependency" in markdown
    assert "not physical commissioning or HIL evidence" in markdown
    assert "api_token" not in markdown


async def _async_twin(seed: int) -> DigitalTwinEvidence:
    assert seed == 187
    return _twin()
