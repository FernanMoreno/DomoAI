"""Deterministic energy-domain fixtures used by unit and integration tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from domoai.optimizer.energy import (
    BatteryProfile,
    EnergyContext,
    SolarForecastPoint,
    StaticEnergyContextProvider,
    TariffPoint,
)
from domoai.optimizer.open_meteo import OpenMeteoSolarConfig
from domoai.optimizer.scenario import Horizon, Load


def energy_horizon(*, slots: int = 8, resolution_minutes: int = 15) -> Horizon:
    start = datetime(2026, 8, 15, tzinfo=UTC)
    return Horizon(
        start=start,
        end=start + timedelta(minutes=slots * resolution_minutes),
        resolution_minutes=resolution_minutes,
        timezone="Europe/Madrid",
    )


def omie_horizon(*, session_date: date = date(2026, 8, 16)) -> Horizon:
    timezone = ZoneInfo("Europe/Madrid")
    start = datetime.combine(session_date, time.min, tzinfo=timezone)
    end = datetime.combine(session_date + timedelta(days=1), time.min, tzinfo=timezone)
    return Horizon(
        start=start,
        end=end,
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )


def omie_file_payload(
    *,
    session_date: date = date(2026, 8, 16),
    rows: int = 96,
    spain_price: float = 100.0,
    portugal_price: float = 90.0,
) -> bytes:
    lines = ["MARGINALPDBC;"]
    for period in range(1, rows + 1):
        lines.append(
            f"{session_date.year};{session_date.month:02d};{session_date.day:02d};"
            f"{period};{portugal_price:.2f};{spain_price:.2f};"
        )
    lines.append("*")
    return ("\n".join(lines) + "\n").encode()


def open_meteo_config(
    *,
    timezone: str = "Europe/Madrid",
    inverter_ac_max_kw: float | None = None,
) -> OpenMeteoSolarConfig:
    return OpenMeteoSolarConfig(
        latitude=40.4168,
        longitude=-3.7038,
        installed_kwp=6,
        tilt=30,
        azimuth=0,
        performance_ratio=0.82,
        inverter_ac_max_kw=inverter_ac_max_kw,
        timezone=timezone,
    )


def open_meteo_payload(
    horizon: Horizon,
    *,
    irradiance_wm2: float = 700,
    timezone: str | None = None,
) -> dict[str, object]:
    start = horizon.start.astimezone(UTC)
    timestamps = [
        int((start + timedelta(minutes=15 * slot)).timestamp())
        for slot in range(horizon.slots)
    ]
    return {
        "timezone": timezone or horizon.timezone,
        "minutely_15": {
            "time": timestamps,
            "global_tilted_irradiance": [irradiance_wm2] * horizon.slots,
        },
    }


def energy_context_for(
    horizon: Horizon | None = None,
    *,
    with_battery: bool = True,
    source_revision: str = "fixture-energy-1",
) -> EnergyContext:
    selected_horizon = horizon or energy_horizon()
    slots = selected_horizon.slots
    return EnergyContext(
        horizon=selected_horizon,
        tariffs=[
            TariffPoint(
                slot=slot,
                price_per_kwh=0.08 if slot < slots // 2 else 0.32,
                currency="EUR",
            )
            for slot in range(slots)
        ],
        solar_forecast=[
            SolarForecastPoint(
                slot=slot,
                power=1.5 if slots // 3 <= slot < (2 * slots) // 3 else 0.0,
            )
            for slot in range(slots)
        ],
        battery=(
            BatteryProfile(
                capacity_kwh=6,
                initial_soc_kwh=2,
                min_soc_kwh=1,
                max_soc_kwh=6,
                max_charge_kw=3,
                max_discharge_kw=3,
                charge_efficiency=0.95,
                discharge_efficiency=0.9,
            )
            if with_battery
            else None
        ),
        source_revision=source_revision,
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )


def static_energy_provider(
    horizon: Horizon | None = None, *, with_battery: bool = True
) -> StaticEnergyContextProvider:
    return StaticEnergyContextProvider(
        energy_context_for(horizon, with_battery=with_battery)
    )


def flexible_load(
    device_id: str,
    *,
    load_id: str = "load-1",
    power_kw: float = 1.0,
    earliest_slot: int = 0,
    latest_slot: int = 3,
    duration_slots: int = 1,
) -> Load:
    return Load(
        id=load_id,
        device_id=device_id,
        capability="power",
        command="turn_on",
        value=True,
        power=power_kw,
        power_unit="kW",
        duration_slots=duration_slots,
        earliest_slot=earliest_slot,
        latest_slot=latest_slot,
    )
