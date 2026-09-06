"""Deterministic validator for portable DomoAI skill procedures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources.abc import Traversable
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
        "commit_or_schedule_bundle",
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

V3_OPERATION_BINDINGS: dict[str, tuple[str, str, str]] = {
    **{key: value for key, value in V2_OPERATION_BINDINGS.items() if key != "execute_plan"},
    "commit_or_schedule_bundle": ("mcp", "commit_or_schedule_bundle", "mutation"),
}

V4_OPERATION_BINDINGS = V3_OPERATION_BINDINGS
_V4_REQUIRED_FORBIDDEN_TOOLS = frozenset(
    {"direct_adapter_call", "direct_vendor_api", "direct_solver_call"}
)
_V4_FAILURE_MODES = frozenset({"stop_and_report", "skip_and_audit"})

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
    required_context: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    state_max_age_seconds: int | None = None
    approval_required_for: tuple[str, ...] = ()
    failure_mode: str | None = None


@dataclass(frozen=True)
class SkillSafetyMetadata:
    required_context: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_resources: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    state_max_age_seconds: int
    approval_required_for: tuple[str, ...]
    failure_mode: str


def validate_skill(
    path: Path | Traversable, *, allowed_operations: frozenset[str] = DEFAULT_OPERATIONS
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
    if contract_version == "v3":
        if "commit_or_schedule_bundle" not in operations:
            raise SkillContractError("v3 procedure must include commit_or_schedule_bundle")
    elif contract_version == "v4":
        pass
    elif "execute_plan" not in operations:
        raise SkillContractError("procedure must include execute_plan")
    if contract_version != "v4":
        if "operator_approval" not in operations:
            raise SkillContractError("procedure requires an explicit approval boundary")
        commit_operation = (
            "commit_or_schedule_bundle" if contract_version == "v3" else "execute_plan"
        )
        if operations.index("operator_approval") > operations.index(commit_operation):
            raise SkillContractError(f"approval must happen before {commit_operation}")
    if contract_version in {"v2", "v3"}:
        if "get_energy_context" not in operations:
            raise SkillContractError("v2 procedure must gather get_energy_context")
        if operations.index("get_energy_context") < operations.index("get_state"):
            raise SkillContractError("get_energy_context must follow get_state")
        if operations.index("get_energy_context") > operations.index("optimize_scenario"):
            raise SkillContractError("get_energy_context must precede optimize_scenario")
    elif contract_version not in {"v1", "v4"}:
        raise SkillContractError(f"unsupported contract version: {contract_version}")
    expected_bindings = (
        V4_OPERATION_BINDINGS
        if contract_version == "v4"
        else (
            V3_OPERATION_BINDINGS
            if contract_version == "v3"
            else (V2_OPERATION_BINDINGS if contract_version == "v2" else V1_OPERATION_BINDINGS)
        )
    )
    bindings = _validate_bindings(
        body,
        operations,
        expected_bindings,
    )
    safety_metadata = (
        _validate_v4_metadata(metadata, bindings) if contract_version == "v4" else None
    )
    has_mutation = any(binding.mode == "mutation" for binding in bindings)
    if contract_version == "v4" and has_mutation:
        if "operator_approval" not in operations:
            raise SkillContractError("v4 mutation procedure requires operator_approval")
        if operations.index("operator_approval") > next(
            index
            for index, operation in enumerate(operations)
            if operation in {"execute_plan", "commit_or_schedule_bundle"}
        ):
            raise SkillContractError("approval must happen before physical mutation")
        assert safety_metadata is not None
        if "physical_mutation" not in safety_metadata.approval_required_for:
            raise SkillContractError("v4 mutation procedure requires physical_mutation approval")
    return SkillProcedure(
        name=name,
        description=description,
        operations=operations,
        approval_required=has_mutation if contract_version == "v4" else True,
        bindings=bindings,
        contract_version=contract_version,
        required_context=safety_metadata.required_context if safety_metadata else (),
        allowed_tools=safety_metadata.allowed_tools if safety_metadata else (),
        allowed_resources=safety_metadata.allowed_resources if safety_metadata else (),
        forbidden_tools=safety_metadata.forbidden_tools if safety_metadata else (),
        state_max_age_seconds=(
            safety_metadata.state_max_age_seconds if safety_metadata else None
        ),
        approval_required_for=safety_metadata.approval_required_for if safety_metadata else (),
        failure_mode=safety_metadata.failure_mode if safety_metadata else None,
    )


def _csv_metadata(metadata: dict[str, str], key: str) -> tuple[str, ...]:
    raw = metadata.get(key)
    if raw is None:
        raise SkillContractError(f"v4 metadata requires {key}")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise SkillContractError(f"v4 metadata requires non-empty {key}")
    if len(values) != len(set(values)):
        raise SkillContractError(f"v4 metadata {key} must not repeat values")
    return values


def _validate_v4_metadata(
    metadata: dict[str, str], bindings: tuple[SkillOperationBinding, ...]
) -> SkillSafetyMetadata:
    required_context = _csv_metadata(metadata, "required_context")
    allowed_tools = _csv_metadata(metadata, "allowed_tools")
    allowed_resources = _csv_metadata(metadata, "allowed_resources")
    if any(not resource.startswith("domotics://") for resource in allowed_resources):
        raise SkillContractError("v4 allowed_resources must use semantic domotics:// URIs")
    forbidden_tools = _csv_metadata(metadata, "forbidden_tools")
    approval_required_for = _csv_metadata(metadata, "approval_required_for")
    if not _V4_REQUIRED_FORBIDDEN_TOOLS.issubset(forbidden_tools):
        raise SkillContractError("v4 forbidden_tools must include direct route protections")
    binding_tools = tuple(f"{binding.provider}.{binding.tool}" for binding in bindings)
    if set(allowed_tools) != set(binding_tools):
        raise SkillContractError("v4 allowed_tools must match operation bindings")
    if set(allowed_tools) & set(forbidden_tools):
        raise SkillContractError("v4 allowed_tools and forbidden_tools must be disjoint")
    try:
        state_max_age_seconds = int(metadata.get("state_max_age_seconds", ""))
    except ValueError as error:
        raise SkillContractError("v4 state_max_age_seconds must be a positive integer") from error
    if state_max_age_seconds <= 0:
        raise SkillContractError("v4 state_max_age_seconds must be a positive integer")
    failure_mode = metadata.get("failure_mode", "")
    if failure_mode not in _V4_FAILURE_MODES:
        raise SkillContractError("v4 failure_mode is unsupported")
    if set(approval_required_for) - {"none", "physical_mutation", "operator_approval"}:
        raise SkillContractError("v4 approval_required_for contains an unsupported scope")
    if "none" in approval_required_for and len(approval_required_for) > 1:
        raise SkillContractError("v4 approval_required_for cannot combine none with another scope")
    return SkillSafetyMetadata(
        required_context=required_context,
        allowed_tools=allowed_tools,
        allowed_resources=allowed_resources,
        forbidden_tools=forbidden_tools,
        state_max_age_seconds=state_max_age_seconds,
        approval_required_for=approval_required_for,
        failure_mode=failure_mode,
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
        metadata[key.strip()] = value.strip().strip("\"'")
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
