from pathlib import Path

import pytest

from domoai.adapters.home_assistant.config import (
    HomeAssistantMappingConfigurationError,
    load_metric_mappings,
)


def test_load_metric_mappings_accepts_strict_v1_document(tmp_path: Path) -> None:
    path = tmp_path / "home-assistant-mappings.json"
    path.write_text(
        '{"schema_version":"v1","metric_mappings":'
        '{"sensor.pv_power":{"power":"energy.pv.power"}}}',
        encoding="utf-8",
    )

    assert load_metric_mappings(path) == {
        "sensor.pv_power": {"power": "energy.pv.power"}
    }


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"schema_version":"v2","metric_mappings":{}}',
        '{"schema_version":"v1","metric_mappings":{},"token":"secret"}',
        '{"schema_version":"v1","metric_mappings":{"sensor.x":{"power":""}}}',
    ],
)
def test_load_metric_mappings_rejects_invalid_or_sensitive_documents(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(HomeAssistantMappingConfigurationError):
        load_metric_mappings(path)
