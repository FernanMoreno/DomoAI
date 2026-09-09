"""Deterministic local Modbus profile catalog."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.domain.p1_completion import (
    ModbusDeviceIdentity,
    ModbusProfileResolution,
    ProfileResolutionStatus,
)


class ModbusProfileCatalog:
    def __init__(self, profiles: Iterable[ModbusProfile] = ()) -> None:
        values = tuple(profiles)
        profile_ids = [profile.profile_id for profile in values]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("Modbus profile IDs must be unique")
        self.profiles = tuple(sorted(values, key=lambda profile: profile.profile_id))

    @classmethod
    def from_directory(cls, directory: Path) -> ModbusProfileCatalog:
        profiles: list[ModbusProfile] = []
        if not directory.is_dir():
            return cls()
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                profiles.append(ModbusProfile.model_validate(payload))
            except (OSError, json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"invalid Modbus profile {path.name}") from error
        return cls(profiles)

    def resolve(self, identity: ModbusDeviceIdentity) -> ModbusProfileResolution:
        if not identity.complete:
            return ModbusProfileResolution(
                status=ProfileResolutionStatus.PROFILE_REQUIRED,
                identity=identity,
                diagnostic="complete Device Identification is required",
            )
        same_product = tuple(
            profile
            for profile in self.profiles
            if profile.vendor_name.casefold() == identity.vendor_name.casefold()
            and profile.product_code.casefold() == identity.product_code.casefold()
        )
        if not same_product:
            return ModbusProfileResolution(
                status=ProfileResolutionStatus.PROFILE_REQUIRED,
                identity=identity,
                diagnostic="no Modbus profile matches this device identity",
            )
        compatible = tuple(
            profile for profile in same_product if profile.supports_firmware(identity.revision)
        )
        if len(compatible) == 1:
            return ModbusProfileResolution(
                status=ProfileResolutionStatus.MATCHED,
                identity=identity,
                profile_id=compatible[0].profile_id,
                candidate_profile_ids=tuple(profile.profile_id for profile in same_product),
            )
        status = ProfileResolutionStatus.INCOMPATIBLE
        reason = (
            "device firmware is outside all compatible profile ranges"
            if not compatible
            else "multiple Modbus profiles match this device"
        )
        return ModbusProfileResolution(
            status=status,
            identity=identity,
            candidate_profile_ids=tuple(profile.profile_id for profile in same_product),
            diagnostic=reason,
        )

    def get(self, profile_id: str) -> ModbusProfile | None:
        return next(
            (profile for profile in self.profiles if profile.profile_id == profile_id),
            None,
        )


__all__ = ["ModbusProfileCatalog"]
