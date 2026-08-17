from domoai.runtime.events import redact_payload


def test_redaction_removes_credentials_recursively() -> None:
    payload = {
        "token": "secret",
        "nested": {"authorization": "Bearer secret", "value": 42},
        "items": [{"password": "secret"}],
    }

    assert redact_payload(payload) == {
        "token": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]", "value": 42},
        "items": [{"password": "[REDACTED]"}],
    }
