from pathlib import Path

from domoai.skills.validator import validate_skill

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = ROOT / "skills" / "core" / "optimize-home-energy" / "SKILL.md"


def test_energy_skill_gathers_context_optimizes_and_pauses_before_execution() -> None:
    procedure = validate_skill(SKILL_PATH)

    assert procedure.name == "optimize-home-energy"
    assert procedure.operations == (
        "discover_devices",
        "get_state",
        "get_energy_context",
        "optimize_scenario",
        "validate_plan",
        "explain_solution",
        "operator_approval",
        "commit_or_schedule_bundle",
    )
    assert procedure.approval_required is True
    assert procedure.operations.index("operator_approval") < procedure.operations.index(
        "commit_or_schedule_bundle"
    )
    assert procedure.bindings[0].provider == "mcp"
    assert procedure.bindings[-1].tool == "commit_or_schedule_bundle"
