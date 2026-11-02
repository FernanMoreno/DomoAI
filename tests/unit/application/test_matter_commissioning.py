import asyncio

import pytest

from domoai.application.matter_commissioning import (
    InMemoryMatterCommissioningController,
    MatterCommissioningService,
)
from domoai.domain.p1_completion import CommissioningMethod, CommissioningState


@pytest.mark.asyncio
async def test_commissioning_requires_authority_and_confirmation_then_cleans_up() -> None:
    controller = InMemoryMatterCommissioningController(result=42)
    service = MatterCommissioningService(controller, timeout=0.2)

    session = await service.start(
        "matter:home",
        method=CommissioningMethod.MANUAL,
        code="12345678",
        can_commission=True,
        prerequisites={"bluetooth_available": True, "wifi_credentials": True},
    )
    assert session.state is CommissioningState.AWAITING_CONFIRMATION

    result = await service.confirm(session.session_id)

    assert result.state is CommissioningState.COMPLETED
    assert result.cleanup_done is True
    assert controller.commission_calls == [(CommissioningMethod.MANUAL, "12345678")]
    assert controller.cleanup_calls == [session.session_id]


@pytest.mark.asyncio
async def test_commissioning_timeout_is_expired_and_cleanup_is_idempotent() -> None:
    controller = InMemoryMatterCommissioningController(delay=0.2)
    service = MatterCommissioningService(controller, timeout=0.01)
    session = await service.start(
        "matter:home",
        method=CommissioningMethod.QR,
        code="MT:Y.K9042C00KA0648G00",
        can_commission=True,
        prerequisites={"bluetooth_available": True, "thread_credentials": True},
    )

    result = await service.confirm(session.session_id)
    await service.cleanup(session.session_id)

    assert result.state is CommissioningState.EXPIRED
    assert result.cleanup_done is True
    assert controller.cleanup_calls == [session.session_id]


@pytest.mark.asyncio
async def test_commissioning_rejects_authority_or_prerequisites_before_session() -> None:
    controller = InMemoryMatterCommissioningController()
    service = MatterCommissioningService(controller)

    with pytest.raises(PermissionError, match="authority"):
        await service.start(
            "matter:home",
            method=CommissioningMethod.QR,
            code="MT:VALID",
            can_commission=False,
            prerequisites={"bluetooth_available": True, "wifi_credentials": True},
        )
    with pytest.raises(ValueError, match="prerequisite"):
        await service.start(
            "matter:home",
            method=CommissioningMethod.QR,
            code="MT:VALID",
            can_commission=True,
            prerequisites={"bluetooth_available": False},
        )


class _Noop:  # keeps this test module's async scheduler import exercised
    async def run(self) -> None:
        await asyncio.sleep(0)
