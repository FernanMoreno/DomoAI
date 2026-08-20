from datetime import UTC, date, datetime

import httpx
import pytest

from domoai.optimizer.omie import (
    OmieDayAheadFile,
    OmieTariffHttpClient,
    OmieTariffProvider,
    parse_omie_marginal_prices,
)
from domoai.optimizer.providers import EnergyProviderError
from tests.fixtures.energy import omie_file_payload, omie_horizon


class FixtureOmieClient:
    def __init__(self, body: bytes, *, revision: str = "marginalpdbc_20260816.1") -> None:
        self.body = body
        self.revision = revision
        self.requested_date = None

    def fetch_day(self, session_date):
        self.requested_date = session_date
        return OmieDayAheadFile(
            body=self.body,
            source_revision=self.revision,
            observed_at=datetime(2026, 8, 16, 14, tzinfo=UTC),
        )


def test_parser_converts_spanish_eur_per_mwh_to_eur_per_kwh() -> None:
    body = omie_file_payload(spain_price=-125.0, portugal_price=80.0)

    prices = parse_omie_marginal_prices(
        body,
        session_date=omie_horizon().start.date(),
    )

    assert len(prices) == 96
    assert prices[1] == -0.125
    assert prices[96] == -0.125


def test_provider_returns_canonical_series_and_preserves_provenance() -> None:
    client = FixtureOmieClient(omie_file_payload(spain_price=42.5))
    provider = OmieTariffProvider(client)

    series = provider.get_tariffs(omie_horizon())

    assert client.requested_date == omie_horizon().start.date()
    assert series.source_id == "omie_spain"
    assert series.source_revision == "marginalpdbc_20260816.1"
    assert series.observed_at.tzinfo is not None
    assert series.points[0].slot == 0
    assert series.points[0].price_per_kwh == 0.0425
    assert series.points[-1].slot == 95


def test_provider_sanitizes_client_failures() -> None:
    class FailingClient:
        def fetch_day(self, _session_date):
            raise ConnectionError("https://secret.example/token=leak")

    with pytest.raises(EnergyProviderError) as raised:
        OmieTariffProvider(FailingClient()).get_tariffs(omie_horizon())

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == "provider_unavailable"
    assert diagnostic.retryable is True
    assert "secret.example" not in str(diagnostic)


@pytest.mark.parametrize(
    ("session_date", "expected_slots"),
    [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)],
)
def test_provider_accepts_dst_period_counts(session_date, expected_slots) -> None:
    horizon = omie_horizon(session_date=session_date)
    provider = OmieTariffProvider(
        FixtureOmieClient(omie_file_payload(session_date=session_date, rows=expected_slots))
    )

    series = provider.get_tariffs(horizon)

    assert horizon.slots == expected_slots
    assert len(series.points) == expected_slots


def test_http_file_version_is_configurable() -> None:
    client = OmieTariffHttpClient(
        file_version="2",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=omie_file_payload())
        ),
    )
    try:
        downloaded = client.fetch_day(date(2026, 8, 16))
    finally:
        client.close()

    assert downloaded.source_revision == "marginalpdbc_20260816.2"
