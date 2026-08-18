"""Deterministic validator for portable DomoAI skill procedures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPERATIONS = frozenset(
    {
        "discover_devices",
        "get_state",
        "get_energy_context",
        "optimize_scenario",
        "validate_plan",
        "explain_solution",
        "operator_approval",
        "execute_plan",
    }
)

V1_OPERATION_BINDINGS: dict[str, tuple[str, str, str]] = {
    "discover_devices": ("mcp", "discover_devices", "read"),
    "get_state": ("mcp", "get_state", "read"),
    "optimize_scenario": ("mcp", "optimize_scenario", "proposal"),
    "validate_plan": ("mcp", "validate_plan", "validation"),
    "explain_solution": ("mcp", "explain_solution", "read"),
    "operator_approval": ("operator", "request_approval", "approval"),
    "execute_plan": ("mcp", "execute_plan", "mutation"),
}

V2_OPERATION_BINDINGS: dict[str, tuple[str, str, str]] = {
    **V1_OPERATION_BINDINGS,
    "get_energy_context": ("mcp", "get_energy_context", "read"),
}

_BINDING_PATTERN = re.compile(
    r"^-\s+`(?P<operation>[^`]+)`\s*(?:→|->)\s+"
    r"`(?P<provider>[^`.]+)\.(?P<tool>[^`]+)`\s+"
    r"\(`(?P<mode>[^`]+)`\)$"
)


class SkillContractError(ValueError):
    """Raised when a SKILL.md cannot satisfy the portable procedure contract."""


@dataclass(frozen=True)
class SkillOperationBinding:
    """Immutable semantic routing declaration for one skill operation."""

    operation: str
    provider: str
    tool: str
    mode: str


@dataclass(frozen=True)
class SkillProcedure:
    name: str
    description: str
    operations: tuple[str, ...]
    approval_required: bool
    bindings: tuple[SkillOperationBinding, ...] = ()
    contract_version: str = "v1"


def validate_skill(
    path: Path, *, allowed_operations: frozenset[str] = DEFAULT_OPERATIONS
) -> SkillProcedure:
    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(text)
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    contract_version = metadata.get("contract_version", "v1")
    if not name or not description:
        raise SkillContractError("skill frontmatter requires name and description")

    declared = tuple(_section_operations(body, "Declared operations"))
    operations = tuple(_procedure_operations(body))
    if not operations:
        raise SkillContractError("skill procedure must declare an ordered operation sequence")
    unsupported = sorted(set(operations) - allowed_operations)
    if unsupported:
        raise SkillContractError(f"unsupported operation: {unsupported[0]}")
    if set(declared) != set(operations):
        raise SkillContractError("procedure operations must match declared operations")
    if len(operations) != len(set(operations)):
        raise SkillContractError("procedure operations must not repeat")
    if "execute_plan" not in operations:
        raise SkillContractError("procedure must include execute_plan")
    if "operator_approval" not in operations:
        raise SkillContractError("procedure requires an explicit approval boundary")
    if operations.index("operator_approval") > operations.index("execute_plan"):
        raise SkillContractError("approval must happen before execute_plan")
    if contract_version == "v2":
        if "get_energy_context" not in operations:
            raise SkillContractError("v2 procedure must gather get_energy_context")
        if operations.index("get_energy_context") < operations.index("get_state"):
            raise SkillContractError("get_energy_context must follow get_state")
        if operations.index("get_energy_context") > operations.index("optimize_scenario"):
            raise SkillContractError("get_energy_context must precede optimize_scenario")
    elif contract_version != "v1":
        raise SkillContractError(f"unsupported contract version: {contract_version}")
    bindings = _validate_bindings(
        body,
        operations,
        V2_OPERATION_BINDINGS if contract_version == "v2" else V1_OPERATION_BINDINGS,
    )
    return SkillProcedure(
        name=name,
        description=description,
        operations=operations,
        approval_required=True,
        bindings=bindings,
        contract_version=contract_version,
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillContractError("skill must start with YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker == -1:
        raise SkillContractError("skill frontmatter is not closed")
    metadata: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"\'')
    return metadata, text[marker + len("\n---\n") :]


def _section_operations(body: str, heading: str) -> list[str]:
    section = _section(body, heading)
    return re.findall(r"^-\s+`([^`]+)`", section, flags=re.MULTILINE)


def _procedure_operations(body: str) -> list[str]:
    section = _section(body, "Procedure")
    return re.findall(r"^\d+\.\s+`([^`]+)`", section, flags=re.MULTILINE)


def _validate_bindings(
    body: str,
    operations: tuple[str, ...],
    expected_bindings: dict[str, tuple[str, str, str]],
) -> tuple[SkillOperationBinding, ...]:
    section = _section(body, "Operation bindings")
    bindings: list[SkillOperationBinding] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _BINDING_PATTERN.fullmatch(line)
        if match is None:
            raise SkillContractError("invalid operation binding syntax")
        binding = SkillOperationBinding(**match.groupdict())
        if binding.operation in {item.operation for item in bindings}:
            raise SkillContractError(f"duplicate operation binding: {binding.operation}")
        expected = expected_bindings.get(binding.operation)
        if expected is None:
            raise SkillContractError(f"unknown operation binding: {binding.operation}")
        if (binding.provider, binding.tool, binding.mode) != expected:
            raise SkillContractError(f"invalid operation binding: {binding.operation}")
        bindings.append(binding)

    if {item.operation for item in bindings} != set(operations):
        raise SkillContractError("operation bindings must match declared operations")
    return tuple(bindings)


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        body,
        flags=re.MULTILINE,
    )
    if match is None:
        raise SkillContractError(f"skill is missing section: {heading}")
    return match.group(1)
