from domoai.domain.models import RiskClass
from domoai.runtime.events import AuditLog


def test_audit_redacts_credentials_and_preserves_safe_action_context() -> None:
    audit = AuditLog()

    event = audit.append(
        event_type="sensitive_action_requested",
        actor="agent",
        subject_id="front-door",
        payload={
            "risk_class": RiskClass.CONFIRM.value,
            "authorization": "Bearer secret-token",
            "token": "secret-token",
            "device": "front-door",
        },
    )

    assert event.payload["authorization"] == "[REDACTED]"
    assert event.payload["token"] == "[REDACTED]"
    assert event.payload["device"] == "front-door"
    assert event.payload["risk_class"] == "confirm"
