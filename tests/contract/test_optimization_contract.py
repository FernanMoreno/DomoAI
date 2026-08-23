from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.models import (
    Command,
    CommandPostcondition,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.domain.provider import MeasurementQuality, NominalCapacityAttestation
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EnergyContext,
)
from domoai.optimizer.ports import OptimizationStatus, build_result
from domoai.optimizer.providers import StateStoreBatteryProvider
from domoai.optimizer.scenario import (
    Constraint,
    Horizon,
    Load,
    Objective,
    OptimizationScenario,
    validate_scenario,
)
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for


def test_scenario_contract_serializes_versioned_horizon_and_objectives() -> None:
    scenario = OptimizationScenario(
        id="scenario-contract-1",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 4, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        ),
        loads=[
            Load(
                id="load-1",
                device_id="garage.ev",
                capability="power",
                command="turn_on",
                power=3.6,
                power_unit="kW",
            )
        ],
        constraints=[Constraint(type="max_house_power", value=5750, unit="W")],
        objectives=[Objective(name="minimize_start", direction="minimize", weight=1)],
    )

    payload = scenario.model_dump(mode="json")

    assert payload["schema_version"] == "v1"
    assert payload["horizon"]["resolution_minutes"] == 60
    assert payload["objectives"][0]["direction"] == "minimize"


def test_scenario_rejects_invalid_horizon_and_non_positive_resolution() -> None:
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, 4, tzinfo=UTC),
            end=datetime(2026, 8, 15, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        )
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, 30, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        )
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, tzinfo=UTC),
            resolution_minutes=0,
            timezone="Europe/Madrid",
        )


def test_dispatchable_battery_binding_round_trips_v1_json() -> None:
    context = energy_context_for()
    assert context.battery is not None
    context = context.model_copy(
        update={
            "battery": context.battery.model_copy(
                update={
                    "actuator": BatteryActuator(
                        device_id="battery.home",
                        capability="battery_power",
                        charge_command="charge_battery",
                        discharge_command="discharge_battery",
                        stop_command="stop_battery",
                        power_feedback_capability="battery_power",
                        power_feedback_tolerance_kw=0.1,
                        power_feedback_settle_timeout_seconds=5.0,
                        power_feedback_poll_interval_seconds=0.25,
                    ),
                    "initial_soc_observation": BatterySocObservation(
                        provider_id="battery_fixture",
                        device_id="battery.home",
                        value_kwh=2.0,
                        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                        source_ref=SourceRef(
                            adapter_id="battery_fixture", external_id="battery_entity"
                        ),
                        conversion_evidence=BatterySocConversionEvidence(
                            source_value_percent=50.0,
                            capacity=BatteryCapacityEvidence(
                                provider_id="battery_fixture",
                                device_id="battery.home",
                                capacity_kwh=4.0,
                            ),
                        ),
                    ),
                }
            )
        }
    )

    payload = context.model_dump(mode="json")
    restored = EnergyContext.model_validate(payload)

    assert payload["battery"]["actuator"]["power_unit"] == "kW"
    assert restored.battery is not None
    assert restored.battery.actuator is not None
    assert restored.battery.actuator.device_id == "battery.home"
    assert restored.battery.actuator.power_feedback_settle_timeout_seconds == 5.0
    assert restored.battery.initial_soc_observation is not None
    assert restored.battery.initial_soc_observation.provider_id == "battery_fixture"
    assert restored.battery.initial_soc_observation.metric == "battery.soc"
    assert restored.battery.initial_soc_observation.conversion_evidence is not None
    assert (
        restored.battery.initial_soc_observation.conversion_evidence.capacity.capacity_kwh == 4.0
    )


def test_dispatchable_battery_binding_contract_round_trips_v1_json() -> None:
    profile = energy_context_for().battery
    assert profile is not None
    actuator = BatteryActuator(
        device_id="battery.home",
        capability="battery_power",
        charge_command="charge_battery",
        discharge_command="discharge_battery",
        stop_command="stop_battery",
        power_feedback_capability="battery_power",
        power_feedback_tolerance_kw=0.1,
        soc_reconciliation_capability="battery.soc",
    )
    binding = DispatchableBatteryBinding(
        provider_id="battery_fixture",
        device_id="battery.home",
        profile=profile.model_copy(update={"actuator": actuator}),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="battery_fixture",
            device_id="battery.home",
            capacity_kwh=profile.capacity_kwh,
        ),
    )

    restored = DispatchableBatteryBinding.model_validate(
        binding.model_dump(mode="json")
    )

    assert restored == binding
    assert restored.profile.actuator is not None
    assert restored.profile.actuator.soc_reconciliation_capability == "battery.soc"


def test_measured_capacity_evidence_round_trips_v1_json() -> None:
    evidence = BatteryCapacityEvidence(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        capacity_kwh=8.0,
        capacity_source="provider_measurement",
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_capacity"
        ),
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, 1, tzinfo=UTC),
        nominal_capacity_attestation=NominalCapacityAttestation(
            evidence_type="vendor_documentation",
            reference="https://www.tesla.com/powerwall",
            subject_model="Powerwall 2",
            attested_by="operator",
            attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        ),
    )

    restored = BatteryCapacityEvidence.model_validate(evidence.model_dump(mode="json"))

    assert restored == evidence
    assert restored.capacity_source == "provider_measurement"
    assert restored.source_ref is not None
    assert restored.source_ref.external_id == "sensor.battery_capacity"


def test_post_write_reconciliation_request_round_trips_v1_json() -> None:
    command = Command(
        id="battery-command-reconcile-contract",
        device_id="battery.home",
        command="charge_battery",
        value=2.0,
        unit="kW",
        idempotency_key="battery-command-reconcile-contract",
        postconditions=[
            CommandPostcondition(
                capability="battery_power",
                expected=2.0,
                tolerance=0.1,
                reconcile_capabilities=["battery.soc"],
            )
        ],
    )

    restored = Command.model_validate(command.model_dump(mode="json"))

    assert restored.postconditions[0].reconcile_capabilities == ["battery.soc"]


@pytest.mark.asyncio
async def test_state_store_battery_provider_result_round_trips_v1_json() -> None:
    context = energy_context_for()
    assert context.battery is not None
    observed_at = datetime(2026, 8, 15, 12, tzinfo=UTC)
    store = StateStore()
    await store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.soc",
            value=3.0,
            unit="kWh",
            observed_at=observed_at,
            received_at=observed_at,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(
                adapter_id="battery_fixture", external_id="battery_entity"
            ),
        )
    )

    state = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=context.battery,
    ).get_state(context.horizon)
    restored = type(state).model_validate(state.model_dump(mode="json"))

    assert restored.source_id == "battery_fixture"
    assert restored.battery is not None
    assert restored.battery.initial_soc_kwh == 3.0


def test_optimization_result_statuses_are_typed_and_diagnostics_are_structured() -> None:
    result = build_result(
        scenario_id="scenario-contract-2",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[{"code": "infeasible", "message": "Power limit is too low"}],
    )

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None
    assert result.diagnostics[0].code == "infeasible"


def test_scenario_rejects_vendor_adapter_and_solver_code_inputs() -> None:
    scenario = OptimizationScenario(
        id="scenario-semantic-only",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        ),
        inputs=[{"adapter_id": "vendor-api", "code": "execute_python(...)"}],
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert [item.code for item in diagnostics] == ["non_semantic_input"]
