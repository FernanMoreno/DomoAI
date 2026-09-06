from pathlib import Path

from domoai.lab.cli import build_parser


def test_preflight_cli_accepts_seed_and_both_report_paths() -> None:
    args = build_parser().parse_args(
        [
            "preflight",
            "--seed",
            "9",
            "--report",
            "preflight.md",
            "--json-report",
            "preflight.json",
        ]
    )

    assert args.operation == "preflight"
    assert args.seed == 9
    assert args.report == Path("preflight.md")
    assert args.json_report == Path("preflight.json")


def test_preflight_cli_defaults_to_phase4_evidence_paths() -> None:
    args = build_parser().parse_args(["preflight"])

    assert args.report == Path("docs/evidence/phase4-preflight-latest.md")
    assert args.json_report == Path("docs/evidence/phase4-preflight-latest.json")
