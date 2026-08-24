"""The battery dispatch contract has one neutral domain owner."""

from __future__ import annotations


def test_dispatchable_binding_has_one_canonical_owner() -> None:
    from domoai.config import battery_profile, battery_qualification
    from domoai.domain.energy import DispatchableBatteryBinding
    from domoai.optimizer.energy import DispatchableBatteryBinding as OptimizerBinding
    from domoai.optimizer.providers import DispatchableBatteryBinding as ProviderBinding

    assert OptimizerBinding is DispatchableBatteryBinding
    assert ProviderBinding is DispatchableBatteryBinding
    assert battery_profile.DispatchableBatteryBinding is DispatchableBatteryBinding
    assert battery_qualification.DispatchableBatteryBinding is DispatchableBatteryBinding
