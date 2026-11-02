"""Read-only Modbus Device Identification service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime

from domoai.adapters.modbus.transport import ModbusTransport
from domoai.domain.p1_completion import ModbusDeviceIdentity


class ModbusIdentificationService:
    """Probe only the explicitly configured, small set of unit IDs."""

    def __init__(
        self,
        transport: ModbusTransport,
        *,
        unit_ids: Iterable[int] = (1,),
        timeout: float = 5.0,
    ) -> None:
        normalized = tuple(unit_ids)
        if not normalized or len(normalized) > 16:
            raise ValueError("Modbus unit-id policy must be bounded")
        if any(
            not isinstance(unit, int)
            or isinstance(unit, bool)
            or not 1 <= unit <= 247
            for unit in normalized
        ):
            raise ValueError("Modbus unit-id policy contains an invalid unit")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Modbus unit-id policy must contain unique units")
        if timeout <= 0:
            raise ValueError("Modbus identification timeout must be positive")
        self.transport = transport
        self.unit_ids = normalized
        self.timeout = timeout

    async def identify(self, host_ref: str) -> ModbusDeviceIdentity:
        first_partial: ModbusDeviceIdentity | None = None
        for unit_id in self.unit_ids:
            try:
                result = await asyncio.wait_for(
                    self.transport.identify_device(unit_id), self.timeout
                )
            except (ConnectionError, OSError, TimeoutError, ValueError):
                continue
            if result is None:
                continue
            identity = ModbusDeviceIdentity(
                host_ref=host_ref,
                unit_id=unit_id,
                vendor_name=self._safe_text(result.vendor_name),
                product_code=self._safe_text(result.product_code),
                revision=self._safe_text(result.revision),
                read_device_id_code=result.read_code,
                complete=bool(result.vendor_name and result.product_code and result.revision),
                observed_at=datetime.now(UTC),
            )
            if identity.complete:
                return identity
            if first_partial is None:
                first_partial = identity
        return first_partial or ModbusDeviceIdentity(host_ref=host_ref, unit_id=self.unit_ids[0])

    @staticmethod
    def _safe_text(value: str) -> str:
        normalized = str(value).strip()
        if len(normalized) > 128:
            normalized = normalized[:128]
        if any(token in normalized.casefold() for token in ("password", "token", "secret")):
            return ""
        return normalized


__all__ = ["ModbusIdentificationService"]
