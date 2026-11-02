"""Additive v1 preference contracts. Preferences never grant execution authority."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type PreferenceKey = Literal[
    "schedule.preferred_run_window",
    "comfort.temperature_target",
    "energy.minimum_soc",
    "energy.device_use_allowed",
]
type Operation = Literal["set", "delete", "restore", "erase"]
type Identifier = Annotated[str, Field(min_length=1, max_length=200)]
type Timestamp = Annotated[datetime, Field(strict=False)]


class PreferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class PreferenceVersion(PreferenceModel):
    generation: UUID
    revision: int = Field(ge=0)

    @field_validator("generation", mode="before")
    @classmethod
    def uuid_value(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value


class PreferenceTarget(PreferenceModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "alias": {"type": "null"},
                    },
                },
                {
                    "required": ["alias"],
                    "properties": {
                        "alias": {"type": "string"},
                        "id": {"type": "null"},
                    },
                },
            ],
        }
    )
    kind: Literal["device", "area"]
    id: Identifier | None = None
    alias: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @model_validator(mode="after")
    def exactly_one_identity(self) -> Self:
        if (self.id is None) == (self.alias is None):
            raise ValueError("target requires exactly one id or alias")
        return self


class PreferenceProvenanceInput(PreferenceModel):
    source_kind: Literal["user_request", "manual", "import"]
    source_ref: (
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")] | None
    ) = None


class DevicePreferenceTarget(PreferenceTarget):
    kind: Literal["device"]


class PreferenceProvenance(PreferenceProvenanceInput):
    updated_by: Identifier
    recorded_at: Timestamp


class PreferenceValidity(PreferenceModel):
    valid_from: Timestamp
    valid_until: Timestamp | None
    timezone: Annotated[str, Field(min_length=1, max_length=100)]

    @field_validator("valid_from", "valid_until", mode="before")
    @classmethod
    def explicit_datetime(cls, value: object) -> datetime | None:
        if value is None:
            return value
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("an explicit datetime with offset is required")
        return value

    @model_validator(mode="after")
    def validate_instants(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be an IANA zone") from error
        for value in (self.valid_from, self.valid_until):
            if value is None:
                continue
            # UTC is the canonical persisted representation. Non-UTC input
            # must describe an actual occurrence in the declared local zone.
            offset = value.utcoffset()
            if offset is None:
                raise ValueError("an explicit datetime with offset is required")
            if offset.total_seconds() != 0:
                local = value.astimezone(zone)
                if local.utcoffset() != value.utcoffset() or local.replace(
                    tzinfo=None
                ) != value.replace(tzinfo=None):
                    raise ValueError("datetime does not match timezone/offset")
        if self.valid_until is not None and self.valid_from >= self.valid_until:
            raise ValueError("valid_until must be after valid_from")
        object.__setattr__(self, "valid_from", self.valid_from.astimezone(UTC))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", self.valid_until.astimezone(UTC))
        return self


class PreferenceInput(PreferenceValidity):
    target: PreferenceTarget
    provenance: PreferenceProvenanceInput


class RunWindow(PreferenceModel):
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] = Field(
        min_length=1,
        max_length=7,
        json_schema_extra={"uniqueItems": True},
    )
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    overnight: bool

    @model_validator(mode="after")
    def coherent_window(self) -> Self:
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be unique")
        if self.start == self.end or self.overnight != (self.start > self.end):
            raise ValueError("overnight must describe the explicit window")
        return self


class SchedulePreference(PreferenceInput):
    target: DevicePreferenceTarget
    key: Literal["schedule.preferred_run_window"]
    value: RunWindow
    unit: Literal["local_time"]


class TemperaturePreference(PreferenceInput):
    key: Literal["comfort.temperature_target"]
    value: Annotated[int | float, Field(ge=5, le=35)]
    unit: Literal["degC"]


class MinimumSocPreference(PreferenceInput):
    target: DevicePreferenceTarget
    key: Literal["energy.minimum_soc"]
    value: int = Field(ge=0, le=100)
    unit: Literal["percent"]


class DeviceUsePreference(PreferenceInput):
    target: DevicePreferenceTarget
    key: Literal["energy.device_use_allowed"]
    value: bool
    unit: Literal["dimensionless"]


type Preference = Annotated[
    SchedulePreference | TemperaturePreference | MinimumSocPreference | DeviceUsePreference,
    Field(discriminator="key"),
]


class PreferenceMutation(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    expected_version: PreferenceVersion
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    preference: Preference


class PreferenceSlotMutation(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    expected_version: PreferenceVersion
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    preference_id: UUID

    @field_validator("preference_id", mode="before")
    @classmethod
    def uuid_value(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value


class PreferenceRestore(PreferenceSlotMutation, PreferenceValidity):
    source_revision: int = Field(ge=1)
    provenance: PreferenceProvenanceInput


class HouseholdPreference(PreferenceModel):
    preference_id: UUID
    key: PreferenceKey
    target: PreferenceTarget
    preference: Preference | None
    provenance: PreferenceProvenance | None
    version: PreferenceVersion
    updated_at: Timestamp
    deleted: bool = False
    erased: bool = False


class PreferenceRead(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    key: PreferenceKey | None = None
    target: PreferenceTarget | None = None
    effective_only: bool = True
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)


class PreferenceHistoryRead(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    preference_id: str | None = Field(default=None, max_length=36)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)


class PreferenceView(PreferenceModel):
    slot: HouseholdPreference
    state: Literal["scheduled", "effective", "expired", "deleted"]


class PreferenceProfile(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    version: PreferenceVersion
    observed_at: Timestamp
    next_transition_at: Timestamp | None
    preferences: list[PreferenceView]
    next_cursor: str | None = None


class PreferenceHistoryEntry(PreferenceModel):
    version: PreferenceVersion
    operation: Operation
    preference_id: UUID
    snapshot: HouseholdPreference | None
    actor: Identifier
    recorded_at: Timestamp
    source_revision: int | None = None


class PreferenceHistory(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    version: PreferenceVersion
    entries: list[PreferenceHistoryEntry]
    next_cursor: str | None = None


class PreferenceResult(PreferenceModel):
    schema_version: Literal["v1"] = "v1"
    version: PreferenceVersion
    operation: Operation
    preference: HouseholdPreference | None
    task_reevaluation: Literal["consumer_unavailable", "pending"] = "consumer_unavailable"
    backups_may_retain_data: bool = False
