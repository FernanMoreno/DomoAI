from dataclasses import dataclass, field

import pytest

from domoai.application.privacy import PrivacyService
from domoai.domain.models import AuthorityContext
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory


def _authority(household: str, *, role: str = "owner") -> AuthorityContext:
    return AuthorityContext(
        tenant_id="tenant-a",
        household_id=household,
        household_ids=[household],
        principal_id=f"{role}-{household}",
        roles=[role],
    )


@dataclass
class MemoryPrivacyStore:
    records: dict[PrivacyCategory, list[dict]]
    deleted: list[tuple[PrivacyCategory, str]] = field(default_factory=list)

    async def export_category(self, category: PrivacyCategory, household_id: str) -> list[dict]:
        return [
            record
            for record in self.records.get(category, [])
            if record.get("household_id") == household_id
        ]

    async def delete_category(self, category: PrivacyCategory, household_id: str) -> int:
        matching = [
            record
            for record in self.records.get(category, [])
            if record.get("household_id") == household_id
        ]
        self.records[category] = [
            record
            for record in self.records.get(category, [])
            if record.get("household_id") != household_id
        ]
        self.deleted.append((category, household_id))
        return len(matching)


def _policy() -> HouseholdDataPolicy:
    return HouseholdDataPolicy(
        authority=_authority("home-a"),
        exportable_categories=[PrivacyCategory.PLANS],
        deletable_categories=[PrivacyCategory.PLANS],
        immutable_categories=[PrivacyCategory.AUDIT],
        retention_days=90,
    )


@pytest.mark.asyncio
async def test_export_is_household_scoped_and_redacts_secret_fields() -> None:
    store = MemoryPrivacyStore(
        {
            PrivacyCategory.PLANS: [
                {
                    "household_id": "home-a",
                    "plan_id": "plan-a",
                    "token": "never-return",
                    "nested": {"password": "never-return"},
                },
                {"household_id": "home-b", "plan_id": "plan-b"},
            ]
        }
    )

    result = await PrivacyService(store).export(
        _policy(), _authority("home-a"), categories=[PrivacyCategory.PLANS]
    )

    assert result.record_count == 1
    assert result.records == [{"household_id": "home-a", "plan_id": "plan-a", "nested": {}}]
    assert "never-return" not in str(result.model_dump())


@pytest.mark.asyncio
async def test_export_rejects_cross_household_request() -> None:
    with pytest.raises(PermissionError, match="household"):
        await PrivacyService(MemoryPrivacyStore({})).export(
            _policy(), _authority("home-b"), categories=[PrivacyCategory.PLANS]
        )


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_preserves_immutable_audit_category() -> None:
    store = MemoryPrivacyStore(
        {PrivacyCategory.PLANS: [{"household_id": "home-a", "plan_id": "plan-a"}]}
    )
    service = PrivacyService(store)

    first = await service.delete(
        _policy(), _authority("home-a"), categories=[PrivacyCategory.PLANS], request_id="req-1"
    )
    second = await service.delete(
        _policy(), _authority("home-a"), categories=[PrivacyCategory.PLANS], request_id="req-1"
    )

    assert first.deleted_counts == {"plans": 1}
    assert second.deleted_counts == {"plans": 0}
    assert store.deleted == [
        (PrivacyCategory.PLANS, "home-a"),
        (PrivacyCategory.PLANS, "home-a"),
    ]

    with pytest.raises(PermissionError, match="immutable"):
        await service.delete(
            _policy(), _authority("home-a"), categories=[PrivacyCategory.AUDIT], request_id="req-2"
        )
