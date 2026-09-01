from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.domain.provider import (
    Measurement,
    MeasurementQuality,
    NominalCapacityAttestation,
)
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EVActuator,
    EVChargingBinding,
    ExteriorTemperaturePoint,
    NominalCapacityTrustPolicy,
    TariffPoint,
)
from domoai.optimizer.providers import (
    BatteryState,
    ComposedEnergyContextProvider,
    EnergyProviderError,
    ExteriorTemperatureSeries,
    SolarForecastSeries,
    StateStoreBatteryProvider,
    StateStoreEVProvider,
    StaticBatteryProvider,
    StaticExteriorTemperatureProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
    battery_capacity_evidence_from_measurement,
    battery_soc_observation_from_measurement,
    battery_soc_observation_from_percentage_measurement,
    validate_nominal_capacity_trust,
)
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, energy_horizon

NOW = datetime(2026, 8, 15, 12, 5, tzinfo=UTC)


def nominal_capacity_attestation() -> NominalCapacityAttestation:
    return NominalCapacityAttestation(
        evidence_type="vendor_documentation",
        reference="https://www.tesla.com/powerwall",
        subject_model="Powerwall 2",
        attested_by="operator",
        attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def dispatchable_battery_state(
    *,
    observation: BatterySocObservation | None = None,
    source_id: str = "battery_fixture",
) -> BatteryState:
    context = energy_context_for()
    assert context.battery is not None
    observation = observation or BatterySocObservation(
        provider_id=source_id,
        device_id="battery.home",
        value_kwh=context.battery.initial_soc_kwh,
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        source_ref=SourceRef(adapter_id=source_id, external_id="battery_entity"),
    )
    profile = context.battery.model_copy(
        update={
            "actuator": BatteryActuator(
                device_id="battery.home",
                capability="battery_power",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery_power",
                power_feedback_tolerance_kw=0.1,
            ),
            "initial_soc_observation": observation,
        }
    )
    return BatteryState(
        horizon=context.horizon,
        source_id=source_id,
        source_revision="state-1",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        battery=profile,
    )


def provider_inputs() -> tuple[TariffSeries, SolarForecastSeries, BatteryState]:
    context = energy_context_for()
    return (
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="day-ahead-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            points=context.tariffs,
        ),
        SolarForecastSeries(
            horizon=context.horizon,
            source_id="solar_fixture",
            source_revision="forecast-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            points=context.solar_forecast,
        ),
        BatteryState(
            horizon=context.horizon,
            source_id="battery_fixture",
            source_revision="state-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            battery=context.battery,
        ),
    )


def provider() -> ComposedEnergyContextProvider:
    tariffs, solar, battery = provider_inputs()
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: NOW,
    )


def _battery_snapshot(
    value: object,
    *,
    unit: str | None = "kWh",
    status: StateStatus = StateStatus.CURRENT,
    provider_id: str = "battery_fixture",
    device_id: str = "battery.home",
    observed_at: datetime = datetime(2026, 8, 15, 12, tzinfo=UTC),
) -> StateSnapshot:
    return StateSnapshot(
        device_id=device_id,
        capability="battery.soc",
        value=value,
        unit=unit,
        observed_at=observed_at,
        received_at=observed_at,
        status=status,
        source_ref=SourceRef(adapter_id=provider_id, external_id="battery_entity"),
    )


async def _battery_store(snapshot: StateSnapshot) -> StateStore:
    store = StateStore()
    await store.save(snapshot)
    return store


def _battery_provider_profile():
    context = energy_context_for()
    assert context.battery is not None
    return context.battery


def _dispatchable_battery_provider_profile():
    return _battery_provider_profile().model_copy(
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


def _measured_capacity_evidence() -> BatteryCapacityEvidence:
    return BatteryCapacityEvidence(
        provider_id="battery_fixture",
        device_id="battery.home",
        capacity_kwh=8.0,
        capacity_source="provider_measurement",
        source_ref=SourceRef(
            adapter_id="battery_fixture", external_id="battery_capacity"
        ),
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        nominal_capacity_attestation=nominal_capacity_attestation(),
    )


def _nominal_capacity_trust_policy() -> NominalCapacityTrustPolicy:
    return NominalCapacityTrustPolicy(
        allowed_evidence_types=["vendor_documentation"],
        trusted_attesters=["operator"],
        trusted_references=["https://www.tesla.com/powerwall"],
    )


def _complete_dispatchable_binding(
    *, measured_capacity: bool = False, policy: NominalCapacityTrustPolicy | None = None
) -> DispatchableBatteryBinding:
    profile = _dispatchable_battery_provider_profile()
    evidence = BatteryCapacityEvidence(
        provider_id="battery_fixture",
        device_id="battery.home",
        capacity_kwh=profile.capacity_kwh,
        capacity_source="provider_measurement" if measured_capacity else "provider_config",
        source_ref=(
            SourceRef(adapter_id="battery_fixture", external_id="battery_capacity")
            if measured_capacity
            else None
        ),
        observed_at=(datetime(2026, 8, 15, 12, tzinfo=UTC) if measured_capacity else None),
        received_at=(datetime(2026, 8, 15, 12, tzinfo=UTC) if measured_capacity else None),
        nominal_capacity_attestation=(
            nominal_capacity_attestation() if measured_capacity else None
        ),
    )
    return DispatchableBatteryBinding(
        provider_id="battery_fixture",
        device_id="battery.home",
        profile=profile,
        capacity_evidence=evidence,
        capacity_trust_policy=policy,
    )


@pytest.mark.asyncio
async def test_state_store_battery_provider_composes_current_kwh_into_energy_context() -> None:
    store = await _battery_store(_battery_snapshot(3.25))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
    )

    state = provider.get_state(energy_context_for().horizon)

    assert state.battery is not None
    assert state.battery.initial_soc_kwh == pytest.approx(3.25)
    assert state.battery.initial_soc_observation is not None
    assert state.battery.initial_soc_observation.value_kwh == pytest.approx(3.25)
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        provider,
        now=lambda: NOW,
    )
    context = composed.get_context(energy_context_for().horizon)
    assert context.battery is not None
    assert context.battery.initial_soc_kwh == pytest.approx(3.25)


@pytest.mark.asyncio
async def test_state_store_battery_provider_converts_percent_only_with_matching_capacity() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    capacity = BatteryCapacityEvidence(
        provider_id="battery_fixture",
        device_id="battery.home",
        capacity_kwh=8.0,
    )
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
        capacity_evidence=capacity,
    )

    state = provider.get_state(energy_context_for().horizon)

    assert state.battery is not None
    assert state.battery.initial_soc_kwh == pytest.approx(4.0)
    observation = state.battery.initial_soc_observation
    assert observation is not None
    assert observation.conversion_evidence is not None
    assert observation.conversion_evidence.capacity == capacity


@pytest.mark.asyncio
async def test_state_store_battery_provider_from_binding_is_explicit_and_complete() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    binding = _complete_dispatchable_binding()

    provider = StateStoreBatteryProvider.from_binding(
        state_store=store,
        binding=binding,
    )
    state = provider.get_state(energy_context_for().horizon)

    assert state.battery is not None
    assert state.battery.actuator is not None
    assert state.battery.initial_soc_kwh == pytest.approx(3.0)


def test_state_store_battery_provider_from_binding_checks_measured_trust_before_use() -> None:
    binding = _complete_dispatchable_binding(
        measured_capacity=True,
        policy=_nominal_capacity_trust_policy(),
    )
    mismatched_policy = binding.capacity_trust_policy.model_copy(
        update={"trusted_attesters": ["other-operator"]}
    )
    mismatched_binding = binding.model_copy(
        update={"capacity_trust_policy": mismatched_policy}
    )

    with pytest.raises(EnergyProviderError) as raised:
        StateStoreBatteryProvider.from_binding(
            state_store=StateStore(),
            binding=mismatched_binding,
        )

    assert raised.value.diagnostic.code == "nominal_capacity_attester_not_trusted"


def test_nominal_capacity_trust_evaluator_requires_server_policy() -> None:
    with pytest.raises(EnergyProviderError) as raised:
        validate_nominal_capacity_trust(_measured_capacity_evidence(), None)

    assert raised.value.diagnostic.code == "nominal_capacity_trust_required"
    assert "tesla" not in str(raised.value).casefold()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "allowed_evidence_types",
            ["installer_attestation"],
            "nominal_capacity_evidence_type_not_trusted",
        ),
        (
            "trusted_attesters",
            ["other-operator"],
            "nominal_capacity_attester_not_trusted",
        ),
        (
            "trusted_references",
            ["other-reference"],
            "nominal_capacity_reference_not_trusted",
        ),
    ],
)
def test_nominal_capacity_trust_evaluator_requires_all_exact_dimensions(
    field: str, value: list[str], code: str
) -> None:
    policy = _nominal_capacity_trust_policy().model_copy(update={field: value})

    with pytest.raises(EnergyProviderError) as raised:
        validate_nominal_capacity_trust(_measured_capacity_evidence(), policy)

    assert raised.value.diagnostic.code == code


def test_nominal_capacity_trust_evaluator_accepts_exact_match() -> None:
    validate_nominal_capacity_trust(
        _measured_capacity_evidence(), _nominal_capacity_trust_policy()
    )


@pytest.mark.asyncio
async def test_dispatchable_state_store_provider_requires_trust_for_measured_capacity() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_dispatchable_battery_provider_profile(),
        capacity_evidence=_measured_capacity_evidence(),
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "nominal_capacity_trust_required"


@pytest.mark.asyncio
async def test_dispatchable_state_store_provider_accepts_trusted_measured_capacity() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_dispatchable_battery_provider_profile(),
        capacity_evidence=_measured_capacity_evidence(),
        capacity_trust_policy=_nominal_capacity_trust_policy(),
    )

    state = provider.get_state(energy_context_for().horizon)

    assert state.battery is not None
    assert state.battery.actuator is not None
    assert state.battery.initial_soc_kwh == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_analysis_only_and_static_capacity_paths_do_not_require_trust_policy() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    analysis_provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
        capacity_evidence=_measured_capacity_evidence(),
        capacity_trust_policy=_nominal_capacity_trust_policy(),
    )
    analysis_state = analysis_provider.get_state(energy_context_for().horizon)
    assert analysis_state.battery is not None
    assert analysis_state.battery.actuator is None

    static_provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_dispatchable_battery_provider_profile(),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="battery_fixture",
            device_id="battery.home",
            capacity_kwh=8.0,
        ),
    )
    static_state = static_provider.get_state(energy_context_for().horizon)
    assert static_state.battery is not None
    assert static_state.battery.actuator is not None


@pytest.mark.asyncio
async def test_stale_soc_preserves_quality_and_rejects_dispatchable_profile() -> None:
    store = await _battery_store(_battery_snapshot(3.0, status=StateStatus.STALE))
    analysis_provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
    )
    analysis_state = analysis_provider.get_state(energy_context_for().horizon)
    assert analysis_state.battery is not None
    assert analysis_state.battery.initial_soc_observation is not None
    assert (
        analysis_state.battery.initial_soc_observation.quality
        is MeasurementQuality.STALE
    )
    profile = _battery_provider_profile().model_copy(
        update={
            "actuator": BatteryActuator(
                device_id="battery.home",
                capability="battery_power",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery_power",
                power_feedback_tolerance_kw=0.1,
            )
        }
    )
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=profile,
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "battery_state_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "value", "unit", "code"),
    [
        (StateStatus.UNAVAILABLE, "unavailable", "kWh", "battery_soc_unavailable"),
        (StateStatus.INVALID, None, "kWh", "battery_soc_invalid"),
        (StateStatus.CURRENT, 3.0, "Wh", "unsupported_battery_soc_unit"),
        (StateStatus.CURRENT, "three", "kWh", "invalid_battery_soc_measurement"),
    ],
)
async def test_state_store_battery_provider_fails_closed_for_degraded_snapshots(
    status: StateStatus, value: object, unit: str, code: str
) -> None:
    store = await _battery_store(_battery_snapshot(value, unit=unit, status=status))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == code
    assert "three" not in str(raised.value)


@pytest.mark.asyncio
async def test_state_store_battery_provider_rejects_cross_identity_snapshot() -> None:
    store = await _battery_store(
        _battery_snapshot(3.0, provider_id="other_provider", device_id="battery.home")
    )
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "battery_soc_identity_mismatch"


@pytest.mark.asyncio
async def test_state_store_battery_provider_rejects_missing_or_bad_capacity() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "invalid_battery_capacity"


@pytest.mark.asyncio
async def test_state_store_battery_provider_rejects_cross_identity_capacity() -> None:
    store = await _battery_store(_battery_snapshot(50, unit="%"))
    provider = StateStoreBatteryProvider(
        state_store=store,
        provider_id="battery_fixture",
        device_id="battery.home",
        soc_capability="battery.soc",
        profile=_battery_provider_profile(),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="battery_fixture",
            device_id="other.battery",
            capacity_kwh=8.0,
        ),
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "invalid_battery_capacity"


@pytest.mark.asyncio
async def test_state_store_battery_provider_revision_changes_with_snapshot_value() -> None:
    first_store = await _battery_store(_battery_snapshot(2.0))
    second_store = await _battery_store(_battery_snapshot(2.5))
    kwargs = {
        "provider_id": "battery_fixture",
        "device_id": "battery.home",
        "soc_capability": "battery.soc",
        "profile": _battery_provider_profile(),
    }

    first = StateStoreBatteryProvider(state_store=first_store, **kwargs).get_state(
        energy_context_for().horizon
    )
    second = StateStoreBatteryProvider(state_store=second_store, **kwargs).get_state(
        energy_context_for().horizon
    )

    assert first.source_revision != second.source_revision


def _ev_binding(
    *, provider_id: str = "ev_fixture", device_id: str = "ev.home"
) -> EVChargingBinding:
    actuator = EVActuator(
        device_id=device_id,
        capability="ev_charging",
        charge_command="charge_ev",
        stop_command="stop_ev",
        connected_capability="ev.connected",
        departure_capability="ev.departure_at",
        max_charge_kw=7.4,
    )
    return EVChargingBinding.model_validate(
        {
            "provider_id": provider_id,
            "device_id": device_id,
            "actuator": actuator,
            "soc_capability": "ev.soc",
            "capacity_capability": "ev.capacity",
        }
    )


def _ev_snapshot(
    capability: str,
    value: object,
    *,
    unit: str | None = None,
    status: StateStatus = StateStatus.CURRENT,
    provider_id: str = "ev_fixture",
    device_id: str = "ev.home",
    observed_at: datetime = datetime(2026, 8, 15, 12, tzinfo=UTC),
) -> StateSnapshot:
    return StateSnapshot(
        device_id=device_id,
        capability=capability,
        value=value,
        unit=unit,
        observed_at=observed_at,
        received_at=observed_at,
        status=status,
        source_ref=SourceRef(adapter_id=provider_id, external_id=f"{device_id}:{capability}"),
    )


async def _ev_store(*snapshots: StateSnapshot) -> StateStore:
    store = StateStore()
    for snapshot in snapshots:
        await store.save(snapshot)
    return store


def _complete_ev_snapshots(
    *,
    connected: bool = True,
    soc: float = 20.0,
    capacity: float = 60.0,
    departure: str | None = "2026-08-16T06:00:00+00:00",
    status: StateStatus = StateStatus.CURRENT,
) -> list[StateSnapshot]:
    snapshots = [
        _ev_snapshot("ev.connected", connected, status=status),
        _ev_snapshot("ev.soc", soc, status=status),
        _ev_snapshot("ev.capacity", capacity, status=status),
    ]
    if departure is not None:
        snapshots.append(_ev_snapshot("ev.departure_at", departure, status=status))
    return snapshots


@pytest.mark.asyncio
async def test_state_store_ev_provider_composes_current_state() -> None:
    store = await _ev_store(*_complete_ev_snapshots())
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    state = provider.get_state(energy_context_for().horizon)

    assert state.device_id == "ev.home"
    assert state.connected is True
    assert state.soc_kwh == pytest.approx(20.0)
    assert state.capacity_kwh == pytest.approx(60.0)
    assert state.max_charge_kw == pytest.approx(7.4)
    assert state.departure_at is not None
    assert state.source_ref.adapter_id == "ev_fixture"


@pytest.mark.asyncio
async def test_state_store_ev_provider_converts_percentage_soc_using_capacity() -> None:
    store = await _ev_store(
        *_complete_ev_snapshots(soc=33.333, capacity=60.0),
    )
    await store.save(_ev_snapshot("ev.soc", 33.333, unit="%"))
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    state = provider.get_state(energy_context_for().horizon)

    assert state.soc_kwh == pytest.approx(19.9998)


@pytest.mark.asyncio
async def test_state_store_ev_provider_uses_oldest_component_timestamp() -> None:
    # A recent connection flag must not rejuvenate older SOC/capacity evidence.
    stale = NOW - timedelta(seconds=901)
    store = await _ev_store(
        _ev_snapshot("ev.connected", True, observed_at=NOW),
        _ev_snapshot("ev.soc", 20.0, observed_at=stale),
        _ev_snapshot("ev.capacity", 60.0, observed_at=NOW),
    )
    ev_provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        ev_providers=(ev_provider,),
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "stale_provider_data"


@pytest.mark.asyncio
async def test_state_store_ev_provider_revision_includes_departure_snapshot() -> None:
    store = await _ev_store(*_complete_ev_snapshots(departure="2026-08-16T06:00:00+00:00"))
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    first = provider.get_state(energy_context_for().horizon)
    await store.save(_ev_snapshot("ev.departure_at", "2026-08-16T07:00:00+00:00"))
    second = provider.get_state(energy_context_for().horizon)

    assert first.departure_at != second.departure_at
    assert first.source_revision != second.source_revision


@pytest.mark.asyncio
async def test_state_store_ev_provider_allows_unknown_departure() -> None:
    store = await _ev_store(*_complete_ev_snapshots(departure=None))
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    state = provider.get_state(energy_context_for().horizon)

    assert state.departure_at is None


@pytest.mark.asyncio
async def test_state_store_ev_provider_rejects_soc_above_capacity() -> None:
    # Spec 162 User Story 3 / FR-004: no clamping, hard rejection.
    store = await _ev_store(*_complete_ev_snapshots(soc=70.0, capacity=60.0))
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "invalid_ev_state"


@pytest.mark.asyncio
async def test_state_store_ev_provider_rejects_cross_identity_snapshot() -> None:
    # Spec 162 User Story 3 / FR-005.
    store = await _ev_store(
        *[
            _ev_snapshot("ev.connected", True, provider_id="other_provider"),
            _ev_snapshot("ev.soc", 20.0, provider_id="other_provider"),
            _ev_snapshot("ev.capacity", 60.0, provider_id="other_provider"),
        ]
    )
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "ev_state_identity_mismatch"


@pytest.mark.asyncio
async def test_state_store_ev_provider_fails_closed_when_a_required_snapshot_is_missing() -> None:
    store = StateStore()
    await store.save(_ev_snapshot("ev.connected", True))
    await store.save(_ev_snapshot("ev.soc", 20.0))
    # capacity snapshot deliberately never saved
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    with pytest.raises(EnergyProviderError) as raised:
        provider.get_state(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "ev_state_unavailable"


@pytest.mark.asyncio
async def test_composer_ev_providers_default_empty_is_non_regression() -> None:
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs), StaticSolarForecastProvider(solar), now=lambda: NOW
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.ev_states == []


@pytest.mark.asyncio
async def test_composer_populates_ev_states_from_ev_providers() -> None:
    store = await _ev_store(*_complete_ev_snapshots())
    ev_provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        ev_providers=(ev_provider,),
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    assert len(context.ev_states) == 1
    assert context.ev_states[0].device_id == "ev.home"
    assert "ev:ev_fixture@" in context.source_revision


@pytest.mark.asyncio
async def test_composer_attributes_two_ev_chargers_independently() -> None:
    store_a = await _ev_store(*_complete_ev_snapshots(soc=10.0))
    store_b = await _ev_store(
        *[
            _ev_snapshot("ev.connected", True, provider_id="ev_fixture_2", device_id="ev.second"),
            _ev_snapshot("ev.soc", 30.0, provider_id="ev_fixture_2", device_id="ev.second"),
            _ev_snapshot("ev.capacity", 40.0, provider_id="ev_fixture_2", device_id="ev.second"),
        ]
    )
    provider_a = StateStoreEVProvider(state_store=store_a, binding=_ev_binding())
    provider_b = StateStoreEVProvider(
        state_store=store_b,
        binding=_ev_binding(provider_id="ev_fixture_2", device_id="ev.second"),
    )
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        ev_providers=(provider_a, provider_b),
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    by_device = {state.device_id: state for state in context.ev_states}
    assert set(by_device) == {"ev.home", "ev.second"}
    assert by_device["ev.home"].soc_kwh == pytest.approx(10.0)
    assert by_device["ev.second"].soc_kwh == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_composer_refuses_stale_ev_state_before_use() -> None:
    # Spec 162 User Story 1 Scenario 2 / FR-002. Staleness is a timestamp
    # check against max_age_seconds at the composer level (the same generic
    # _validate_observed_at every other provider result already goes
    # through) -- StateSnapshot.status is a separate, StateStore-internal
    # concept and is deliberately not what this test exercises.
    # Tariff/solar fixtures (provider_inputs()) are observed 300s before NOW;
    # the default max_age_seconds is 900, so they stay fresh while the EV
    # snapshot below (901s before NOW) alone crosses the staleness line.
    stale_observed_at = NOW - timedelta(seconds=901)
    store = await _ev_store(
        *[
            _ev_snapshot("ev.connected", True, observed_at=stale_observed_at),
            _ev_snapshot("ev.soc", 20.0, observed_at=stale_observed_at),
            _ev_snapshot("ev.capacity", 60.0, observed_at=stale_observed_at),
        ]
    )
    ev_provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        ev_providers=(ev_provider,),
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "stale_provider_data"
    assert raised.value.diagnostic.provider_id == "ev_fixture"


@pytest.mark.asyncio
async def test_state_store_ev_provider_reports_disconnected_without_rejecting() -> None:
    # A disconnected charger is valid, truthful data -- refusing to charge on
    # it is DynamicSafetyGuard/validate_executable_scenario's existing,
    # unmodified job (contracts.md Non-goals), not this provider's.
    store = await _ev_store(*_complete_ev_snapshots(connected=False))
    provider = StateStoreEVProvider(state_store=store, binding=_ev_binding())

    state = provider.get_state(energy_context_for().horizon)

    assert state.connected is False


def test_composer_returns_complete_context_with_deterministic_revision() -> None:
    context = provider().get_context(energy_context_for().horizon)

    assert context.battery is not None
    assert context.source_revision == (
        "tariff:tariff_fixture@day-ahead-1|"
        "solar:solar_fixture@forecast-1|"
        "battery:battery_fixture@state-1"
    )
    assert context.observed_at == datetime(2026, 8, 15, 12, tzinfo=UTC)


def test_composer_allows_optional_battery() -> None:
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.battery is None
    assert context.source_revision.endswith("battery:none")


def test_composer_rejects_stale_provider_data_with_typed_diagnostic() -> None:
    tariffs, solar, battery = provider_inputs()
    stale = tariffs.model_copy(update={"observed_at": NOW - timedelta(seconds=61)})
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(stale),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        max_age_seconds=60,
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "stale_provider_data"
    assert raised.value.diagnostic.provider_id == "tariff_fixture"
    assert raised.value.diagnostic.details["max_age_seconds"] == 60


def test_composer_rejects_a_different_horizon() -> None:
    tariffs, solar, battery = provider_inputs()
    different_horizon = energy_context_for().horizon.model_copy(
        update={"end": energy_context_for().horizon.end + timedelta(minutes=15)}
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider().get_context(different_horizon)

    assert raised.value.diagnostic.code == "horizon_mismatch"
    assert raised.value.diagnostic.provider_id == "tariff_fixture"


def test_dispatchable_battery_preserves_soc_provenance() -> None:
    tariffs, solar, _ = provider_inputs()
    battery = dispatchable_battery_state()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.battery is not None
    assert context.battery.initial_soc_observation is not None
    assert context.battery.initial_soc_observation.device_id == "battery.home"
    assert context.battery.initial_soc_observation.source_ref.external_id == "battery_entity"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda profile: profile.model_copy(update={"initial_soc_observation": None}),
        lambda profile: profile.model_copy(
            update={
                "initial_soc_observation": BatterySocObservation(
                    provider_id="battery_fixture",
                    device_id="other.battery",
                    value_kwh=2.0,
                    observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                    received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                    source_ref=SourceRef(
                        adapter_id="battery_fixture", external_id="battery_entity"
                    ),
                )
            }
        ),
        lambda profile: profile.model_copy(
            update={
                "initial_soc_observation": BatterySocObservation(
                    provider_id="other_provider",
                    device_id="battery.home",
                    value_kwh=2.0,
                    observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                    received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                    source_ref=SourceRef(adapter_id="other_provider", external_id="battery_entity"),
                )
            }
        ),
    ],
)
def test_dispatchable_battery_requires_coherent_soc_observation(mutator) -> None:
    state = dispatchable_battery_state()
    assert state.battery is not None
    profile = mutator(state.battery)

    with pytest.raises(ValidationError):
        BatteryState.model_validate(state.model_copy(update={"battery": profile}).model_dump())


def test_dispatchable_battery_rejects_non_good_soc_quality() -> None:
    observation = BatterySocObservation(
        provider_id="battery_fixture",
        device_id="battery.home",
        value_kwh=2.0,
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.STALE,
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )

    with pytest.raises(ValidationError, match="GOOD"):
        dispatchable_battery_state(observation=observation)


def test_dispatchable_battery_rejects_stale_nested_soc_observation() -> None:
    observation = BatterySocObservation(
        provider_id="battery_fixture",
        device_id="battery.home",
        value_kwh=2.0,
        observed_at=NOW - timedelta(seconds=61),
        received_at=NOW - timedelta(seconds=60),
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )
    tariffs, solar, _ = provider_inputs()
    tariffs = tariffs.model_copy(update={"observed_at": NOW})
    solar = solar.model_copy(update={"observed_at": NOW})
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(dispatchable_battery_state(observation=observation)),
        max_age_seconds=60,
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "stale_provider_data"
    assert raised.value.diagnostic.provider_id == "battery_fixture"


def test_dispatchable_battery_rejects_future_nested_soc_observation() -> None:
    observation = BatterySocObservation(
        provider_id="battery_fixture",
        device_id="battery.home",
        value_kwh=2.0,
        observed_at=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=1),
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(dispatchable_battery_state(observation=observation)),
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "invalid_observed_at"


def test_measurement_bridge_preserves_soc_provenance() -> None:
    measurement = Measurement(
        provider_id="battery_fixture",
        device_id="battery.home",
        metric="battery.soc",
        value=2,
        unit="kWh",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, 1, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )

    observation = battery_soc_observation_from_measurement(measurement)

    assert observation.model_dump(mode="json") == {
        "schema_version": "v1",
        "provider_id": "battery_fixture",
        "device_id": "battery.home",
        "metric": "battery.soc",
        "value_kwh": 2.0,
        "unit": "kWh",
        "observed_at": "2026-08-15T12:00:00Z",
        "received_at": "2026-08-15T12:01:00Z",
        "quality": "good",
        "source_ref": {
            "adapter_id": "battery_fixture",
            "external_id": "battery_entity",
            "external_type": None,
            "metadata_digest": None,
        },
        "conversion_evidence": None,
    }


def test_percentage_bridge_converts_and_records_capacity_evidence() -> None:
    measurement = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.soc",
        value=50.0,
        unit="%",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, 1, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
    )
    capacity = BatteryCapacityEvidence(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        capacity_kwh=8.0,
    )

    observation = battery_soc_observation_from_percentage_measurement(measurement, capacity)

    assert observation.value_kwh == 4.0
    assert observation.unit == "kWh"
    assert observation.conversion_evidence is not None
    assert observation.conversion_evidence.source_value_percent == 50.0
    assert observation.conversion_evidence.capacity == capacity
    assert observation.conversion_evidence.method == "percentage_of_declared_capacity"


def test_capacity_measurement_bridge_preserves_measured_provenance() -> None:
    measurement = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.capacity",
        value=8.0,
        unit="kWh",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, 1, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_capacity"
        ),
        nominal_capacity_attestation=nominal_capacity_attestation(),
    )

    evidence = battery_capacity_evidence_from_measurement(measurement)

    assert evidence.capacity_kwh == 8.0
    assert evidence.capacity_source == "provider_measurement"
    assert evidence.quality is MeasurementQuality.GOOD
    assert evidence.source_ref == measurement.source_ref
    assert evidence.observed_at == measurement.observed_at
    assert evidence.received_at == measurement.received_at
    assert evidence.nominal_capacity_attestation == measurement.nominal_capacity_attestation


def test_percentage_bridge_accepts_good_measured_capacity() -> None:
    soc = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.soc",
        value=50.0,
        unit="%",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
    )
    capacity = battery_capacity_evidence_from_measurement(
        Measurement(
            provider_id="home_assistant",
            device_id="ha-battery-1",
            metric="battery.capacity",
            value=8.0,
            unit="kWh",
            observed_at=soc.observed_at,
            received_at=soc.received_at,
            source_ref=SourceRef(
                adapter_id="home_assistant", external_id="sensor.battery_capacity"
            ),
            nominal_capacity_attestation=nominal_capacity_attestation(),
        )
    )

    observation = battery_soc_observation_from_percentage_measurement(soc, capacity)

    assert observation.value_kwh == 4.0
    assert observation.conversion_evidence is not None
    assert observation.conversion_evidence.capacity.capacity_source == "provider_measurement"


def test_capacity_measurement_bridge_rejects_non_good_quality() -> None:
    measurement = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.capacity",
        value=8.0,
        unit="kWh",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.STALE,
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_capacity"
        ),
        nominal_capacity_attestation=nominal_capacity_attestation(),
    )

    evidence = battery_capacity_evidence_from_measurement(measurement)
    soc = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.soc",
        value=50.0,
        unit="%",
        observed_at=measurement.observed_at,
        received_at=measurement.received_at,
        source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_soc_observation_from_percentage_measurement(soc, evidence)

    assert raised.value.diagnostic.code == "invalid_battery_capacity"


def test_capacity_measurement_bridge_requires_nominal_attestation() -> None:
    measurement = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.capacity",
        value=8.0,
        unit="kWh",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_capacity"
        ),
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_capacity_evidence_from_measurement(measurement)

    assert raised.value.diagnostic.code == "missing_nominal_capacity_attestation"


def test_measurement_attestation_is_only_valid_for_capacity() -> None:
    with pytest.raises(ValueError, match="battery.capacity"):
        Measurement(
            provider_id="home_assistant",
            device_id="ha-battery-1",
            metric="battery.soc",
            value=50.0,
            unit="%",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            source_ref=SourceRef(
                adapter_id="home_assistant", external_id="sensor.battery_soc"
            ),
            nominal_capacity_attestation=nominal_capacity_attestation(),
        )


@pytest.mark.parametrize(
    ("metric", "unit", "value", "code"),
    [
        ("battery.soc", "kWh", 8.0, "invalid_battery_capacity"),
        ("battery.capacity", "%", 80.0, "unsupported_battery_capacity_unit"),
        ("battery.capacity", "kWh", 0.0, "invalid_battery_capacity"),
        ("battery.capacity", "kWh", -1.0, "invalid_battery_capacity"),
        ("battery.capacity", "kWh", True, "invalid_battery_capacity"),
        ("battery.capacity", "kWh", float("nan"), "invalid_battery_capacity"),
    ],
)
def test_capacity_measurement_bridge_rejects_ambiguous_inputs(
    metric: str, unit: str, value: object, code: str
) -> None:
    measurement = Measurement.model_construct(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric=metric,
        value=value,
        unit=unit,
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_capacity"
        ),
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_capacity_evidence_from_measurement(measurement)

    assert raised.value.diagnostic.code == code


@pytest.mark.parametrize(
    ("measurement_value", "capacity_value", "code"),
    [
        (-0.1, 8.0, "invalid_battery_soc_percentage"),
        (100.1, 8.0, "invalid_battery_soc_percentage"),
        (float("nan"), 8.0, "invalid_battery_soc_percentage"),
        (True, 8.0, "invalid_battery_soc_percentage"),
        (50.0, 0.0, "invalid_battery_capacity"),
        (50.0, -1.0, "invalid_battery_capacity"),
        (50.0, float("nan"), "invalid_battery_capacity"),
    ],
)
def test_percentage_bridge_rejects_invalid_values_and_capacity(
    measurement_value: object,
    capacity_value: float,
    code: str,
) -> None:
    measurement = Measurement.model_construct(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.soc",
        value=measurement_value,
        unit="%",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
    )
    capacity = BatteryCapacityEvidence.model_construct(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        capacity_kwh=capacity_value,
        capacity_source="provider_config",
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_soc_observation_from_percentage_measurement(measurement, capacity)

    assert raised.value.diagnostic.code == code


def test_percentage_bridge_rejects_capacity_from_another_device() -> None:
    measurement = Measurement(
        provider_id="home_assistant",
        device_id="ha-battery-1",
        metric="battery.soc",
        value=50.0,
        unit="%",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
    )
    capacity = BatteryCapacityEvidence(
        provider_id="home_assistant",
        device_id="ha-battery-2",
        capacity_kwh=8.0,
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_soc_observation_from_percentage_measurement(measurement, capacity)

    assert raised.value.diagnostic.code == "invalid_battery_capacity"


def test_percentage_bridge_preserves_degraded_quality_for_dispatch_guard() -> None:
    measurement = Measurement(
        provider_id="battery_fixture",
        device_id="battery.home",
        metric="battery.soc",
        value=50.0,
        unit="%",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.STALE,
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )
    capacity = BatteryCapacityEvidence(
        provider_id="battery_fixture",
        device_id="battery.home",
        capacity_kwh=4.0,
    )

    observation = battery_soc_observation_from_percentage_measurement(measurement, capacity)

    assert observation.quality is MeasurementQuality.STALE
    assert observation.value_kwh == 2.0
    with pytest.raises(ValidationError, match="GOOD"):
        dispatchable_battery_state(observation=observation)


@pytest.mark.parametrize(
    ("metric", "unit", "value", "code"),
    [
        ("battery.power", "kWh", 2.0, "invalid_battery_soc_measurement"),
        ("battery.soc", "%", 50.0, "unsupported_battery_soc_unit"),
        ("battery.soc", "Wh", 2000.0, "unsupported_battery_soc_unit"),
        ("battery.soc", None, 2.0, "unsupported_battery_soc_unit"),
        ("battery.soc", "kWh", "2.0", "invalid_battery_soc_measurement"),
        ("battery.soc", "kWh", True, "invalid_battery_soc_measurement"),
        ("battery.soc", "kWh", float("nan"), "invalid_battery_soc_measurement"),
    ],
)
def test_measurement_bridge_rejects_ambiguous_soc_telemetry(
    metric: str,
    unit: str | None,
    value: object,
    code: str,
) -> None:
    measurement = Measurement.model_construct(
        provider_id="battery_fixture",
        device_id="battery.home",
        metric=metric,
        value=value,
        unit=unit,
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )

    with pytest.raises(EnergyProviderError) as raised:
        battery_soc_observation_from_measurement(measurement)

    assert raised.value.diagnostic.code == code
    assert raised.value.diagnostic.provider_id == "battery_fixture"


def test_measurement_bridge_preserves_non_good_quality_for_dispatch_guard() -> None:
    measurement = Measurement(
        provider_id="battery_fixture",
        device_id="battery.home",
        metric="battery.soc",
        value=2.0,
        unit="kWh",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        quality=MeasurementQuality.STALE,
        source_ref=SourceRef(adapter_id="battery_fixture", external_id="battery_entity"),
    )

    observation = battery_soc_observation_from_measurement(measurement)

    assert observation.quality is MeasurementQuality.STALE
    with pytest.raises(ValidationError, match="GOOD"):
        dispatchable_battery_state(observation=observation)


def test_provider_failure_is_sanitized() -> None:
    tariffs, solar, battery = provider_inputs()

    class FailingTariffs:
        provider_id = "tariff_live"

        def get_tariffs(self, _horizon: object) -> TariffSeries:
            raise RuntimeError("access_token=do-not-leak")

    composed = ComposedEnergyContextProvider(
        FailingTariffs(),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == "provider_invalid"
    assert diagnostic.provider_id == "tariff_live"
    assert "do-not-leak" not in str(diagnostic)


def test_negative_market_tariffs_are_valid() -> None:
    context = energy_context_for()
    points = [
        TariffPoint(slot=0, price_per_kwh=-0.001, currency="EUR"),
        *context.tariffs[1:],
    ]
    series = TariffSeries(
        horizon=context.horizon,
        source_id="omie_fixture",
        source_revision="quarter-hour-1",
        observed_at=NOW,
        points=points,
    )

    assert series.points[0].price_per_kwh == -0.001


def test_provider_models_are_strict_and_validate_slots() -> None:
    context = energy_context_for()
    with pytest.raises(ValidationError):
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="one",
            observed_at=NOW,
            points=context.tariffs[:-1],
            unexpected="rejected",
        )

    with pytest.raises(ValidationError, match="ordered"):
        SolarForecastSeries(
            horizon=context.horizon,
            source_id="solar_fixture",
            source_revision="one",
            observed_at=NOW,
            points=[
                context.solar_forecast[1],
                context.solar_forecast[0],
                *context.solar_forecast[2:],
            ],
        )


def test_static_component_providers_are_read_only() -> None:
    tariffs, solar, battery = provider_inputs()

    assert not hasattr(StaticTariffProvider(tariffs), "execute")
    assert not hasattr(StaticSolarForecastProvider(solar), "execute")
    assert not hasattr(StaticBatteryProvider(battery), "execute")


def _exterior_temperature_series() -> ExteriorTemperatureSeries:
    context = energy_context_for()
    return ExteriorTemperatureSeries(
        horizon=context.horizon,
        source_id="exterior_temperature_fixture",
        source_revision="forecast-1",
        observed_at=NOW,
        points=[
            ExteriorTemperaturePoint(slot=slot, temperature_c=5.0)
            for slot in range(context.horizon.slots)
        ],
    )


def test_static_exterior_temperature_provider_is_read_only() -> None:
    series = _exterior_temperature_series()
    provider = StaticExteriorTemperatureProvider(series)

    assert provider.provider_id == "exterior_temperature_fixture"
    assert provider.get_forecast(series.horizon) is series
    assert not hasattr(provider, "execute")


def test_static_exterior_temperature_provider_rejects_a_different_horizon() -> None:
    series = _exterior_temperature_series()
    provider = StaticExteriorTemperatureProvider(series)
    different = energy_horizon(slots=series.horizon.slots + 1)

    with pytest.raises(EnergyProviderError, match="horizon"):
        provider.get_forecast(different)


@pytest.mark.asyncio
async def test_composer_exterior_temperature_default_absent_is_non_regression() -> None:
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs), StaticSolarForecastProvider(solar), now=lambda: NOW
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.exterior_temperature_forecast is None
    assert context.thermal is None


@pytest.mark.asyncio
async def test_composer_populates_exterior_temperature_forecast_from_provider() -> None:
    tariffs, solar, _ = provider_inputs()
    exterior_provider = StaticExteriorTemperatureProvider(_exterior_temperature_series())
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        exterior_temperature=exterior_provider,
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.exterior_temperature_forecast is not None
    assert len(context.exterior_temperature_forecast) == context.horizon.slots
    assert context.exterior_temperature_forecast[0].temperature_c == 5.0
    assert "exterior_temperature:exterior_temperature_fixture@" in context.source_revision
