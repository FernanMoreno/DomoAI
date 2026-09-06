from __future__ import annotations

from pathlib import Path

from domoai.domain.digital_twin import DigitalTwinCheck, DigitalTwinCoverage, DigitalTwinEvidence
from domoai.lab.cli import build_parser
from domoai.lab.qualification import render_markdown


def test_twin_cli_accepts_seed_and_report_path() -> None:
    args = build_parser().parse_args(["twin", "--seed", "9", "--report", "evidence.md"])

    assert args.operation == "twin"
    assert args.seed == 9
    assert args.report == Path("evidence.md")


def test_markdown_report_is_sanitized_and_contains_coverage() -> None:
    evidence = DigitalTwinEvidence(
        run_id="twin-1",
        seed=1,
        plant_digest="a" * 64,
        trace_digest="b" * 64,
        coverage=DigitalTwinCoverage(
            adapters=["fixture"], domains=["light"], checks=["readback"]
        ),
        checks=[DigitalTwinCheck(check_id="readback", details={"routes": 1})],
    )

    report = render_markdown(evidence)

    assert "# Digital twin qualification" in report
    assert "fixture" in report
    assert "readback" in report
    assert "password" not in report.casefold()
