"""Explicit cross-layer composition for dispatchable battery bindings."""

from __future__ import annotations

import math
from collections.abc import Iterable

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCommandRoute,
    HomeAssistantMappingConfigurationError,
)
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.domain.models import AdapterSnapshot, Capability
from domoai.optimizer.energy import (
    BatteryCapacityEvidence,
    BatteryProfile,
    DispatchableBatteryBinding,
    NominalCapacityTrustPolicy,
)


def compose_home_assistant_dispatchable_battery_binding(
    provider: HomeAssistantProvider,
    snapshot: AdapterSnapshot,
    *,
    binding_id: str,
    canonical_device_id: str,
    profile: BatteryProfile,
    capacity_evidence: BatteryCapacityEvidence,
    capacity_trust_policy: NominalCapacityTrustPolicy | None = None,
) -> DispatchableBatteryBinding:
    """Compose a validated HA route document into the canonical binding.

    This function only joins already server-supplied semantic inputs. It does
    not refresh Home Assistant, call a service, persist state or install a
    ``StateStoreBatteryProvider``. The caller owns the later provider
    installation decision. ``canonical_device_id`` must be the exact
    `DeviceRegistry` ID resolved for the configured source routes; the HA
    binding's `device_id` remains source-device identity only.
    """

    if not canonical_device_id.strip():
        raise HomeAssistantMappingConfigurationError(
            "canonical battery device id must not be empty"
        )
    provider.validate_battery_dispatch_routes(snapshot)
    binding = provider.battery_dispatch_bindings.get(binding_id)
    if binding is None:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery dispatch binding is unknown"
        )

    actuator = profile.actuator
    if actuator is None:
        raise HomeAssistantMappingConfigurationError(
            "dispatchable battery composition requires an actuator"
        )
    if actuator.device_id != canonical_device_id:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery actuator device does not match canonical device"
        )
    if actuator.charge_command != binding.charge.provider_command:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery charge command does not match actuator"
        )
    if actuator.discharge_command != binding.discharge.provider_command:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery discharge command does not match actuator"
        )
    if actuator.stop_command != binding.stop.provider_command:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery stop command does not match actuator"
        )
    if actuator.power_feedback_capability != binding.power_feedback_capability:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery feedback capability does not match actuator"
        )
    if actuator.power_unit != binding.power_unit:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery feedback unit does not match actuator"
        )
    if actuator.soc_reconciliation_capability != binding.soc_capability:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery SOC reconciliation does not match route binding"
        )
    if actuator.capability != binding.control_capability:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery actuator capability does not match route binding"
        )

    if capacity_evidence.provider_id != provider.manifest.provider_id:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery capacity provider does not match provider"
        )
    if capacity_evidence.device_id != canonical_device_id:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery capacity device does not match canonical device"
        )
    if (
        capacity_evidence.source_ref is not None
        and capacity_evidence.source_ref.external_id != binding.capacity_entity_id
    ):
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery capacity source does not match route binding"
        )
    if not math.isclose(
        capacity_evidence.capacity_kwh,
        profile.capacity_kwh,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery capacity does not match profile"
        )
    capacity_values: list[float] = []
    for state in snapshot.source_states:
        value = state.get("value")
        if (
            str(state.get("entity_id")) == binding.capacity_entity_id
            and state.get("unit") == "kWh"
            and state.get("available", True)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            capacity_values.append(float(value))
    if len(capacity_values) != 1 or not math.isclose(
        capacity_values[0],
        capacity_evidence.capacity_kwh,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery capacity evidence does not match snapshot"
        )

    common_capabilities = _common_route_capabilities(
        snapshot,
        (binding.charge, binding.discharge, binding.stop),
    )
    if binding.control_capability not in common_capabilities:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery routes do not resolve to actuator capability"
        )

    observation = profile.initial_soc_observation
    if observation is None:
        raise HomeAssistantMappingConfigurationError(
            "dispatchable battery composition requires initial SOC observation"
        )
    if (
        observation.provider_id != provider.manifest.provider_id
        or observation.device_id != canonical_device_id
        or observation.metric != binding.soc_capability
    ):
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant battery SOC observation does not match route binding"
        )

    return DispatchableBatteryBinding(
        provider_id=provider.manifest.provider_id,
        device_id=canonical_device_id,
        soc_capability=binding.soc_capability,
        profile=profile,
        capacity_evidence=capacity_evidence,
        capacity_trust_policy=capacity_trust_policy,
    )


def _common_route_capabilities(
    snapshot: AdapterSnapshot,
    routes: Iterable[HomeAssistantBatteryCommandRoute],
) -> set[str]:
    source_entities = {
        str(entity["entity_id"]): entity for entity in snapshot.source_entities
    }
    candidates: list[set[str]] = []
    for route in routes:
        entity = source_entities.get(route.entity_id)
        if entity is None:
            return set()
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        candidates.append(
            {
                capability.name
                for capability in capabilities
                if capability.writable and route.provider_command in capability.commands
            }
        )
    if not candidates:
        return set()
    return set.intersection(*candidates)


__all__ = ["compose_home_assistant_dispatchable_battery_binding"]
