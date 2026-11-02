from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import domoai.domain.connections as connection_domain
from domoai.domain.connections import ConnectionProfile


def _profile_kwargs() -> dict[str, object]:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    return {
        "id": uuid4(),
        "connector_id": "home_assistant",
        "unique_id": "home-assistant:living-room",
        "endpoint": {"host": "home-assistant.test", "port": 8123},
        "options": {"tls": True},
        "credential_ref": "env:DOMOAI_HOME_ASSISTANT_TOKEN",
        "mapping_ref": None,
        "created_at": now,
        "updated_at": now,
    }


def test_connection_profile_defaults_and_json_round_trip() -> None:
    profile = ConnectionProfile(**_profile_kwargs())

    assert profile.enabled is True
    assert profile.schema_version == "v1"
    assert profile.connector_id == "home_assistant"
    assert profile.credential_ref == "env:DOMOAI_HOME_ASSISTANT_TOKEN"

    restored = ConnectionProfile.model_validate(profile.model_dump(mode="json"))

    assert restored.id == profile.id
    assert restored.endpoint == profile.endpoint
    assert restored.model_dump(mode="json") == profile.model_dump(mode="json")


@pytest.mark.parametrize("field", ["connector_id", "unique_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_connection_profile_rejects_blank_required_identifiers(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        ConnectionProfile(**{**_profile_kwargs(), field: value})


def test_connection_profile_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_field"):
        ConnectionProfile(**{**_profile_kwargs(), "extra_field": "forbidden"})


@pytest.mark.parametrize("sensitive_key", ["token", "password", "api_key", "private_key"])
def test_connection_profile_rejects_sensitive_metadata_without_echoing_secret(
    sensitive_key: str,
) -> None:
    secret_value = "fixture-secret-value-must-not-leak"

    with pytest.raises(ValidationError) as error:
        ConnectionProfile(
            **{
                **_profile_kwargs(),
                "options": {sensitive_key: secret_value},
            }
        )

    assert "sensitive" in str(error.value).casefold()
    assert secret_value not in str(error.value)


def test_connection_profile_accepts_opaque_credential_reference_only() -> None:
    profile = ConnectionProfile(
        **{
            **_profile_kwargs(),
            "credential_ref": "secret:0191b8e0-7f2f-7b89-9e8a-1c7d0b47d3f1",
        }
    )

    assert isinstance(profile.id, UUID)
    assert profile.credential_ref is not None


def test_connection_profile_rejects_embedded_url_credentials() -> None:
    with pytest.raises(ValidationError):
        ConnectionProfile(
            **{
                **_profile_kwargs(),
                "endpoint": {"url": "mqtt://user:password@broker.local"},
            }
        )


def _contract_type(name: str) -> type[object]:
    contract = getattr(connection_domain, name, None)
    assert contract is not None, f"missing connection contract: {name}"
    return contract


def _diagnostic_kwargs(code: str = "authentication_required") -> dict[str, object]:
    return {
        "code": code,
        "severity": "error",
        "retryable": False,
        "user_action": "Provide a valid credential reference",
        "message": "The connection requires authentication",
        "technical_details": {"status_code": 401},
    }


def test_connection_status_enums_serialize_with_stable_values() -> None:
    transport_status = _contract_type("TransportStatus")
    inventory_status = _contract_type("InventoryStatus")
    product_status = _contract_type("ProductStatus")

    assert transport_status.CONNECTED.value == "connected"
    assert inventory_status.MAPPING_REQUIRED.value == "mapping_required"
    assert product_status.NEEDS_SETUP.value == "needs_setup"


def test_probe_result_preserves_independent_health_dimensions() -> None:
    probe_result = _contract_type("ProbeResult")

    result = probe_result(
        transport_status="connected",
        inventory_status="mapping_required",
        product_status="needs_setup",
    )

    assert result.transport_status.value == "connected"
    assert result.inventory_status.value == "mapping_required"
    assert result.product_status.value == "needs_setup"
    assert result.model_dump(mode="json")["product_status"] == "needs_setup"


def test_connection_diagnostic_is_typed_and_secret_safe() -> None:
    diagnostic = _contract_type("ConnectionDiagnostic")

    result = diagnostic(**_diagnostic_kwargs())

    assert result.code == "authentication_required"
    assert result.retryable is False
    assert result.technical_details == {"status_code": 401}

    secret_value = "fixture-diagnostic-token-must-not-leak"
    with pytest.raises(ValidationError) as error:
        diagnostic(
            **{
                **_diagnostic_kwargs(),
                "message": f"authentication failed token={secret_value}",
            }
        )

    assert secret_value not in str(error.value)


def test_diagnostic_result_rejects_empty_and_duplicate_codes() -> None:
    diagnostic = _contract_type("ConnectionDiagnostic")
    probe_result = _contract_type("ProbeResult")

    with pytest.raises(ValidationError, match="code"):
        diagnostic(**{**_diagnostic_kwargs(), "code": "   "})

    first = diagnostic(**_diagnostic_kwargs("auth_failed"))
    second = diagnostic(**_diagnostic_kwargs("auth_failed"))
    with pytest.raises(ValidationError, match="unique|duplicate"):
        probe_result(
            transport_status="auth_failed",
            inventory_status="unknown",
            product_status="reauth_required",
            diagnostics=[first, second],
        )


def test_validation_result_preserves_warnings_and_ordered_diagnostics() -> None:
    diagnostic = _contract_type("ConnectionDiagnostic")
    validation_result = _contract_type("ValidationResult")

    warnings = [
        diagnostic(
            **{
                **_diagnostic_kwargs("mapping_required"),
                "severity": "warning",
                "retryable": True,
            }
        ),
        diagnostic(
            **{
                **_diagnostic_kwargs("no_devices_found"),
                "severity": "info",
            }
        ),
    ]
    result = validation_result(valid=True, diagnostics=warnings)

    assert result.valid is True
    assert [item.code for item in result.diagnostics] == [
        "mapping_required",
        "no_devices_found",
    ]


def test_validation_result_can_carry_a_normalized_connection_draft() -> None:
    draft = _contract_type("ConnectionDraft")
    validation_result = _contract_type("ValidationResult")

    normalized = draft(
        connector_id="home_assistant",
        unique_id="home-assistant:living-room",
        endpoint={"host": "home-assistant.test", "port": 8123},
        options={"tls": True},
        credential_ref="env:DOMOAI_HOME_ASSISTANT_TOKEN",
    )
    result = validation_result(valid=True, normalized_draft=normalized)

    assert result.normalized_draft is not None
    assert result.normalized_draft.unique_id == "home-assistant:living-room"


def test_connection_candidate_keeps_discovery_metadata_secret_safe() -> None:
    candidate = _contract_type("ConnectionCandidate")

    result = candidate(
        connector_id="home_assistant",
        candidate_id="ha:home",
        display_name="Home Assistant",
        endpoint={"host": "home-assistant.test", "port": 8123},
        metadata={"source": "mdns", "tls": True},
    )

    assert result.candidate_id == "ha:home"
    assert result.metadata["source"] == "mdns"

    secret_value = "fixture-candidate-token-must-not-leak"
    with pytest.raises(ValidationError) as error:
        candidate(
            connector_id="home_assistant",
            candidate_id="ha:home",
            display_name="Home Assistant",
            endpoint={"host": "home-assistant.test"},
            metadata={"token": secret_value},
        )

    assert secret_value not in str(error.value)


def test_setup_step_requires_secret_input_names_to_be_declared_inputs() -> None:
    setup_step = _contract_type("SetupStep")

    step = setup_step(
        step_id="credentials",
        title="Credentials",
        input_keys=("token", "endpoint"),
        secret_input_keys=("token",),
    )

    assert step.secret_input_keys == ("token",)

    with pytest.raises(ValidationError, match="input_keys"):
        setup_step(
            step_id="credentials",
            title="Credentials",
            input_keys=("endpoint",),
            secret_input_keys=("token",),
        )


def test_onboarding_session_and_transition_expose_explicit_next_step() -> None:
    session_type = _contract_type("OnboardingSession")
    transition_type = _contract_type("SetupTransition")
    session_status = _contract_type("OnboardingSessionStatus")
    transition_status = _contract_type("SetupTransitionStatus")
    setup_step = _contract_type("SetupStep")
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    session = session_type(
        id=uuid4(),
        connector_id="home_assistant",
        candidate_id="ha:home",
        status=session_status.ACTIVE,
        current_step_id="credentials",
        expires_at=datetime(2026, 9, 8, 12, 10, tzinfo=UTC),
        created_at=now,
        updated_at=now,
    )
    step = setup_step(
        step_id="credentials",
        title="Credentials",
        input_keys=("token",),
        secret_input_keys=("token",),
    )
    transition = transition_type(
        session_id=session.id,
        status=transition_status.CONTINUE,
        next_step=step,
    )

    assert session.status.value == "active"
    assert transition.status.value == "continue"
    assert transition.next_step is not None
    assert transition.next_step.step_id == "credentials"


def test_setup_transition_rejects_unknown_fields() -> None:
    transition_type = _contract_type("SetupTransition")

    with pytest.raises(ValidationError, match="extra_field"):
        transition_type(
            session_id=uuid4(),
            status="failed",
            extra_field="forbidden",
        )
