"""Fail when front-door docs drift from the current runtime contracts."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def main() -> None:
    skill = _read("skills/core/optimize-home-energy/SKILL.md")
    readme = _read("README.md")
    unified = _read("docs/unified-mcp.md")
    workflow = _read("src/domoai/skills/workflow.py")
    schema = json.loads(
        (ROOT / "schemas/v1/optimization-result.schema.json").read_text(encoding="utf-8")
    )

    required = {
        "skill v3": "contract_version: v3" in skill,
        "bundle operation": (
            "commit_or_schedule_bundle" in skill and "commit_or_schedule_bundle" in readme
        ),
        "v3 implementation default": "V3_OPERATION_BINDINGS" in workflow,
        "no-action status": "no_action_required" in json.dumps(schema),
        "qualification language": "software-qualified" in readme and "hil-qualified" in readme,
        "HIL caveat": "HIL" in readme and "not executed" in readme,
    }
    stale = {
        "obsolete portable v2 wording": "portable v2 procedure" in readme,
        "obsolete skill default": "execute_plan` as the skill mutation" in skill,
    }
    failures = [name for name, present in required.items() if not present]
    failures.extend(name for name, present in stale.items() if present)
    if failures:
        raise SystemExit("runtime contract documentation drift: " + ", ".join(failures))
    # Keep the unified MCP page in the same contract family; this assertion is
    # intentionally small because its generic execute_plan discussion remains
    # valid for non-energy MCP callers.
    if "one public local MCP process" not in unified:
        raise SystemExit("unified MCP front-door contract is missing")
    print("runtime contract documentation is coherent")


if __name__ == "__main__":
    main()
