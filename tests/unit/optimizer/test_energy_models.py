from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.models import Command, CommandPostcondition, SourceRef
from domoai.domain.provider import MeasurementQuality, NominalCapacityAttestation
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EnergyContext,
    NominalCapacityTrustPolicy,
    StaticEnergyContextProvider,
)
from tests.fixtures.energy import energy_context_for, energy_horizon


def _dispatchable_binding(
    *, measured_capacity: bool = False, policy: NominalCapacityTrustPolicy | None = None
) -> DispatchableBatteryBinding:
    profile = energy_context_for().battery
    assert profile is not None
    profile = profile.model_copy(
        update={
            "actuator": BatteryActuator(
                device_id="battery.home",
                capability="battery_power",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery_power",
                power_feedback_tolerance_kw=0.1,
                soc_reconciliation_capability="battery.soc",
            )
        }
    )
    evidence_payload: dict[str, object] = {
        "provider_id": "battery_fixture",
        "device_id": "battery.home",
        "capacity_kwh": profile.capacity_kwh,
    }
    if measured_capacity:
        evidence_payload.update(
            {
                "capacity_source": "provider_measurement",
                "source_ref": SourceRef(
                    adapter_id="battery_fixture", external_id="battery_capacity"
                ),
                "observed_at": datetime(2026, 8, 15, 12, tzinfo=UTC),
                "received_at": datetime(2026, 8, 15, 12, tzinfo=UTC),
                "nominal_capacity_attestation": NominalCapacityAttestation(
                    evidence_type="vendor_documentation",
                    reference="reference",
                    subject_model="Battery Model",
                    attested_by="operator",
                    attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
                ),
            }
        )
    return DispatchableBatteryBinding(
        provider_id="battery_fixture",
        device_id="battery.home",
        profile=profile,
        capacity_evidence=BatteryCapacityEvidence.model_validate(evidence_payload),
        capacity_trust_policy=policy,
    )


def test_energy_context_is_complete_and_provider_is_read_only() -> None:
    context = energy_context_for()
    provider = StaticEnergyContextProvider(context)

    assert [point.slot for point in context.tariffs] == list(range(context.horizon.slots))
    assert provider.get_context(context.horizon) == context
    assert not hasattr(provider, "execute")


@pytest.mark.parametrize(
    "field",
    ["tariffs", "solar_forecast"],
)
def test_energy_context_rejects_missing_or_duplicate_slots(field: str) -> None:
    context = energy_context_for()
    points = list(getattr(context, field))[:-1]
    if field == "tariffs":
        payload = context.model_copy(update={"tariffs": points})
    else:
        payload = context.model_copy(update={"solar_forecast": points})

    with pytest.raises(ValidationError, match="must contain exactly one point"):
        EnergyContext.model_validate(payload.model_dump(mode="python"))


def test_energy_context_rejects_bad_units_and_battery_bounds() -> None:
    context = energy_context_for()
    payload = context.model_dump(mode="python")
    payload["solar_forecast"][0]["unit"] = "W"
    with pytest.raises(ValidationError, match="unit"):
        EnergyContext.model_validate(payload)

    invalid_battery = context.model_dump(mode="python")
    invalid_battery["battery"]["initial_soc_kwh"] = 99
    with pytest.raises(ValidationError, match="initial_soc"):
        EnergyContext.model_validate(invalid_battery)


def test_battery_actuator_is_optional_but_commands_must_be_distinct() -> None:
    context = energy_context_for()
    assert context.battery is not None
    assert context.battery.actuator is None

    with pytest.raises(ValidationError, match="distinct"):
        BatteryActuator(
            device_id="garage.home_battery",
            capability="battery_power",
            charge_command="set_power",
            discharge_command="set_power",
            stop_command="stop_battery",
            power_feedback_capability="battery_power",
            power_feedback_tolerance_kw=0.1,
        )


def test_battery_actuator_requires_explicit_power_feedback_contract() -> None:
    with pytest.raises(ValidationError, match="power_feedback_capability"):
        BatteryActuator(
            device_id="garage.home_battery",
            capability="battery_power",
            charge_command="charge_battery",
            discharge_command="discharge_battery",
            stop_command="stop_battery",
        )

    with pytest.raises(ValidationError, match="poll interval"):
        BatteryActuator(
            device_id="garage.home_battery",
            capability="battery_power",
            charge_command="charge_battery",
            discharge_command="discharge_battery",
            stop_command="stop_battery",
            power_feedback_capability="battery_power",
            power_feedback_tolerance_kw=0.1,
            power_feedback_settle_timeout_seconds=1.0,
            power_feedback_poll_interval_seconds=2.0,
        )


def test_battery_soc_reconciliation_is_optional_and_typed() -> None:
    actuator = BatteryActuator(
        device_id="garage.home_battery",
        capability="battery_power",
        charge_command="charge_battery",
        discharge_command="discharge_battery",
        stop_command="stop_battery",
        power_feedback_capability="battery_power",
        power_feedback_tolerance_kw=0.1,
        soc_reconciliation_capability="battery.soc",
    )
    assert actuator.soc_reconciliation_capability == "battery.soc"

    postcondition = CommandPostcondition(
        capability="battery_power",
        expected=2.0,
        reconcile_capabilities=["battery.soc"],
    )
    assert postcondition.reconcile_capabilities == ["battery.soc"]


def test_dispatchable_battery_binding_round_trips_complete_contract() -> None:
    binding = _dispatchable_binding()

    restored = DispatchableBatteryBinding.model_validate(
        binding.model_dump(mode="json")
    )

    assert restored == binding
    assert restored.soc_capability == "battery.soc"
    assert restored.profile.actuator is not None
    assert restored.profile.actuator.soc_reconciliation_capability == "battery.soc"


def test_dispatchable_battery_binding_requires_actuator_and_soc_reconciliation() -> None:
    payload = _dispatchable_binding().model_dump(mode="python")
    payload["profile"]["actuator"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="actuator"):
        DispatchableBatteryBinding.model_validate(payload)

    payload = _dispatchable_binding().model_dump(mode="python")
    payload["profile"]["actuator"]["soc_reconciliation_capability"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="reconciliation"):
        DispatchableBatteryBinding.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("device_id", "device"),
        ("capacity_kwh", "capacity"),
    ],
)
def test_dispatchable_battery_binding_rejects_cross_field_drift(
    mutation: str, match: str
) -> None:
    payload = _dispatchable_binding().model_dump(mode="python")
    if mutation == "device_id":
        payload["device_id"] = "other.battery"
    else:
        payload["capacity_evidence"]["capacity_kwh"] = 5.0  # type: ignore[index]

    with pytest.raises(ValidationError, match=match):
        DispatchableBatteryBinding.model_validate(payload)


def test_dispatchable_battery_binding_requires_policy_for_measured_capacity() -> None:
    with pytest.raises(ValidationError, match="trust policy"):
        _dispatchable_binding(measured_capacity=True)


def test_battery_soc_observation_round_trips_with_profile_provenance() -> None:
    context = energy_context_for()
    assert context.battery is not None
    observation = BatterySocObservation(
        provider_id="inverter_fixture",
        device_id="battery.home",
        value_kwh=2.0,
        observed_at=datetime(2026, 8, 15, 11, 59, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(adapter_id="inverter_fixture", external_id="battery_entity"),
    )

    profile = context.battery.model_copy(update={"initial_soc_observation": observation})
    restored = type(profile).model_validate(profile.model_dump(mode="json"))

    assert restored.initial_soc_observation == observation
    assert restored.initial_soc_observation is not None
    assert restored.initial_soc_observation.unit == "kWh"


def test_battery_soc_conversion_evidence_round_trips_with_observation() -> None:
    evidence = BatterySocConversionEvidence(
        source_value_percent=50.0,
        capacity=BatteryCapacityEvidence(
            provider_id="inverter_fixture",
            device_id="battery.home",
            capacity_kwh=4.0,
        ),
    )
    observation = BatterySocObservation(
        provider_id="inverter_fixture",
        device_id="battery.home",
        value_kwh=2.0,
        observed_at=datetime(2026, 8, 15, 11, 59, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        source_ref=SourceRef(adapter_id="inverter_fixture", external_id="battery_entity"),
        conversion_evidence=evidence,
    )

    restored = BatterySocObservation.model_validate(observation.model_dump(mode="json"))

    assert restored.conversion_evidence == evidence


def test_nominal_capacity_trust_policy_round_trips_as_server_owned_config() -> None:
    policy = NominalCapacityTrustPolicy(
        allowed_evidence_types=["vendor_documentation"],
        trusted_attesters=["operator"],
        trusted_references=["https://www.tesla.com/powerwall"],
    )

    restored = NominalCapacityTrustPolicy.model_validate(
        policy.model_dump(mode="json")
    )

    assert restored == policy


@pytest.mark.parametrize(
    "payload",
    [
        {
            "allowed_evidence_types": [],
            "trusted_attesters": ["operator"],
            "trusted_references": ["reference"],
        },
        {
            "allowed_evidence_types": ["vendor_documentation"],
            "trusted_attesters": ["operator", "operator"],
            "trusted_references": ["reference"],
        },
        {
            "allowed_evidence_types": ["vendor_documentation"],
            "trusted_attesters": ["operator"],
            "trusted_references": ["", "reference"],
        },
        {
            "allowed_evidence_types": ["vendor_documentation"],
            "trusted_attesters": ["operator"],
            "trusted_references": ["reference"],
            "unexpected": True,
        },
    ],
)
def test_nominal_capacity_trust_policy_rejects_invalid_allowlists(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        NominalCapacityTrustPolicy.model_validate(payload)


def test_measured_battery_capacity_evidence_round_trips_provenance() -> None:
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


def test_measured_battery_capacity_requires_provenance() -> None:
    with pytest.raises(ValidationError, match="source_ref and timestamps"):
        BatteryCapacityEvidence(
            provider_id="home_assistant",
            device_id="ha-battery-1",
            capacity_kwh=8.0,
            capacity_source="provider_measurement",
        )


def test_measured_battery_capacity_requires_nominal_attestation() -> None:
    with pytest.raises(ValidationError, match="nominal capacity attestation"):
        BatteryCapacityEvidence(
            provider_id="home_assistant",
            device_id="ha-battery-1",
            capacity_kwh=8.0,
            capacity_source="provider_measurement",
            source_ref=SourceRef(
                adapter_id="home_assistant", external_id="sensor.battery_capacity"
            ),
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("observed_at", "received_at", "value_kwh", "match"),
    [
        (datetime(2026, 8, 15, 12), datetime(2026, 8, 15, 12, tzinfo=UTC), 2.0, "timezone"),
        (
            datetime(2026, 8, 15, 12, tzinfo=UTC),
            datetime(2026, 8, 15, 11, 59, tzinfo=UTC),
            2.0,
            "received_at",
        ),
        (
            datetime(2026, 8, 15, 12, tzinfo=UTC),
            datetime(2026, 8, 15, 12, tzinfo=UTC),
            float("nan"),
            "value_kwh",
        ),
    ],
)
def test_battery_soc_observation_rejects_invalid_measurements(
    observed_at: datetime,
    received_at: datetime,
    value_kwh: float,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        BatterySocObservation(
            provider_id="inverter_fixture",
            device_id="battery.home",
            value_kwh=value_kwh,
            observed_at=observed_at,
            received_at=received_at,
            source_ref=SourceRef(adapter_id="inverter_fixture", external_id="battery_entity"),
        )


def test_battery_profile_rejects_soc_value_mismatch() -> None:
    context = energy_context_for()
    assert context.battery is not None
    payload = context.battery.model_dump(mode="python")
    payload["initial_soc_observation"] = {
        "provider_id": "inverter_fixture",
        "device_id": "battery.home",
        "value_kwh": 2.5,
        "observed_at": datetime(2026, 8, 15, 12, tzinfo=UTC),
        "received_at": datetime(2026, 8, 15, 12, tzinfo=UTC),
        "source_ref": {"adapter_id": "inverter_fixture", "external_id": "battery_entity"},
    }

    with pytest.raises(ValidationError, match="initial_soc_observation"):
        type(context.battery).model_validate(payload)


def test_command_postcondition_tolerance_is_numeric_only() -> None:
    with pytest.raises(ValidationError, match="numeric"):
        CommandPostcondition(capability="battery_power", expected="charging", tolerance=0.1)

    command = Command(
        id="battery-command-1",
        device_id="garage.home_battery",
        command="charge_battery",
        value=2.0,
        unit="kW",
        idempotency_key="battery-command-1",
        postconditions=[
            CommandPostcondition(capability="battery_power", expected=2.0, tolerance=0.1)
        ],
    )

    assert command.postconditions[0].expected == 2.0


def test_feedback_settling_policy_has_safe_defaults_and_bounds() -> None:
    immediate = CommandPostcondition(capability="battery_power", expected=0.0)
    assert immediate.settle_timeout_seconds is None
    assert immediate.poll_interval_seconds == 0.25

    bounded = CommandPostcondition(
        capability="battery_power",
        expected=2.0,
        tolerance=0.1,
        settle_timeout_seconds=5.0,
        poll_interval_seconds=0.25,
    )
    assert bounded.settle_timeout_seconds == 5.0

    with pytest.raises(ValidationError, match="120"):
        CommandPostcondition(
            capability="battery_power", expected=2.0, settle_timeout_seconds=120.1
        )
    with pytest.raises(ValidationError, match="poll"):
        CommandPostcondition(
            capability="battery_power",
            expected=2.0,
            settle_timeout_seconds=1.0,
            poll_interval_seconds=2.0,
        )


def test_static_provider_rejects_a_different_horizon() -> None:
    provider = StaticEnergyContextProvider(energy_context_for())
    different = energy_horizon(slots=4)

    with pytest.raises(ValueError, match="horizon"):
        provider.get_context(different)
