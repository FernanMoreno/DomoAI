from pathlib import Path

import pytest

from domoai.skills.catalog import CORE_CATALOG_NAMES, load_core_catalog
from domoai.skills.validator import SkillContractError


def test_core_catalog_contains_validated_portable_skills() -> None:
    catalog = load_core_catalog()

    assert tuple(item.name for item in catalog) == CORE_CATALOG_NAMES
    assert len(catalog) == 9
    assert all(item.contract_version == "v4" for item in catalog)
    assert all(item.forbidden_tools for item in catalog)
    assert all(item.state_max_age_seconds == 60 for item in catalog)


def test_core_catalog_rejects_path_name_mismatch(tmp_path: Path) -> None:
    entry = tmp_path / "optimize-home-energy"
    entry.mkdir()
    (entry / "SKILL.md").write_text(
        "---\nname: other\ndescription: wrong\n---\n", encoding="utf-8"
    )

    with pytest.raises(SkillContractError, match="invalid core catalog entry"):
        load_core_catalog(tmp_path)
