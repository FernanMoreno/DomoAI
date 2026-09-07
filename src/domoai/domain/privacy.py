"""Household-scoped privacy contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import AuthorityContext, StrictModel


class PrivacyCategory(StrEnum):
    INVENTORY = "inventory"
    STATE = "state"
    PLANS = "plans"
    APPROVALS = "approvals"
    BUNDLES = "bundles"
    SCHEDULES = "schedules"
    AUTOMATIONS = "automations"
    AUDIT = "audit"


class HouseholdDataPolicy(StrictModel):
    schema_version: Literal["v1"] = "v1"
    authority: AuthorityContext
    exportable_categories: list[PrivacyCategory] = Field(min_length=1, max_length=16)
    deletable_categories: list[PrivacyCategory] = Field(default_factory=list, max_length=16)
    immutable_categories: list[PrivacyCategory] = Field(default_factory=list, max_length=16)
    retention_days: int = Field(ge=1, le=3650)

    @model_validator(mode="after")
    def validate_categories(self) -> HouseholdDataPolicy:
        for field_name in (
            "exportable_categories",
            "deletable_categories",
            "immutable_categories",
        ):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must contain unique categories")
        overlap = set(self.deletable_categories) & set(self.immutable_categories)
        if overlap:
            raise ValueError("deletable categories cannot be immutable")
        return self


class PrivacyExport(StrictModel):
    schema_version: Literal["v1"] = "v1"
    household_id: str = Field(min_length=1, max_length=200)
    categories: list[PrivacyCategory] = Field(min_length=1, max_length=16)
    records: list[dict[str, object]] = Field(default_factory=list, max_length=1024)
    record_count: int = Field(default=0, ge=0)
    total_record_count: int | None = Field(default=None, ge=0)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_count(self) -> PrivacyExport:
        if self.record_count == 0 and self.records:
            self.record_count = len(self.records)
        elif self.record_count != len(self.records):
            raise ValueError("privacy export record_count must match records")
        if self.total_record_count is None:
            self.total_record_count = len(self.records)
        elif self.total_record_count < self.record_count:
            raise ValueError("privacy export total_record_count must cover this page")
        return self


class PrivacyDeletion(StrictModel):
    schema_version: Literal["v1"] = "v1"
    request_id: str = Field(min_length=1, max_length=200)
    household_id: str = Field(min_length=1, max_length=200)
    categories: list[PrivacyCategory] = Field(min_length=1, max_length=16)
    deleted_counts: dict[str, int] = Field(default_factory=dict, max_length=16)
    immutable_preserved: list[PrivacyCategory] = Field(default_factory=list, max_length=16)


__all__ = [
    "HouseholdDataPolicy",
    "PrivacyCategory",
    "PrivacyDeletion",
    "PrivacyExport",
]
