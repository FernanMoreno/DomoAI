"""Optimization ports, models and implementations."""

from domoai.domain.energy import (
    BatteryCapacityEvidence,
    BatteryControlPolicy,
    BatteryProfile,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    NominalCapacityTrustPolicy,
)
from domoai.optimizer.energy import (
    EnergyContext,
    EVState,
    SolarForecastPoint,
    StaticEnergyContextProvider,
    TariffPoint,
)
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.omie import OmieTariffHttpClient, OmieTariffProvider
from domoai.optimizer.open_meteo import (
    OpenMeteoHttpClient,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
)
from domoai.optimizer.ports import EnergyContextProvider
from domoai.optimizer.providers import (
    StateStoreBatteryProvider,
    battery_capacity_evidence_from_measurement,
    battery_soc_observation_from_measurement,
    battery_soc_observation_from_percentage_measurement,
    validate_nominal_capacity_trust,
)

__all__ = [
    "BatteryControlPolicy",
    "BatteryCapacityEvidence",
    "BatteryProfile",
    "EVState",
    "BatterySocConversionEvidence",
    "BatterySocObservation",
    "DispatchableBatteryBinding",
    "battery_capacity_evidence_from_measurement",
    "battery_soc_observation_from_measurement",
    "battery_soc_observation_from_percentage_measurement",
    "EnergyContext",
    "EnergyContextProvider",
    "Horizon",
    "OmieTariffHttpClient",
    "OmieTariffProvider",
    "OpenMeteoHttpClient",
    "OpenMeteoSolarConfig",
    "OpenMeteoSolarProvider",
    "SolarForecastPoint",
    "StaticEnergyContextProvider",
    "StateStoreBatteryProvider",
    "TariffPoint",
    "NominalCapacityTrustPolicy",
    "validate_nominal_capacity_trust",
]
