from __future__ import annotations

import json

import pytest

from domoai.runtime.events import AuditLog, redact_payload


@pytest.mark.composition
def test_nested_audit_and_exception_payloads_share_defensive_redaction() -> None:
    payload = {
        "operator_token": "operator-secret",
        "mqtt-password": "mqtt-secret",
        "client_secret": "client-secret",
        "private_key": "private-secret",
        "safe_key": "keep-me",
        "public_key": "public-value",
        "nested": [
            {"url": "https://example.test/hook?access_token=url-secret&ok=1"},
            RuntimeError("provider failed token=exception-secret"),
        ],
    }

    event = AuditLog().append(
        event_type="provider_failure",
        actor="runtime",
        subject_id="provider",
        payload=payload,
    )
    serialized = json.dumps(event.payload, sort_keys=True)

    assert "operator-secret" not in serialized
    assert "mqtt-secret" not in serialized
    assert "client-secret" not in serialized
    assert "private-secret" not in serialized
    assert "url-secret" not in serialized
    assert "exception-secret" not in serialized
    assert event.payload["safe_key"] == "keep-me"
    assert event.payload["public_key"] == "public-value"
    assert redact_payload({"operator_token": "x"})["operator_token"] == "[REDACTED]"
