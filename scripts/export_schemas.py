"""Export versioned JSON Schemas from the canonical Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCommandRoute,
    HomeAssistantDispatchableBatteryBinding,
)
from domoai.adapters.sdk import AdapterManifest
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
from domoai.mcp.ortools_server import OptimizationExplanation
from domoai.optimizer.energy import (
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EnergyContext,
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
    "home-assistant-battery-command-route": HomeAssistantBatteryCommandRoute,
    "home-assistant-dispatchable-battery-binding": HomeAssistantDispatchableBatteryBinding,
    "energy-context": EnergyContext,
    "tariff-series": TariffSeries,
    "solar-forecast-series": SolarForecastSeries,
    "battery-state": BatteryState,
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


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        (OUTPUT / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"Exported {len(MODELS)} schemas to {OUTPUT}")


if __name__ == "__main__":
    main()
