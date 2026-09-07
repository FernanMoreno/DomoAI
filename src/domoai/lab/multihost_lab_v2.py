"""Secret-safe reports and allowlisted scenario names for the Docker lab v2."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel

LAB_SCENARIOS = frozenset(
    {
        "ownership-race",
        "partition-takeover",
        "crash-replay",
        "control-plane-loss",
        "database-primary-failover",
        "secure-rotation",
        "backup-restore",
        "bounded-load",
    }
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SECRET_WORDS = (
    "password",
    "secret",
    "credential",
    "private_key",
    "api_key",
    "access_token",
    "bearer",
    "connection_string",
    "dsn",
    "token",
)


def _assert_secret_safe(value: object, *, path: str = "report") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(word in normalized for word in _SECRET_WORDS):
                raise ValueError(f"{path} must be secret-safe")
            _assert_secret_safe(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_secret_safe(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if "postgresql://" in lowered or "bearer " in lowered:
            raise ValueError(f"{path} must be secret-safe")


class LabScenarioResult(StrictModel):
    """One bounded, non-secret result from a disposable scenario."""

    schema_version: Literal["v1"] = "v1"
    run_id: str = Field(min_length=1, max_length=128)
    qualification_environment: Literal["lab"] = "lab"
    scenario_id: str = Field(min_length=1, max_length=128)
    status: Literal["passed", "failed", "blocked", "incomplete"]
    instance_ids: list[str] = Field(default_factory=list, max_length=16)
    fault: str | None = Field(default=None, max_length=128)
    observations: dict[str, bool | int | float | str] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list, max_length=32)
    duration_ms: int = Field(ge=0, le=86_400_000)

    @model_validator(mode="after")
    def validate_report_fields(self) -> LabScenarioResult:
        if not _SAFE_ID.fullmatch(self.run_id) or not _SAFE_ID.fullmatch(self.scenario_id):
            raise ValueError("lab report identifiers are invalid")
        if self.scenario_id not in LAB_SCENARIOS:
            raise ValueError("lab report scenario is not allowlisted")
        if any(not _SAFE_ID.fullmatch(value) for value in self.instance_ids):
            raise ValueError("lab report instance identity is invalid")
        _assert_secret_safe(self.model_dump(mode="json"))
        return self


class LabRecoveryReport(StrictModel):
    """Final report for one v2 lab run; never eligible for production."""

    schema_version: Literal["v1"] = "v1"
    run_id: str = Field(min_length=1, max_length=128)
    qualification_environment: Literal["lab"] = "lab"
    started_at: datetime
    completed_at: datetime
    scenarios: list[LabScenarioResult] = Field(min_length=1, max_length=32)
    cleanup: dict[str, bool | int | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_report(self) -> LabRecoveryReport:
        if self.completed_at < self.started_at:
            raise ValueError("lab report timestamps are out of order")
        if any(scenario.run_id != self.run_id for scenario in self.scenarios):
            raise ValueError("lab scenario run_id does not match report")
        _assert_secret_safe(self.model_dump(mode="json"))
        return self


__all__ = ["LAB_SCENARIOS", "LabRecoveryReport", "LabScenarioResult"]
