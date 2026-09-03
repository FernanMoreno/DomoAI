"""Export versioned JSON Schemas from the canonical Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCapacityBinding,
    HomeAssistantBatteryCommandRoute,
    HomeAssistantDispatchableBatteryBinding,
    HomeAssistantIdentityClaims,
)
from domoai.adapters.sdk import AdapterManifest
from domoai.config.battery_qualification import BatteryHILEvidence
from domoai.domain.commissioning import (
    CommissioningBlocker,
    CommissioningCandidate,
    CommissioningReport,
    CommissioningRoute,
)
from domoai.domain.models import (
    Area,
    AuditEvent,
    BundleCommit,
    BundleMemberCommit,
    Capability,
    Command,
    Device,
    ErrorDetail,
    ExecutionOutcome,
    Plan,
    Policy,
    PolicyDecision,
    SourceRef,
    StateSnapshot,
    ValidationResult,
)
from domoai.domain.provider import (
    DeviceDescriptor,
    Measurement,
    NominalCapacityAttestation,
    ProviderCollectionResult,
    ProviderCommand,
    ProviderDiagnostic,
    ProviderDiscoveryResult,
    ProviderExecutionResult,
    ProviderManifest,
)
from domoai.domain.solar import SolarInstallationProfile
from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulationState
from domoai.mcp.ortools_server import OptimizationExplanation
from domoai.optimizer.energy import (
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EnergyContext,
    EVChargingBinding,
    NominalCapacityTrustPolicy,
    SolarForecastPoint,
    TariffPoint,
)
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.open_meteo import OpenMeteoSolarConfig
from domoai.optimizer.ports import OptimizationResult
from domoai.optimizer.providers import (
    BatteryState,
    EnergyProviderDiagnostic,
    SolarForecastSeries,
    TariffSeries,
)
from domoai.optimizer.scenario import OptimizationScenario

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "schemas" / "v1"

MODELS: dict[str, type[BaseModel]] = {
    "area": Area,
    "audit-event": AuditEvent,
    "bundle-commit": BundleCommit,
    "bundle-member-commit": BundleMemberCommit,
    "capability": Capability,
    "command": Command,
    "device": Device,
    "error-detail": ErrorDetail,
    "execution-outcome": ExecutionOutcome,
    "plan": Plan,
    "policy": Policy,
    "policy-decision": PolicyDecision,
    "source-ref": SourceRef,
    "state-snapshot": StateSnapshot,
    "validation-result": ValidationResult,
    "commissioning-blocker": CommissioningBlocker,
    "commissioning-candidate": CommissioningCandidate,
    "commissioning-report": CommissioningReport,
    "commissioning-route": CommissioningRoute,
    "optimization-scenario": OptimizationScenario,
    "optimization-result": OptimizationResult,
    "optimization-explanation": OptimizationExplanation,
    "horizon": Horizon,
    "tariff-point": TariffPoint,
    "solar-forecast-point": SolarForecastPoint,
    "battery-profile": BatteryProfile,
    "battery-capacity-evidence": BatteryCapacityEvidence,
    "nominal-capacity-trust-policy": NominalCapacityTrustPolicy,
    "battery-soc-observation": BatterySocObservation,
    "battery-soc-conversion-evidence": BatterySocConversionEvidence,
    "dispatchable-battery-binding": DispatchableBatteryBinding,
    "ev-charging-binding": EVChargingBinding,
    "home-assistant-battery-command-route": HomeAssistantBatteryCommandRoute,
    "home-assistant-battery-capacity-binding": HomeAssistantBatteryCapacityBinding,
    "home-assistant-dispatchable-battery-binding": HomeAssistantDispatchableBatteryBinding,
    "home-assistant-identity-claims": HomeAssistantIdentityClaims,
    "energy-context": EnergyContext,
    "tariff-series": TariffSeries,
    "solar-forecast-series": SolarForecastSeries,
    "battery-state": BatteryState,
    "battery-hil-evidence": BatteryHILEvidence,
    "energy-provider-diagnostic": EnergyProviderDiagnostic,
    "open-meteo-solar-config": OpenMeteoSolarConfig,
    "solar-installation-profile": SolarInstallationProfile,
    "adapter-manifest": AdapterManifest,
    "provider-manifest": ProviderManifest,
    "device-descriptor": DeviceDescriptor,
    "measurement": Measurement,
    "nominal-capacity-attestation": NominalCapacityAttestation,
    "provider-command": ProviderCommand,
    "provider-execution-result": ProviderExecutionResult,
    "provider-diagnostic": ProviderDiagnostic,
    "provider-discovery-result": ProviderDiscoveryResult,
    "provider-collection-result": ProviderCollectionResult,
}

# Lab-only dataclasses (not Pydantic models) that still need a published
# schema so consumers of schemas/v1 can validate the simulator's state/
# profile shape. These are never wired into a production qualification or
# dispatch binding schema; see `battery_binding_digest` and
# `BatteryHILEvidence.qualifies` for the actual production boundary.
DATACLASS_MODELS: dict[str, type[object]] = {
    "battery-simulation-profile": BatterySimulationProfile,
    "battery-simulation-state": BatterySimulationState,
}


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        (OUTPUT / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    for name, dataclass_type in DATACLASS_MODELS.items():
        schema = TypeAdapter(dataclass_type).json_schema()
        (OUTPUT / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    total = len(MODELS) + len(DATACLASS_MODELS)
    print(f"Exported {total} schemas to {OUTPUT}")


if __name__ == "__main__":
    main()
