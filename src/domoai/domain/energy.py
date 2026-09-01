"""Canonical battery contracts shared by configuration and optimization."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import Field, StrictFloat, model_validator

from domoai.domain.models import SourceRef, StrictModel
from domoai.domain.provider import MeasurementQuality, NominalCapacityAttestation

SOC_OBSERVATION_TOLERANCE_KWH = 1e-6


class BatteryControlPolicy(StrictModel):
    """Deployment declaration for physical control ownership."""

    owner: str = Field(default="domoai", min_length=1, max_length=128)
    native_scheduler_status: Literal["disabled", "inactive", "active", "unknown"] = "unknown"
    allow_native_takeover: bool = False
    lease_seconds: float = Field(default=300.0, gt=0, le=86_400)


class BatteryActuator(StrictModel):
    """Explicit canonical binding for physical battery dispatch."""

    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    charge_command: str = Field(min_length=1)
    discharge_command: str = Field(min_length=1)
    stop_command: str = Field(min_length=1)
    power_feedback_capability: str = Field(min_length=1)
    power_feedback_convention: Literal["charge_positive", "discharge_positive"] = "charge_positive"
    power_feedback_tolerance_kw: float = Field(gt=0)
    power_feedback_settle_timeout_seconds: float = Field(default=0, ge=0, le=120)
    power_feedback_poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    soc_reconciliation_capability: str | None = Field(default=None, min_length=1)
    power_unit: Literal["kW"] = "kW"

    @model_validator(mode="after")
    def validate_distinct_commands(self) -> BatteryActuator:
        commands = {self.charge_command, self.discharge_command, self.stop_command}
        if len(commands) != 3:
            raise ValueError("battery actuator commands must be distinct")
        if (
            self.power_feedback_settle_timeout_seconds > 0
            and self.power_feedback_poll_interval_seconds
            > self.power_feedback_settle_timeout_seconds
        ):
            raise ValueError("power feedback poll interval must not exceed settle timeout")
        return self


class EVActuator(StrictModel):
    """Explicit server-owned binding for a latched EV charge surface."""

    schema_version: Literal["v1"] = "v1"
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    charge_command: str = Field(min_length=1)
    stop_command: str = Field(min_length=1)
    connected_capability: str = Field(default="ev.connected", min_length=1)
    departure_capability: str | None = Field(default="ev.departure_at", min_length=1)
    max_charge_kw: float = Field(gt=0)
    power_unit: Literal["kW"] = "kW"

    @model_validator(mode="after")
    def validate_commands(self) -> EVActuator:
        if self.charge_command == self.stop_command:
            raise ValueError("EV actuator charge and stop commands must be distinct")
        if self.connected_capability == self.capability:
            raise ValueError("EV connected state must not use the writable command capability")
        if self.departure_capability == self.capability:
            raise ValueError("EV departure state must not use the writable command capability")
        return self


class EVChargingBinding(StrictModel):
    """Explicit server-owned binding required to supply EV charging state.

    Deliberately lighter than `DispatchableBatteryBinding`: `EVState.
    capacity_kwh` is an ordinary observed value describing the connected
    vehicle, not a regulatory nominal-capacity claim, so no capacity
    attestation/trust-policy ceremony applies here (spec 162, research.md
    Decision 1).
    """

    schema_version: Literal["v1"] = "v1"
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    device_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    actuator: EVActuator
    control_policy: BatteryControlPolicy = Field(default_factory=BatteryControlPolicy)
    soc_capability: str = Field(default="ev.soc", min_length=1)
    capacity_capability: str = Field(default="ev.capacity", min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> EVChargingBinding:
        if self.actuator.device_id != self.device_id:
            raise ValueError("EV actuator device must match binding device")
        roles = [
            self.soc_capability,
            self.capacity_capability,
            self.actuator.capability,
            self.actuator.connected_capability,
        ]
        if self.actuator.departure_capability is not None:
            roles.append(self.actuator.departure_capability)
        if len(set(roles)) != len(roles):
            raise ValueError("EV binding capability roles must be distinct")
        return self


class NominalCapacityTrustPolicy(StrictModel):
    """Server-owned exact allowlist for measured nominal capacity evidence."""

    schema_version: Literal["v1"] = "v1"
    allowed_evidence_types: list[Literal["vendor_documentation", "installer_attestation"]] = Field(
        min_length=1, max_length=2
    )
    trusted_attesters: list[str] = Field(min_length=1, max_length=128)
    trusted_references: list[str] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_allowlists(self) -> NominalCapacityTrustPolicy:
        if len(set(self.allowed_evidence_types)) != len(self.allowed_evidence_types):
            raise ValueError("allowed_evidence_types must be unique")
        for field_name, values, max_item_length in (
            ("trusted_attesters", self.trusted_attesters, 256),
            ("trusted_references", self.trusted_references, 512),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} must not contain blank entries")
            if any(len(value) > max_item_length for value in values):
                raise ValueError(f"{field_name} contains an entry that is too long")
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
        return self


class BatteryCapacityEvidence(StrictModel):
    """Explicit provider/device-bound capacity used for SOC conversion."""

    schema_version: Literal["v1"] = "v1"
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    device_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    capacity_kwh: StrictFloat = Field(gt=0)
    capacity_source: Literal["provider_config", "provider_measurement"] = "provider_config"
    quality: MeasurementQuality = MeasurementQuality.GOOD
    source_ref: SourceRef | None = None
    observed_at: datetime | None = None
    received_at: datetime | None = None
    nominal_capacity_attestation: NominalCapacityAttestation | None = None

    @model_validator(mode="after")
    def validate_capacity(self) -> BatteryCapacityEvidence:
        if not math.isfinite(self.capacity_kwh):
            raise ValueError("capacity_kwh must be finite")
        if self.source_ref is not None and self.source_ref.adapter_id != self.provider_id:
            raise ValueError("capacity source_ref.adapter_id must match provider_id")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("capacity observed_at must be timezone-aware")
        if self.received_at is not None and self.received_at.tzinfo is None:
            raise ValueError("capacity received_at must be timezone-aware")
        if (
            self.observed_at is not None
            and self.received_at is not None
            and self.received_at < self.observed_at
        ):
            raise ValueError("capacity received_at must be greater than or equal to observed_at")
        if self.capacity_source == "provider_measurement" and (
            self.source_ref is None or self.observed_at is None or self.received_at is None
        ):
            raise ValueError("provider-measurement capacity requires source_ref and timestamps")
        if self.capacity_source == "provider_measurement" and (
            self.nominal_capacity_attestation is None
        ):
            raise ValueError("provider-measurement capacity requires nominal capacity attestation")
        return self


class BatterySocConversionEvidence(StrictModel):
    """Non-secret evidence for converting percentage SOC to canonical energy."""

    schema_version: Literal["v1"] = "v1"
    source_value_percent: StrictFloat = Field(ge=0, le=100)
    capacity: BatteryCapacityEvidence
    method: Literal["percentage_of_declared_capacity"] = "percentage_of_declared_capacity"

    @model_validator(mode="after")
    def validate_percentage(self) -> BatterySocConversionEvidence:
        if not math.isfinite(self.source_value_percent):
            raise ValueError("source_value_percent must be finite")
        return self


class BatterySocObservation(StrictModel):
    """Canonical, provenance-rich SOC evidence used to initialize planning."""

    schema_version: Literal["v1"] = "v1"
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    device_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    metric: Literal["battery.soc"] = "battery.soc"
    value_kwh: float = Field(ge=0)
    unit: Literal["kWh"] = "kWh"
    observed_at: datetime
    received_at: datetime
    quality: MeasurementQuality = MeasurementQuality.GOOD
    source_ref: SourceRef
    conversion_evidence: BatterySocConversionEvidence | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> BatterySocObservation:
        if not math.isfinite(self.value_kwh):
            raise ValueError("value_kwh must be finite")
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("observed_at and received_at must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at must be greater than or equal to observed_at")
        if self.source_ref.adapter_id != self.provider_id:
            raise ValueError("source_ref.adapter_id must match provider_id")
        if self.conversion_evidence is not None:
            capacity = self.conversion_evidence.capacity
            if capacity.provider_id != self.provider_id:
                raise ValueError("conversion capacity provider must match observation provider")
            if capacity.device_id != self.device_id:
                raise ValueError("conversion capacity device must match observation device")
            expected_kwh = (
                self.conversion_evidence.source_value_percent / 100.0 * capacity.capacity_kwh
            )
            if not math.isclose(
                self.value_kwh,
                expected_kwh,
                rel_tol=0.0,
                abs_tol=SOC_OBSERVATION_TOLERANCE_KWH,
            ):
                raise ValueError("conversion evidence must agree with value_kwh")
        return self


class BatteryProfile(StrictModel):
    capacity_kwh: float = Field(gt=0)
    initial_soc_kwh: float = Field(ge=0)
    min_soc_kwh: float = Field(ge=0)
    max_soc_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(ge=0)
    max_discharge_kw: float = Field(ge=0)
    charge_efficiency: float = Field(gt=0, le=1)
    discharge_efficiency: float = Field(gt=0, le=1)
    degradation_cost_per_kwh: float | None = Field(default=None, ge=0)
    actuator: BatteryActuator | None = None
    initial_soc_observation: BatterySocObservation | None = None

    @model_validator(mode="after")
    def validate_state_domain(self) -> BatteryProfile:
        if self.min_soc_kwh > self.initial_soc_kwh:
            raise ValueError("initial_soc_kwh must be greater than or equal to min_soc_kwh")
        if self.initial_soc_kwh > self.max_soc_kwh:
            raise ValueError("initial_soc_kwh must be less than or equal to max_soc_kwh")
        if self.max_soc_kwh > self.capacity_kwh:
            raise ValueError("max_soc_kwh must be less than or equal to capacity_kwh")
        if self.min_soc_kwh > self.max_soc_kwh:
            raise ValueError("min_soc_kwh must be less than or equal to max_soc_kwh")
        if self.initial_soc_observation is not None and not math.isclose(
            self.initial_soc_observation.value_kwh,
            self.initial_soc_kwh,
            rel_tol=0.0,
            abs_tol=SOC_OBSERVATION_TOLERANCE_KWH,
        ):
            raise ValueError("initial_soc_observation.value_kwh must agree with initial_soc_kwh")
        return self


class DispatchableBatteryBinding(StrictModel):
    """Complete semantic binding required before physical battery dispatch."""

    schema_version: Literal["v1"] = "v1"
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    device_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    soc_capability: Literal["battery.soc"] = "battery.soc"
    profile: BatteryProfile
    capacity_evidence: BatteryCapacityEvidence
    capacity_trust_policy: NominalCapacityTrustPolicy | None = None
    control_policy: BatteryControlPolicy = Field(default_factory=BatteryControlPolicy)

    @model_validator(mode="after")
    def validate_binding(self) -> DispatchableBatteryBinding:
        actuator = self.profile.actuator
        if actuator is None:
            raise ValueError("dispatchable battery binding requires an actuator")
        if actuator.device_id != self.device_id:
            raise ValueError("battery actuator device must match binding device")
        if actuator.soc_reconciliation_capability != self.soc_capability:
            raise ValueError("battery actuator SOC reconciliation must match binding capability")
        evidence = self.capacity_evidence
        if evidence.provider_id != self.provider_id:
            raise ValueError("battery capacity provider must match binding provider")
        if evidence.device_id != self.device_id:
            raise ValueError("battery capacity device must match binding device")
        if not math.isclose(
            evidence.capacity_kwh,
            self.profile.capacity_kwh,
            rel_tol=0.0,
            abs_tol=SOC_OBSERVATION_TOLERANCE_KWH,
        ):
            raise ValueError("battery capacity evidence must match profile capacity")
        if evidence.capacity_source == "provider_measurement":
            if evidence.quality is not MeasurementQuality.GOOD:
                raise ValueError("dispatchable battery measured capacity must have GOOD quality")
            if self.capacity_trust_policy is None:
                raise ValueError("dispatchable battery measured capacity requires trust policy")
        return self


class HVACActuator(StrictModel):
    """Explicit canonical binding for physical HVAC dispatch.

    Mirrors ``BatteryActuator``'s shape (power-feedback postcondition
    verification), not ``EVActuator``'s -- HVAC commands are verified
    against reported power the same way battery dispatch already is.
    """

    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    heat_command: str = Field(min_length=1)
    cool_command: str = Field(min_length=1)
    stop_command: str = Field(min_length=1)
    power_feedback_capability: str = Field(min_length=1)
    power_feedback_convention: Literal["heat_positive", "cool_positive"] = "heat_positive"
    power_feedback_tolerance_kw: float = Field(gt=0)
    power_feedback_settle_timeout_seconds: float = Field(default=0, ge=0, le=120)
    power_feedback_poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    power_unit: Literal["kW"] = "kW"

    @model_validator(mode="after")
    def validate_distinct_commands(self) -> HVACActuator:
        commands = {self.heat_command, self.cool_command, self.stop_command}
        if len(commands) != 3:
            raise ValueError("HVAC actuator commands must be distinct")
        if (
            self.power_feedback_settle_timeout_seconds > 0
            and self.power_feedback_poll_interval_seconds
            > self.power_feedback_settle_timeout_seconds
        ):
            raise ValueError("power feedback poll interval must not exceed settle timeout")
        return self


class ThermalProfile(StrictModel):
    """Whole-house thermal model consumed by the CP-SAT optimizer.

    No ``DispatchableBatteryBinding``-style attestation wrapper: thermal
    capacitance is an ordinary scenario-supplied value, not a regulated
    claim (unlike battery capacity).
    """

    capacitance_kwh_per_c: float = Field(gt=0)
    ua_kw_per_c: float = Field(gt=0)
    initial_temperature_c: float
    comfort_min_c: float
    comfort_max_c: float
    max_heat_kw: float = Field(ge=0)
    max_cool_kw: float = Field(ge=0)
    heating_cop: float = Field(gt=0)
    cooling_cop: float = Field(gt=0)
    actuator: HVACActuator | None = None

    @model_validator(mode="after")
    def validate_comfort_bounds(self) -> ThermalProfile:
        if self.comfort_min_c >= self.comfort_max_c:
            raise ValueError("comfort_min_c must be less than comfort_max_c")
        return self


__all__ = [
    "SOC_OBSERVATION_TOLERANCE_KWH",
    "BatteryActuator",
    "EVActuator",
    "EVChargingBinding",
    "HVACActuator",
    "ThermalProfile",
    "BatteryCapacityEvidence",
    "BatteryControlPolicy",
    "BatteryProfile",
    "BatterySocConversionEvidence",
    "BatterySocObservation",
    "DispatchableBatteryBinding",
    "NominalCapacityTrustPolicy",
]
