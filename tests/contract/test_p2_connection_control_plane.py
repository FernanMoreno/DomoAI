from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.knx.config import KnxMappingDocument
from domoai.domain.p2_connections import (
    EtsGroupAddress,
    EtsImportPreview,
    KnxSecureOptions,
    OAuthAuthorizationRequest,
    OAuthCredentialRecord,
)
from domoai.domain.repairs import RepairAction, RepairActionName, RepairPlan
from tests.fixtures.knx import mapping_payload


def test_ets_preview_requires_review_and_keeps_mapping_strict() -> None:
    mapping = KnxMappingDocument.model_validate(mapping_payload())
    preview = EtsImportPreview(
        source_digest="a" * 64,
        source_kind="xml",
        project_name="House",
        group_addresses=(
            EtsGroupAddress(
                address="1/0/1",
                dpt="1.001",
                name="Living Light",
                area="living_room",
                support="supported",
                capability="power",
            ),
        ),
        mapping=mapping,
        warnings=(),
    )

    assert preview.review_required is True
    assert preview.mapping == mapping
    assert preview.model_dump_json().find("secret") == -1


def test_secure_options_accept_only_opaque_credential_reference() -> None:
    options = KnxSecureOptions(
        mode="ip_and_data_secure",
        keyring_path="/run/secrets/home.knxkeys",
        interface="192.0.2.10",
        user_id=7,
        credential_ref="secret:12345678-1234-1234-1234-123456789012",
    )

    assert options.mode == "ip_and_data_secure"
    with pytest.raises(ValueError):
        KnxSecureOptions(
            mode="ip_secure",
            credential_ref="raw-password",
        )


def test_oauth_records_expose_reference_and_expiry_only() -> None:
    expires_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    request = OAuthAuthorizationRequest(
        state="state-1234567890",
        authorization_url="https://auth.example/authorize?state=state-1234567890",
        expires_at=expires_at,
    )
    record = OAuthCredentialRecord(
        provider="home_assistant",
        credential_ref="secret:12345678-1234-1234-1234-123456789012",
        expires_at=expires_at,
        scopes=("read",),
        refreshable=True,
    )

    assert "access_token" not in request.model_dump_json()
    assert "refresh_token" not in record.model_dump_json()


def test_repair_plan_rejects_credential_parameters_and_is_typed() -> None:
    action = RepairAction(
        action_id="retry-1",
        action="retry",
        parameters={},
        requires_confirmation=False,
        risk="safe",
        diagnostic_code="timeout",
        plan_digest="b" * 64,
    )
    plan = RepairPlan(
        connection_id="12345678-1234-1234-1234-123456789012",
        diagnostics_digest="c" * 64,
        actions=(action,),
        created_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
    )

    assert action.action is RepairActionName.RETRY
    assert plan.actions == (action,)
    with pytest.raises(ValueError):
        RepairAction(
            action_id="unsafe-1",
            action="retry",
            parameters={"credential": "must-not-enter"},
            requires_confirmation=False,
            risk="safe",
            diagnostic_code="timeout",
            plan_digest="b" * 64,
        )
