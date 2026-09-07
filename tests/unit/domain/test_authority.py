import pytest
from pydantic import ValidationError

from domoai.domain.models import AuthorityContext, PrincipalRole


def test_authority_context_defaults_preserve_single_home_legacy_rows() -> None:
    authority = AuthorityContext()

    assert authority.tenant_id == "default"
    assert authority.household_id == "default"
    assert authority.household_ids == ["default"]
    assert authority.principal_id == "system"
    assert authority.roles == [PrincipalRole.SERVICE]


def test_authority_context_rejects_household_not_in_granted_set() -> None:
    with pytest.raises(ValidationError, match="household_id"):
        AuthorityContext(household_id="home-a", household_ids=["home-b"])


def test_authority_context_rejects_duplicate_scope_entries() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AuthorityContext(device_ids=["light.one", "light.one"])
