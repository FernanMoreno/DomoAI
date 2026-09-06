from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.domain.coordination import FencingToken, LeaseScope


def test_lease_scope_requires_explicit_authority_dimensions() -> None:
    scope = LeaseScope(tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a")

    assert scope.model_dump() == {
        "tenant_id": "tenant-a",
        "household_id": "home-a",
        "deployment_id": "gw-a",
    }


def test_fencing_token_rejects_naive_expiry() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)

    with pytest.raises(ValidationError):
        FencingToken(
            scope=LeaseScope(tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a"),
            owner_id="host-a",
            epoch=1,
            lease_id="lease-a",
            issued_at=now,
            expires_at=now.replace(tzinfo=None) + timedelta(seconds=30),
        )
