from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.domain.p1_completion import (
    CommissioningMethod,
    CommissioningState,
    MatterCommissioningSession,
    ModbusDeviceIdentity,
    ModbusProfileResolution,
    ProfileResolutionStatus,
)


def test_commissioning_session_requires_explicit_confirmation_before_running() -> None:
    now = datetime.now(UTC)
    session = MatterCommissioningSession(
        session_id="commissioning-1",
        connection_id="matter:home",
        method=CommissioningMethod.QR,
        state=CommissioningState.AWAITING_CONFIRMATION,
        deadline=now + timedelta(seconds=30),
        confirmation_required=True,
    )

    assert session.state is CommissioningState.AWAITING_CONFIRMATION
    assert session.confirmation_required is True


def test_commissioning_session_rejects_deadline_before_creation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="deadline"):
        MatterCommissioningSession(
            session_id="commissioning-1",
            connection_id="matter:home",
            method=CommissioningMethod.MANUAL,
            state=CommissioningState.CREATED,
            deadline=now - timedelta(seconds=1),
            created_at=now,
        )


def test_modbus_identity_requires_bounded_unit_id_and_profile_resolution_is_explicit() -> None:
    identity = ModbusDeviceIdentity(
        host_ref="modbus:controller",
        unit_id=1,
        vendor_name="Acme",
        product_code="meter-1",
        revision="1.2",
        complete=True,
    )
    result = ModbusProfileResolution(
        status=ProfileResolutionStatus.PROFILE_REQUIRED,
        identity=identity,
    )

    assert result.status is ProfileResolutionStatus.PROFILE_REQUIRED
    with pytest.raises(ValidationError):
        ModbusDeviceIdentity(
            host_ref="modbus:controller",
            unit_id=0,
            vendor_name="Acme",
            product_code="meter-1",
            revision="1.2",
            complete=True,
        )
