from datetime import date

import httpx
import pytest

from domoai.optimizer.omie import (
    OmieTariffHttpClient,
    OmieTariffProvider,
    parse_omie_marginal_prices,
)
from domoai.optimizer.providers import EnergyProviderError
from tests.fixtures.energy import omie_file_payload, omie_horizon


def test_http_client_uses_public_file_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=omie_file_payload())

    client = OmieTariffHttpClient(transport=httpx.MockTransport(handler))
    try:
        downloaded = client.fetch_day(date(2026, 8, 16))
    finally:
        client.close()

    assert str(requests[0].url).startswith("https://www.omie.es/en/file-download?")
    assert requests[0].url.params["parents"] == "marginalpdbc"
    assert requests[0].url.params["filename"] == "marginalpdbc_20260816.1"
    assert downloaded.source_revision == "marginalpdbc_20260816.1"
    assert downloaded.body.startswith(b"MARGINALPDBC;")


@pytest.mark.parametrize(
    "body",
    [
        b"BAD;\n*\n",
        omie_file_payload(rows=95),
        omie_file_payload().replace(b";1;90.00;100.00;", b";1;90.00;not-a-number;"),
        omie_file_payload().replace(b";2;90.00;100.00;", b";1;90.00;100.00;", 1),
    ],
)
def test_parser_and_provider_reject_malformed_files(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_omie_marginal_prices(body, session_date=omie_horizon().start.date())

    class Client:
        def fetch_day(self, _session_date):
            from datetime import UTC, datetime

            from domoai.optimizer.omie import OmieDayAheadFile

            return OmieDayAheadFile(
                body=body,
                source_revision="marginalpdbc_20260816.1",
                observed_at=datetime(2026, 8, 16, 14, tzinfo=UTC),
            )

    with pytest.raises(EnergyProviderError) as raised:
        OmieTariffProvider(Client()).get_tariffs(omie_horizon())
    assert raised.value.diagnostic.code == "provider_invalid"
    assert "not-a-number" not in str(raised.value.diagnostic)


def test_provider_rejects_unsupported_horizon_before_client_call() -> None:
    called = False

    class Client:
        def fetch_day(self, _session_date):
            nonlocal called
            called = True
            raise AssertionError("network must not be called")

    horizon = omie_horizon().model_copy(update={"resolution_minutes": 30})
    with pytest.raises(EnergyProviderError) as raised:
        OmieTariffProvider(Client()).get_tariffs(horizon)

    assert raised.value.diagnostic.code == "unsupported_horizon"
    assert called is False


def test_provider_rejects_unsafe_revision() -> None:
    from datetime import UTC, datetime

    from domoai.optimizer.omie import OmieDayAheadFile

    class Client:
        def fetch_day(self, _session_date):
            return OmieDayAheadFile(
                body=omie_file_payload(),
                source_revision="marginalpdbc?token=secret",
                observed_at=datetime(2026, 8, 16, 14, tzinfo=UTC),
            )

    with pytest.raises(EnergyProviderError) as raised:
        OmieTariffProvider(Client()).get_tariffs(omie_horizon())
    assert raised.value.diagnostic.code == "provider_invalid"
    assert "secret" not in str(raised.value.diagnostic)
