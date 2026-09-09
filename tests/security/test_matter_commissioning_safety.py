import pytest

from domoai.application.matter_commissioning import (
    InMemoryMatterCommissioningController,
    MatterCommissioningService,
)
from domoai.domain.p1_completion import CommissioningMethod


@pytest.mark.asyncio
async def test_only_one_commissioning_session_per_connection_and_invalid_code_is_rejected() -> None:
    controller = InMemoryMatterCommissioningController()
    service = MatterCommissioningService(controller, timeout=0.2)
    kwargs = {
        "method": CommissioningMethod.MANUAL,
        "code": "12345678",
        "can_commission": True,
        "prerequisites": {"bluetooth_available": True, "wifi_credentials": True},
    }
    await service.start("matter:home", **kwargs)
    with pytest.raises(RuntimeError, match="active"):
        await service.start("matter:home", **kwargs)
    with pytest.raises(ValueError, match="code"):
        await service.start(
            "matter:other",
            method=CommissioningMethod.MANUAL,
            code="short",
            can_commission=True,
            prerequisites={"bluetooth_available": True, "wifi_credentials": True},
        )
