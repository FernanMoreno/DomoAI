from pathlib import Path

import pytest

from domoai.skills.validator import SkillContractError, validate_skill

SKILL = """---
name: test-skill
description: test procedure
---

## Declared operations

- `discover_devices`
- `execute_plan`

## Procedure

1. `discover_devices` — read inventory.
2. `execute_plan` — execute.
"""

VALID_BINDINGS = """
## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `optimize_scenario` → `mcp.optimize_scenario` (`proposal`)
- `validate_plan` → `mcp.validate_plan` (`validation`)
- `explain_solution` → `mcp.explain_solution` (`read`)
- `operator_approval` → `operator.request_approval` (`approval`)
- `execute_plan` → `mcp.execute_plan` (`mutation`)
"""

FULL_SKILL = (
    """---
name: test-skill
description: test procedure
---

## Declared operations

- `discover_devices`
- `get_state`
- `optimize_scenario`
- `validate_plan`
- `explain_solution`
- `operator_approval`
- `execute_plan`

## Procedure

1. `discover_devices` — read inventory.
2. `get_state` — read state.
3. `optimize_scenario` — propose.
4. `validate_plan` — validate.
5. `explain_solution` — explain.
6. `operator_approval` — approve.
7. `execute_plan` — execute.
"""
    + VALID_BINDINGS
)

V3_SKILL = (
    FULL_SKILL.replace(
        "description: test procedure\n---",
        "description: test procedure\ncontract_version: v3\n---",
    )
    .replace(
        "- `get_state`\n",
        "- `get_state`\n- `get_energy_context`\n",
    )
    .replace(
        "2. `get_state` — read state.\n",
        "2. `get_state` — read state.\n3. `get_energy_context` — read energy.\n",
    )
    .replace("3. `optimize_scenario`", "4. `optimize_scenario`")
    .replace("4. `validate_plan`", "5. `validate_plan`")
    .replace("5. `explain_solution`", "6. `explain_solution`")
    .replace("6. `operator_approval`", "7. `operator_approval`")
    .replace("7. `execute_plan`", "8. `execute_plan`")
    .replace(
        "- `get_state` → `mcp.get_state` (`read`)\n",
        "- `get_state` → `mcp.get_state` (`read`)\n"
        "- `get_energy_context` → `mcp.get_energy_context` (`read`)\n",
    )
    .replace(
        "- `execute_plan`\n\n## Procedure",
        "- `commit_or_schedule_bundle`\n\n## Procedure",
    )
    .replace(
        "8. `execute_plan` — execute.",
        "8. `commit_or_schedule_bundle` — commit or schedule the bundle.",
    )
    .replace(
        "- `execute_plan` → `mcp.execute_plan` (`mutation`)",
        "- `commit_or_schedule_bundle` → `mcp.commit_or_schedule_bundle` (`mutation`)",
    )
)


def test_validator_rejects_undeclared_operation(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL.replace("execute_plan", "delete_everything"), encoding="utf-8")

    with pytest.raises(SkillContractError, match="unsupported operation"):
        validate_skill(path)


def test_validator_rejects_missing_approval_boundary(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL, encoding="utf-8")

    with pytest.raises(SkillContractError, match="approval"):
        validate_skill(path)


def test_validator_returns_exact_provider_bindings(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(FULL_SKILL, encoding="utf-8")

    procedure = validate_skill(path)

    assert [
        (item.operation, item.provider, item.tool, item.mode) for item in procedure.bindings
    ] == [
        ("discover_devices", "mcp", "discover_devices", "read"),
        ("get_state", "mcp", "get_state", "read"),
        ("optimize_scenario", "mcp", "optimize_scenario", "proposal"),
        ("validate_plan", "mcp", "validate_plan", "validation"),
        ("explain_solution", "mcp", "explain_solution", "read"),
        ("operator_approval", "operator", "request_approval", "approval"),
        ("execute_plan", "mcp", "execute_plan", "mutation"),
    ]


def test_validator_accepts_one_general_mcp_binding(tmp_path: Path) -> None:
    single_connection = FULL_SKILL.replace("domotics.", "mcp.").replace("ortools.", "mcp.")
    path = tmp_path / "SKILL.md"
    path.write_text(single_connection, encoding="utf-8")

    procedure = validate_skill(path)

    assert {item.provider for item in procedure.bindings} == {"mcp", "operator"}
    assert all(item.provider == "operator" or item.provider == "mcp" for item in procedure.bindings)


def test_validator_accepts_v2_context_binding_and_requires_its_order(tmp_path: Path) -> None:
    v2 = (
        FULL_SKILL.replace(
            "description: test procedure\n---",
            "description: test procedure\ncontract_version: v2\n---",
        )
        .replace("- `get_state`\n", "- `get_state`\n- `get_energy_context`\n")
        .replace(
            "2. `get_state` — read state.\n",
            "2. `get_state` — read state.\n3. `get_energy_context` — read energy.\n",
        )
        .replace(
            "- `get_state` → `mcp.get_state` (`read`)\n",
            "- `get_state` → `mcp.get_state` (`read`)\n"
            "- `get_energy_context` → `mcp.get_energy_context` (`read`)\n",
        )
    )
    path = tmp_path / "SKILL.md"
    path.write_text(v2, encoding="utf-8")

    procedure = validate_skill(path)

    assert procedure.contract_version == "v2"
    assert procedure.operations.index("get_energy_context") < procedure.operations.index(
        "optimize_scenario"
    )


def test_validator_accepts_v3_bundle_commit_boundary(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(V3_SKILL, encoding="utf-8")

    procedure = validate_skill(path)

    assert procedure.contract_version == "v3"
    assert "execute_plan" not in procedure.operations
    assert procedure.operations[-2:] == ("operator_approval", "commit_or_schedule_bundle")
    assert procedure.bindings[-1].tool == "commit_or_schedule_bundle"


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        (
            "- `discover_devices` → `mcp.discover_devices` (`proposal`)",
            "binding",
        ),
        (
            "- `discover_devices` → `hue.turn_on` (`read`)",
            "binding",
        ),
        (
            "- `discover_devices` → `mcp.vendor_cluster` (`read`)",
            "binding",
        ),
    ],
)
def test_validator_rejects_invalid_provider_tool_or_mode(
    tmp_path: Path, binding: str, message: str
) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        FULL_SKILL.replace("- `discover_devices` → `mcp.discover_devices` (`read`)", binding),
        encoding="utf-8",
    )

    with pytest.raises(SkillContractError, match=message):
        validate_skill(path)


def test_validator_rejects_missing_binding(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        FULL_SKILL.replace("- `get_state` → `mcp.get_state` (`read`)\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(SkillContractError, match="binding"):
        validate_skill(path)


def test_validator_rejects_reordered_approval(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    reordered = FULL_SKILL.replace(
        "5. `explain_solution` — explain.\n"
        "6. `operator_approval` — approve.\n"
        "7. `execute_plan` — execute.",
        "5. `explain_solution` — explain.\n"
        "6. `execute_plan` — execute.\n"
        "7. `operator_approval` — approve.",
    )
    path.write_text(reordered, encoding="utf-8")

    with pytest.raises(SkillContractError, match="approval"):
        validate_skill(path)


V4_READ_ONLY_SKILL = """---
name: v4-diagnostics
description: inspect canonical device state without mutation
contract_version: v4
required_context: devices,state
allowed_tools: mcp.discover_devices,mcp.get_state,mcp.explain_solution
allowed_resources: domotics://devices,domotics://runtime
forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call
state_max_age_seconds: 60
approval_required_for: none
failure_mode: stop_and_report
---

## Declared operations

- `discover_devices`
- `get_state`
- `explain_solution`

## Procedure

1. `discover_devices` — read the canonical inventory.
2. `get_state` — read current state.
3. `explain_solution` — report bounded diagnostics.

## Operation bindings

- `discover_devices` → `mcp.discover_devices` (`read`)
- `get_state` → `mcp.get_state` (`read`)
- `explain_solution` → `mcp.explain_solution` (`read`)
"""


def test_validator_normalizes_v4_safety_metadata(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(V4_READ_ONLY_SKILL, encoding="utf-8")

    procedure = validate_skill(path)

    assert procedure.contract_version == "v4"
    assert procedure.required_context == ("devices", "state")
    assert procedure.allowed_tools == (
        "mcp.discover_devices",
        "mcp.get_state",
        "mcp.explain_solution",
    )
    assert procedure.allowed_resources == ("domotics://devices", "domotics://runtime")
    assert procedure.forbidden_tools == (
        "direct_adapter_call",
        "direct_vendor_api",
        "direct_solver_call",
    )
    assert procedure.state_max_age_seconds == 60
    assert procedure.approval_required_for == ("none",)
    assert procedure.failure_mode == "stop_and_report"


@pytest.mark.parametrize(
    "replacement",
    [
        ("required_context: devices,state\n", "required_context: \n"),
        ("state_max_age_seconds: 60\n", "state_max_age_seconds: -1\n"),
        (
            "forbidden_tools: direct_adapter_call,direct_vendor_api,direct_solver_call\n",
            "forbidden_tools: direct_vendor_api\n",
        ),
        (
            "allowed_resources: domotics://devices,domotics://runtime\n",
            "allowed_resources: https://vendor.example/api\n",
        ),
    ],
)
def test_validator_rejects_incomplete_v4_safety_metadata(
    tmp_path: Path, replacement: tuple[str, str]
) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(V4_READ_ONLY_SKILL.replace(*replacement), encoding="utf-8")

    with pytest.raises(SkillContractError):
        validate_skill(path)


def test_validator_rejects_v4_direct_route_even_when_operation_is_semantic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        V4_READ_ONLY_SKILL.replace(
            "`mcp.explain_solution` (`read`)", "`vendor.explain_solution` (`read`)"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SkillContractError, match="invalid operation binding"):
        validate_skill(path)
