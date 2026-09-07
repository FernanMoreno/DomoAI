"""Reusable Phase 4 provider and authority fixtures."""

from domoai.adapters.sdk import AdapterManifest
from domoai.domain.models import CapabilityKind, DeviceType


def phase4_fixture_manifest(adapter_id: str = "fixture") -> AdapterManifest:
    return AdapterManifest(
        adapter_id=adapter_id,
        name="Phase 4 fixture adapter",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="1.0.0",
        device_types=[DeviceType.LIGHT],
        capabilities=[
            {
                "name": "brightness",
                "kind": CapabilityKind.INTEGER,
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
                "guarantees": {
                    "resolution": 1,
                    "readback_required": False,
                },
            }
        ],
    )


__all__ = ["phase4_fixture_manifest"]
