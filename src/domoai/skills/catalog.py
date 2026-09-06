"""Local, deterministic catalog of published core Skills."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from domoai.skills.validator import SkillContractError, SkillProcedure, validate_skill

CORE_CATALOG_NAMES = (
    "optimize-home-energy",
    "optimize-ev-charging",
    "thermal-comfort",
    "solar-self-consumption",
    "battery-arbitrage",
    "night-mode",
    "vacation-mode",
    "device-diagnostics",
    "commission-new-device",
)


def load_core_catalog(root: Path | None = None) -> tuple[SkillProcedure, ...]:
    """Validate and return the fixed core catalog in publication order."""

    catalog_root: Path | resources.abc.Traversable
    if root is not None:
        catalog_root = root
    else:
        packaged_root = resources.files("domoai.skills").joinpath("core")
        catalog_root = (
            packaged_root
            if packaged_root.joinpath(CORE_CATALOG_NAMES[0], "SKILL.md").is_file()
            else Path(__file__).resolve().parents[3] / "skills" / "core"
        )
    procedures: list[SkillProcedure] = []
    seen: set[str] = set()
    for name in CORE_CATALOG_NAMES:
        path = catalog_root / name / "SKILL.md"
        try:
            procedure = validate_skill(path)
        except (OSError, SkillContractError) as error:
            raise SkillContractError(f"invalid core catalog entry {name}: {error}") from error
        if procedure.name in seen:
            raise SkillContractError(f"duplicate core catalog Skill: {procedure.name}")
        if procedure.name != name:
            raise SkillContractError(
                f"core catalog path {name} declares unexpected name {procedure.name}"
            )
        seen.add(procedure.name)
        procedures.append(procedure)
    return tuple(procedures)
