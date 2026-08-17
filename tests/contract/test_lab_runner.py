from domoai.lab.runner import FIXTURE_SMOKE_TESTS


def test_fixture_smoke_covers_all_protocol_boundaries_without_live_modules() -> None:
    selected = set(FIXTURE_SMOKE_TESTS)

    assert "tests/integration/test_home_assistant_provider_runtime.py" in selected
    assert "tests/integration/test_zigbee2mqtt_fixture.py" in selected
    assert "tests/integration/test_modbus_fixture.py" in selected
    assert "tests/integration/test_matter_server_fixture.py" in selected
    assert "tests/integration/test_knx_fixture.py" in selected
    assert not any(path.endswith("_smoke.py") for path in selected)
