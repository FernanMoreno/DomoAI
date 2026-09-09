"""Runtime-owned contracts for connection onboarding providers."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    ConnectionDraft,
    ProbeResult,
    SetupStep,
    ValidationResult,
)
from domoai.domain.models import StrictModel
from domoai.runtime.ports import AdapterPort

_IDENTIFIER = r"^[a-z0-9][a-z0-9_.-]*$"


class ConnectionProviderManifest(StrictModel):
    """Non-secret identity and setup description for one connector family."""

    contract_version: Literal["v1"] = "v1"
    connector_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    display_name: str = Field(min_length=1, max_length=128)
    protocol: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    supports_discovery: bool = True
    supports_manual_setup: bool = True
    steps: tuple[SetupStep, ...] = ()

    @model_validator(mode="after")
    def validate_steps(self) -> ConnectionProviderManifest:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("provider setup step IDs must be unique")
        return self


class ConnectionSetup(StrictModel):
    """Selected candidate plus secret-safe setup values."""

    candidate: ConnectionCandidate
    draft: ConnectionDraft

    @model_validator(mode="after")
    def validate_provenance(self) -> ConnectionSetup:
        if self.candidate.connector_id != self.draft.connector_id:
            raise ValueError("candidate and draft connector IDs must match")
        if self.candidate.candidate_id != self.draft.unique_id:
            raise ValueError("draft unique_id must match the selected candidate")
        return self


class ConnectionProviderPort(Protocol):
    """Provider operations required by the connection manager."""

    manifest: ConnectionProviderManifest

    async def discover(self) -> list[ConnectionCandidate]: ...

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult: ...

    async def validate(self, setup: ConnectionSetup) -> ValidationResult: ...

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort: ...

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]: ...


__all__ = [
    "ConnectionProviderManifest",
    "ConnectionProviderPort",
    "ConnectionSetup",
]
