"""Export versioned JSON Schemas from the canonical Pydantic models."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from pydantic.json_schema import SkipJsonSchema

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCapacityBinding,
    HomeAssistantBatteryCommandRoute,
    HomeAssistantDispatchableBatteryBinding,
    HomeAssistantIdentityClaims,
)
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.adapters.mqtt.discovery import MqttDiscoveryPreview
from domoai.adapters.sdk import AdapterManifest
from domoai.application.connection_providers import ConnectionProviderManifest, ConnectionSetup
from domoai.application.scene import SceneCommitRequest
from domoai.config.battery_qualification import BatteryHILEvidence, HILIdentityObservation
from domoai.domain.automation import (
    AutomationCondition,
    AutomationConsent,
    AutomationEvaluation,
    AutomationEvent,
    AutomationRule,
    AutomationTrigger,
)
from domoai.domain.commissioning import (
    CommissioningBlocker,
    CommissioningCandidate,
    CommissioningCheck,
    CommissioningEvidence,
    CommissioningQualification,
    CommissioningReport,
    CommissioningRoute,
)
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    ConnectionDraft,
    ConnectionProfile,
    OnboardingSession,
    ProbeResult,
    ProviderCatalogEntry,
    SetupFieldDescriptor,
    SetupStep,
    SetupTransition,
)
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.coordination import FencingToken, LeaseScope, PhysicalIntent
from domoai.domain.coverage import CoverageEvidenceSnapshot
from domoai.domain.digital_twin import DigitalTwinEvidence
from domoai.domain.household_onboarding import (
    HouseholdOnboardingInventory,
    HouseholdOnboardingRead,
    HouseholdOnboardingStatus,
)
from domoai.domain.household_preferences import (
    HouseholdPreference,
    PreferenceHistory,
    PreferenceHistoryRead,
    PreferenceMutation,
    PreferenceProfile,
    PreferenceRead,
    PreferenceRestore,
    PreferenceResult,
    PreferenceSlotMutation,
)
from domoai.domain.models import (
    Area,
    AuditEvent,
    AuthorityContext,
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
    SourceCursor,
    SourceRef,
    StateSnapshot,
    ValidationResult,
)
from domoai.domain.multihost_qualification import (
    GatewayFencingProbeRequest,
    GatewayFencingProbeResult,
    MultiHostQualificationEvidence,
)
from domoai.domain.p1_completion import (
    MatterCommissioningSession,
    ModbusDeviceIdentity,
    ModbusProfileResolution,
)
from domoai.domain.p2_connections import (
    EtsGroupAddress,
    EtsImportPreview,
    EtsImportWarning,
    KnxSecureOptions,
    OAuthAuthorizationRequest,
    OAuthCredentialRecord,
    OAuthMetadataDiscoveryResult,
    OAuthProviderMetadata,
)
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyDeletion, PrivacyExport
from domoai.domain.product import (
    AccountingEvidence,
    ProductAlternative,
    ProductSummary,
    ScenarioComparison,
    ScenarioComparisonVariation,
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
from domoai.domain.repairs import RepairAction, RepairPlan
from domoai.domain.solar import SolarInstallationProfile
from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulationState
from domoai.mcp.ortools_server import OptimizationExplanation
from domoai.optimizer.base_load import DeclaredBaseLoadProfile
from domoai.optimizer.energy import (
    BaseLoadSource,
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
    BaseLoadForecastSeries,
    BatteryState,
    EnergyProviderDiagnostic,
    SolarForecastSeries,
    TariffSeries,
)
from domoai.optimizer.scenario import OptimizationScenario
from domoai.optimizer.solar_accounting import SolarAccountingEvidence
from domoai.runtime.connection_provider_loader import ConnectionProviderLoadDiagnostic

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "schemas" / "v1"

# The runtime models read both versions. These typed projections publish each
# wire contract independently while reusing its canonical fields/validators.
# Retain original model names so legacy $defs, titles and payloads stay stable.
EnergyContextV1 = create_model(
    "EnergyContext",
    __base__=EnergyContext,
    __doc__=EnergyContext.__doc__,
    schema_version=(Literal["v1"], "v1"),
    base_load_source=(SkipJsonSchema[None], None),
)
EnergyContextV2 = create_model(
    "EnergyContext",
    __base__=EnergyContext,
    __doc__=EnergyContext.__doc__,
    schema_version=(Literal["v2"], "v2"),
    base_load_source=(BaseLoadSource, ...),
)
OptimizationScenarioV1 = create_model(
    "OptimizationScenario",
    __base__=OptimizationScenario,
    __doc__=OptimizationScenario.__doc__,
    energy_context=(EnergyContextV1 | None, None),
)
OptimizationScenarioV2 = create_model(
    "OptimizationScenario",
    __base__=OptimizationScenario,
    __doc__=OptimizationScenario.__doc__,
    schema_version=(Literal["v2"], "v2"),
    energy_context=(EnergyContextV2, ...),
)

# Historical projections deliberately keep the original str version fields,
# names and docstrings. They describe archival v1, not active constructors.
PlanV1 = create_model(
    "Plan",
    __base__=Plan,
    __doc__=Plan.__doc__,
    deployment_id=(SkipJsonSchema[None], None),
    execution_lease=(SkipJsonSchema[None], None),
)
_PLAN_V2_SHAPE = {
    "anyOf": [
        {"required": [name], "properties": {name: {"not": {"type": "null"}}}}
        for name in ("deployment_id", "execution_lease")
    ]
}
PlanV2 = create_model(
    "Plan",
    __base__=Plan,
    __doc__=Plan.__doc__,
    __config__=ConfigDict(json_schema_extra=_PLAN_V2_SHAPE),
    schema_version=(Literal["v2"], "v2"),
)
AutomationRuleV1 = create_model(
    "AutomationRule",
    __base__=AutomationRule,
    __doc__=AutomationRule.__doc__,
    plan_template=(PlanV1, ...),
)
AutomationRuleV2 = create_model(
    "AutomationRule",
    __base__=AutomationRule,
    __doc__=AutomationRule.__doc__,
    schema_version=(Literal["v2"], "v2"),
    plan_template=(PlanV2, ...),
)
OptimizationResultV1 = create_model(
    "OptimizationResult",
    __base__=OptimizationResult,
    __doc__=OptimizationResult.__doc__,
    plan=(PlanV1 | None, None),
    plans=(list[PlanV1], Field(default_factory=list)),
)
_RESULT_V2_SHAPE = {
    "anyOf": [
        {
            "required": ["plan"],
            "properties": {
                "plan": {
                    "type": "object",
                    "required": ["schema_version"],
                    "properties": {"schema_version": {"const": "v2"}},
                }
            },
        },
        {
            "required": ["plans"],
            "properties": {
                "plans": {
                    "contains": {
                        "type": "object",
                        "required": ["schema_version"],
                        "properties": {"schema_version": {"const": "v2"}},
                    }
                }
            },
        },
    ]
}
# In v2 envelopes the legacy branch must be labelled v1; archival v1
# artifacts retain their exact original (unrestricted string) schema.
NestedPlanV1 = create_model(
    "PlanV1",
    __base__=PlanV1,
    __doc__=Plan.__doc__,
    schema_version=(Literal["v1"], "v1"),
)
OptimizationResultV2 = create_model(
    "OptimizationResult",
    __base__=OptimizationResult,
    __doc__=OptimizationResult.__doc__,
    __config__=ConfigDict(json_schema_extra=_RESULT_V2_SHAPE),
    schema_version=(Literal["v2"], "v2"),
    plan=(NestedPlanV1 | PlanV2 | None, None),
    plans=(list[NestedPlanV1 | PlanV2], Field(default_factory=list)),
)
AutomationEventV1 = create_model(
    "AutomationEvent",
    __base__=AutomationEvent,
    __doc__=AutomationEvent.__doc__,
    target_rule_id=(SkipJsonSchema[None], None),
)
AutomationEventV2 = create_model(
    "AutomationEvent",
    __base__=AutomationEvent,
    __doc__=AutomationEvent.__doc__,
    __config__=ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"event_type": {"const": "time"}}},
                    "then": {
                        "required": ["target_rule_id"],
                        "properties": {"target_rule_id": {"type": "string"}},
                    },
                    "else": {"properties": {"target_rule_id": {"type": "null"}}},
                }
            ]
        }
    ),
)
PhysicalIntentStatusV1 = StrEnum(
    "PhysicalIntentStatus",
    {
        name: name.lower()
        for name in ("PREPARED", "ACKNOWLEDGED", "CONFIRMED", "REJECTED", "UNKNOWN")
    },
)
PhysicalIntentV1 = create_model(
    "PhysicalIntent",
    __base__=PhysicalIntent,
    __doc__=PhysicalIntent.__doc__,
    status=(PhysicalIntentStatusV1, PhysicalIntentStatusV1.PREPARED),
    owner_id=(SkipJsonSchema[None], None),
    execution_attempt_id=(SkipJsonSchema[None], None),
    lease_id=(SkipJsonSchema[None], None),
)
PhysicalIntentV2 = create_model(
    "PhysicalIntent",
    __base__=PhysicalIntent,
    __doc__=PhysicalIntent.__doc__,
    __config__=ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "required": ["status"],
                        "properties": {"status": {"const": "dispatching"}},
                    },
                    "then": {
                        "required": ["owner_id", "execution_attempt_id"],
                        "properties": {
                            "owner_id": {"type": "string"},
                            "execution_attempt_id": {"type": "string"},
                        },
                    },
                }
            ]
        }
    ),
)
V2_MODELS: dict[str, type[BaseModel]] = {
    "energy-context": EnergyContextV2,
    "optimization-scenario": OptimizationScenarioV2,
    "plan": PlanV2,
    "automation-rule": AutomationRuleV2,
    "optimization-result": OptimizationResultV2,
    "automation-event": AutomationEventV2,
    "physical-intent": PhysicalIntentV2,
}

MODELS: dict[str, type[BaseModel]] = {
    "household-preference": HouseholdPreference,
    "household-preference-mutation": PreferenceMutation,
    "household-preference-slot-mutation": PreferenceSlotMutation,
    "household-preference-restore": PreferenceRestore,
    "household-preference-read": PreferenceRead,
    "household-preference-profile": PreferenceProfile,
    "household-preference-history-read": PreferenceHistoryRead,
    "household-preference-history": PreferenceHistory,
    "household-preference-result": PreferenceResult,
    "household-onboarding-status": HouseholdOnboardingStatus,
    "household-onboarding-inventory": HouseholdOnboardingInventory,
    "household-onboarding-read": HouseholdOnboardingRead,
    "optimization-accounting": AccountingEvidence,
    "area": Area,
    "audit-event": AuditEvent,
    "authority-context": AuthorityContext,
    "fencing-token": FencingToken,
    "bundle-commit": BundleCommit,
    "bundle-member-commit": BundleMemberCommit,
    "capability": Capability,
    "command": Command,
    "device": Device,
    "error-detail": ErrorDetail,
    "execution-outcome": ExecutionOutcome,
    "plan": PlanV1,
    "physical-intent": PhysicalIntentV1,
    "policy": Policy,
    "policy-decision": PolicyDecision,
    "source-cursor": SourceCursor,
    "source-ref": SourceRef,
    "state-snapshot": StateSnapshot,
    "lease-scope": LeaseScope,
    "validation-result": ValidationResult,
    "commissioning-blocker": CommissioningBlocker,
    "commissioning-check": CommissioningCheck,
    "commissioning-candidate": CommissioningCandidate,
    "commissioning-evidence": CommissioningEvidence,
    "commissioning-report": CommissioningReport,
    "commissioning-qualification": CommissioningQualification,
    "commissioning-route": CommissioningRoute,
    "digital-twin-evidence": DigitalTwinEvidence,
    "coverage-evidence": CoverageEvidenceSnapshot,
    "household-data-policy": HouseholdDataPolicy,
    "privacy-deletion": PrivacyDeletion,
    "privacy-export": PrivacyExport,
    "product-alternative": ProductAlternative,
    "product-summary": ProductSummary,
    "scenario-comparison": ScenarioComparison,
    "scenario-comparison-variation": ScenarioComparisonVariation,
    "scene-commit": SceneCommitRequest,
    "automation-condition": AutomationCondition,
    "automation-consent": AutomationConsent,
    "automation-event": AutomationEventV1,
    "automation-evaluation": AutomationEvaluation,
    "automation-rule": AutomationRuleV1,
    "automation-trigger": AutomationTrigger,
    "optimization-scenario": OptimizationScenarioV1,
    "optimization-result": OptimizationResultV1,
    "optimization-explanation": OptimizationExplanation,
    "horizon": Horizon,
    "tariff-point": TariffPoint,
    "solar-forecast-point": SolarForecastPoint,
    "solar-accounting-evidence": SolarAccountingEvidence,
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
    "energy-context": EnergyContextV1,
    "base-load-forecast-series": BaseLoadForecastSeries,
    "declared-base-load-profile": DeclaredBaseLoadProfile,
    "tariff-series": TariffSeries,
    "solar-forecast-series": SolarForecastSeries,
    "battery-state": BatteryState,
    "battery-hil-evidence": BatteryHILEvidence,
    "hil-identity-observation": HILIdentityObservation,
    "gateway-fencing-probe-request": GatewayFencingProbeRequest,
    "gateway-fencing-probe-result": GatewayFencingProbeResult,
    "multihost-qualification-evidence": MultiHostQualificationEvidence,
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
    "connection-profile": ConnectionProfile,
    "connection-candidate": ConnectionCandidate,
    "connection-draft": ConnectionDraft,
    "connection-diagnostic": ConnectionDiagnostic,
    "probe-result": ProbeResult,
    "onboarding-session": OnboardingSession,
    "setup-step": SetupStep,
    "setup-field-descriptor": SetupFieldDescriptor,
    "setup-transition": SetupTransition,
    "connection-provider-manifest": ConnectionProviderManifest,
    "connection-setup": ConnectionSetup,
    "provider-catalog-entry": ProviderCatalogEntry,
    "connector-recipe": ConnectorRecipe,
    "matter-commissioning-session": MatterCommissioningSession,
    "modbus-device-identity": ModbusDeviceIdentity,
    "modbus-profile-resolution": ModbusProfileResolution,
    "modbus-profile": ModbusProfile,
    "mqtt-discovery-preview": MqttDiscoveryPreview,
    "connection-provider-load-diagnostic": ConnectionProviderLoadDiagnostic,
    "ets-group-address": EtsGroupAddress,
    "ets-import-preview": EtsImportPreview,
    "ets-import-warning": EtsImportWarning,
    "knx-secure-options": KnxSecureOptions,
    "oauth-authorization-request": OAuthAuthorizationRequest,
    "oauth-credential-record": OAuthCredentialRecord,
    "oauth-provider-metadata": OAuthProviderMetadata,
    "oauth-metadata-discovery-result": OAuthMetadataDiscoveryResult,
    "repair-action": RepairAction,
    "repair-plan": RepairPlan,
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
    version_two_output = ROOT / "schemas" / "v2"
    version_two_output.mkdir(parents=True, exist_ok=True)
    for name, model in V2_MODELS.items():
        (version_two_output / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(by_alias=True), indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
    total = len(MODELS) + len(DATACLASS_MODELS)
    print(f"Exported {total} schemas to {OUTPUT}")
    print(f"Exported {len(V2_MODELS)} schemas to {version_two_output}")


if __name__ == "__main__":
    main()
