"""Read-only OMIE public day-ahead tariff provider.

This module owns only the public-file transport, parsing, unit conversion and
source provenance. It deliberately does not select solar/battery providers or
wire itself into the default runtime.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import httpx

from domoai.optimizer.energy import TariffPoint
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.providers import (
    EnergyProviderDiagnostic,
    EnergyProviderError,
    TariffSeries,
)

OMIE_BASE_URL = "https://www.omie.es"
OMIE_TIMEZONE = ZoneInfo("Europe/Madrid")
OMIE_SOURCE_ID = "omie_spain"
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True)
class OmieDayAheadFile:
    """Downloaded public file and the provenance needed by ``TariffSeries``."""

    body: bytes
    source_revision: str
    observed_at: datetime


class OmieTariffFileClient(Protocol):
    """Read-only source boundary used by ``OmieTariffProvider``."""

    def fetch_day(self, session_date: date) -> OmieDayAheadFile: ...


class OmieTariffHttpClient:
    """HTTP client for OMIE's public ``marginalpdbc`` download files."""

    def __init__(
        self,
        *,
        base_url: str = OMIE_BASE_URL,
        timeout: float = 10.0,
        file_version: str = "1",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9]+", file_version):
            raise ValueError("OMIE file_version must contain only letters and digits")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self._file_version = file_version

    def fetch_day(self, session_date: date) -> OmieDayAheadFile:
        filename = f"marginalpdbc_{session_date:%Y%m%d}.{self._file_version}"
        response = self._client.get(
            "/en/file-download",
            params={"parents": "marginalpdbc", "filename": filename},
        )
        response.raise_for_status()
        if not response.content:
            raise ValueError("OMIE returned an empty file")
        return OmieDayAheadFile(
            body=response.content,
            source_revision=filename,
            observed_at=datetime.now(UTC),
        )

    def close(self) -> None:
        self._client.close()


def parse_omie_marginal_prices(
    body: bytes,
    *,
    session_date: date,
    zone: Literal["ES", "PT"] = "ES",
) -> dict[int, float]:
    """Parse OMIE ``MARGINALPDBC`` rows into EUR/kWh by source period."""

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("OMIE file is not valid UTF-8") from error

    lines = text.splitlines()
    if not lines or lines[0].strip().rstrip(";") != "MARGINALPDBC":
        raise ValueError("OMIE file header is invalid")

    price_index = 5 if zone == "ES" else 4
    prices: dict[int, float] = {}
    for line_number, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            continue
        if line == "*":
            break
        fields = [field.strip() for field in line.split(";")]
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) != 6:
            raise ValueError(f"OMIE row {line_number} has an invalid field count")
        try:
            year, month, day, period = (int(value) for value in fields[:4])
            row_date = date(year, month, day)
        except (TypeError, ValueError) as error:
            raise ValueError(f"OMIE row {line_number} has an invalid date or period") from error
        if row_date != session_date or period < 1:
            raise ValueError(f"OMIE row {line_number} is outside the requested session")
        if period in prices:
            raise ValueError(f"OMIE period {period} is duplicated")
        try:
            price_eur_per_mwh = Decimal(fields[price_index].replace(",", "."))
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"OMIE row {line_number} has an invalid price") from error
        if not price_eur_per_mwh.is_finite():
            raise ValueError(f"OMIE row {line_number} has a non-finite price")
        price_eur_per_kwh = float(price_eur_per_mwh / Decimal("1000"))
        if not math.isfinite(price_eur_per_kwh):
            raise ValueError(f"OMIE row {line_number} has an unusable price")
        prices[period] = price_eur_per_kwh

    if not prices or sorted(prices) != list(range(1, max(prices) + 1)):
        raise ValueError("OMIE periods must be contiguous and non-empty")
    expected_periods = _expected_periods_for_date(session_date)
    if len(prices) != expected_periods:
        raise ValueError(
            f"OMIE file must contain exactly {expected_periods} periods for the session date"
        )
    return prices


def _expected_periods_for_date(session_date: date) -> int:
    timezone = OMIE_TIMEZONE
    start = datetime.combine(session_date, time.min, tzinfo=timezone)
    end = datetime.combine(session_date + timedelta(days=1), time.min, tzinfo=timezone)
    elapsed_seconds = (
        end.astimezone(UTC) - start.astimezone(UTC)
    ).total_seconds()
    periods = int(elapsed_seconds // (15 * 60))
    if periods not in {92, 96, 100}:
        raise ValueError("OMIE session date does not have 92, 96 or 100 periods")
    return periods


def _provider_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, str | int | float | bool] | None = None,
) -> EnergyProviderError:
    return EnergyProviderError(
        EnergyProviderDiagnostic(
            code=code,
            provider_id=OMIE_SOURCE_ID,
            message=message,
            retryable=retryable,
            details=details or {},
        )
    )


def _is_retryable_transport(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return isinstance(error, (httpx.RequestError, ConnectionError, TimeoutError, OSError))


class OmieTariffProvider:
    """Host-injected Spanish OMIE provider implementing ``TariffProvider``."""

    provider_id = OMIE_SOURCE_ID

    def __init__(
        self,
        client: OmieTariffFileClient,
    ) -> None:
        self.client = client

    def get_tariffs(self, horizon: Horizon) -> TariffSeries:
        session_date = self._validate_horizon(horizon)
        try:
            downloaded = self.client.fetch_day(session_date)
        except EnergyProviderError:
            raise
        except Exception as error:
            raise _provider_error(
                "provider_unavailable",
                "OMIE tariff source is unavailable",
                retryable=_is_retryable_transport(error),
            ) from error

        if not isinstance(downloaded, OmieDayAheadFile):
            raise _provider_error(
                "provider_invalid", "OMIE tariff client returned an invalid file"
            )
        if not isinstance(downloaded.source_revision, str) or not _SAFE_REVISION.fullmatch(
            downloaded.source_revision
        ):
            raise _provider_error(
                "provider_invalid", "OMIE tariff source revision is invalid"
            )
        if downloaded.observed_at.tzinfo is None:
            raise _provider_error(
                "provider_invalid", "OMIE tariff observation is not timezone-aware"
            )

        try:
            prices = parse_omie_marginal_prices(
                downloaded.body,
                session_date=session_date,
                zone="ES",
            )
            if len(prices) != horizon.slots:
                raise ValueError(
                    "OMIE file does not contain the expected number of periods"
                )
            points = [
                TariffPoint(
                    slot=slot,
                    price_per_kwh=prices[slot + 1],
                    currency="EUR",
                )
                for slot in range(horizon.slots)
            ]
            return TariffSeries(
                horizon=horizon,
                source_id=self.provider_id,
                source_revision=downloaded.source_revision,
                observed_at=downloaded.observed_at,
                points=points,
            )
        except EnergyProviderError:
            raise
        except (TypeError, ValueError) as error:
            raise _provider_error(
                "provider_invalid",
                "OMIE tariff file is invalid",
                details={"session_date": session_date.isoformat()},
            ) from error

    def _validate_horizon(self, horizon: Horizon) -> date:
        if horizon.timezone != "Europe/Madrid" or horizon.resolution_minutes != 15:
            raise _provider_error(
                "unsupported_horizon",
                "OMIE v1 requires a Europe/Madrid 15-minute horizon",
            )
        local_start = horizon.start.astimezone(OMIE_TIMEZONE)
        local_end = horizon.end.astimezone(OMIE_TIMEZONE)
        if (
            local_start.time() != time(0)
            or local_end.time() != time(0)
            or local_end.date() != local_start.date() + timedelta(days=1)
        ):
            raise _provider_error(
                "unsupported_horizon",
                "OMIE v1 requires a complete local calendar day",
            )
        elapsed_slots = (
            local_end.astimezone(UTC) - local_start.astimezone(UTC)
        ).total_seconds() / (15 * 60)
        expected_periods = _expected_periods_for_date(local_start.date())
        if horizon.slots != expected_periods or elapsed_slots != expected_periods:
            raise _provider_error(
                "unsupported_horizon",
                "OMIE horizon does not match its 92, 96 or 100-period local day",
            )
        return local_start.date()


__all__ = [
    "OMIE_BASE_URL",
    "OMIE_SOURCE_ID",
    "OmieDayAheadFile",
    "OmieTariffFileClient",
    "OmieTariffHttpClient",
    "OmieTariffProvider",
    "parse_omie_marginal_prices",
]
