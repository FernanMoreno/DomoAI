from __future__ import annotations

from domoai.application.knx_secure import knx_secure_diagnostic


def test_knx_secure_diagnostic_is_sanitized_and_actionable() -> None:
    diagnostic = knx_secure_diagnostic(ValueError("password=very-secret key=deadbeef"))

    assert diagnostic.code == "knx_secure_configuration_invalid"
    assert "very-secret" not in diagnostic.message
    assert "deadbeef" not in diagnostic.model_dump_json()
    assert diagnostic.retryable is False
