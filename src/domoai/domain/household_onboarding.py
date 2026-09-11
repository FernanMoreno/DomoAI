"""Versioned presentation of household readiness; these DTOs grant no authority."""

from typing import Literal

from pydantic import AwareDatetime, Field

from domoai.domain.coverage import CoverageEvidenceSnapshot
from domoai.domain.models import Device, StateSnapshot, StrictModel

type OnboardingRecoveryCode = Literal[
    "connection_recovery_pending", "connection_persistence_pending"
]


type OnboardingAuthMode = Literal["verified_bearer", "static_bearer", "local_loopback"]


class OnboardingCheck(StrictModel):
    code: Literal["service", "lifecycle", "ownership", "connectors"]
    status: Literal["passed", "failed", "pending"]


class OnboardingClientConnection(StrictModel):
    url: str | None = None
    transport: Literal["streamable-http"] = "streamable-http"
    auth_mode: OnboardingAuthMode
    status: Literal["prepared", "pending"] = "pending"


class HouseholdOnboardingStatus(StrictModel):
    schema_version: Literal["domoai.household-onboarding.v1"] = "domoai.household-onboarding.v1"
    auth_mode: OnboardingAuthMode
    can_manage_connections: bool
    checks: list[OnboardingCheck] = Field(max_length=4)
    recovery_diagnostics: list[OnboardingRecoveryCode] = Field(default_factory=list, max_length=2)
    client_connection: OnboardingClientConnection


class HouseholdOnboardingInventory(StrictModel):
    schema_version: Literal["domoai.household-onboarding.v1"] = "domoai.household-onboarding.v1"
    runtime_revision: str
    observed_at: AwareDatetime
    devices: list[Device] = Field(max_length=100)
    next_cursor: str | None = None
    coverage_evidence: CoverageEvidenceSnapshot


class HouseholdOnboardingRead(StrictModel):
    schema_version: Literal["domoai.household-onboarding.v1"] = "domoai.household-onboarding.v1"
    runtime_revision: str
    device_id: str
    states: list[StateSnapshot] = Field(default_factory=list)
    diagnostics: list[
        Literal[
            "state_missing",
            "stale",
            "invalid",
            "unavailable",
            "source_unavailable",
            "connection_mismatch",
            "source_mismatch",
            "capability_unavailable",
        ]
    ] = Field(default_factory=list, max_length=8)
    observed_at: AwareDatetime
    read_status: Literal["verified", "pending", "unavailable"] = "pending"
