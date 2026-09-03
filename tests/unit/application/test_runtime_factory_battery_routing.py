from types import SimpleNamespace

import pytest

from domoai.application.runtime_factory import _select_control_adapter


class _TakeoverAdapter:
    adapter_id = "home_assistant"

    async def acquire_control(self, request):
        raise AssertionError("not called by routing test")


def test_battery_takeover_routes_to_matching_composite_child() -> None:
    child = _TakeoverAdapter()
    composite = SimpleNamespace(adapter_id="composite", adapters=(child,))

    assert _select_control_adapter(composite, "home_assistant") is child


def test_battery_takeover_rejects_missing_provider_instead_of_using_composite() -> None:
    composite = SimpleNamespace(adapter_id="composite", adapters=())

    with pytest.raises(ValueError, match="not configured as a concrete runtime adapter"):
        _select_control_adapter(composite, "home_assistant")
