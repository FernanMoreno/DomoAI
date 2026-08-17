"""Opt-in read-only smoke against the public OMIE file service."""

import os
from datetime import datetime

import pytest

from domoai.optimizer.omie import OMIE_TIMEZONE, OmieTariffHttpClient, OmieTariffProvider
from tests.fixtures.energy import omie_horizon


def test_live_omie_smoke_is_opt_in_and_supports_dst_period_counts() -> None:
    if os.getenv("DOMOAI_OMIE_LIVE") != "1":
        pytest.skip("Set DOMOAI_OMIE_LIVE=1 to run the public OMIE smoke")

    session_date = datetime.now(OMIE_TIMEZONE).date()
    horizon = omie_horizon(session_date=session_date)
    client = OmieTariffHttpClient()
    try:
        series = OmieTariffProvider(client).get_tariffs(horizon)
    finally:
        client.close()

    assert len(series.points) == horizon.slots
    assert horizon.slots in {92, 96, 100}
    assert series.source_id == "omie_spain"
    assert series.source_revision
    assert all(point.currency == "EUR" for point in series.points)
