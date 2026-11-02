"""T012-C6 RED: RuntimeReviewSource reads real runtime state and never swallows errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tests.security.test_household_consent_security import _c3_device, _c3_plan, _c3_rule

from domoai.domain.household_consent import ConsentControlVersion, ConsentReference
from domoai.domain.household_preferences import PreferenceProfile, PreferenceVersion
from domoai.domain.models import PrincipalRole

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
HEAD = ConsentControlVersion(generation="00000000-0000-4000-8000-000000000004", revision=3)


def _identity() -> Any:
    return SimpleNamespace(
        principal_id="resident-a",
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=("home-a",),
        roles=(PrincipalRole.OWNER,),
        operations=("review_consent", "approve_consent"),
    )


class _Plans:
    def __init__(self, plan: Any = None, error: Exception | None = None) -> None:
        self.plan, self.error, self.asked = plan, error, []

    async def get(self, plan_id: str) -> Any:
        self.asked.append(plan_id)
        if self.error is not None:
            raise self.error
        return self.plan


class _Rules:
    def __init__(self, rule: Any = None) -> None:
        self.rule = rule

    async def get(self, rule_id: str) -> Any:
        return None if self.rule is None else SimpleNamespace(rule=self.rule)


class _Preferences:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.error = error
        self.profile = PreferenceProfile(
            version=PreferenceVersion(
                generation="00000000-0000-4000-8000-000000000002", revision=9
            ),
            observed_at=NOW,
            next_transition_at=None,
            preferences=[],
            next_cursor=None,
        )

    async def read(self, request: Any, authority: Any) -> Any:
        self.calls.append((request, authority))
        if self.error is not None:
            raise self.error
        return self.profile


class _Store:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def get_consent_control(self, tenant_id: str, household_id: str) -> ConsentControlVersion:
        self.asked.append((tenant_id, household_id))
        return HEAD


def _source(**changes: Any) -> Any:
    from domoai.mcp.consent_review_source import RuntimeReviewSource

    options: dict[str, Any] = {
        "plans": _Plans(_c3_plan(NOW)),
        "rules": _Rules(_c3_rule(NOW)),
        "registry": SimpleNamespace(devices=[_c3_device("light-a")]),
        "preferences": _Preferences(),
        "approval_store": _Store(),
        "timezone": "Europe/Madrid",
        "tenant_id": "tenant-a",
        "household_id": "home-a",
    }
    options.update(changes)
    return RuntimeReviewSource(**options)


def test_source_load_is_a_coroutine_function() -> None:
    import inspect

    from domoai.mcp.consent_review_source import RuntimeReviewSource

    assert inspect.iscoroutinefunction(RuntimeReviewSource.load)


async def test_plan_review_input_carries_the_real_state() -> None:
    preferences, store = _Preferences(), _Store()
    source = _source(preferences=preferences, approval_store=store)
    loaded = await source.load(_identity(), ConsentReference(kind="plan", id="plan-a"))
    assert loaded.target.id == "plan-a"
    assert [device.id for device in loaded.devices] == ["light-a"]
    assert loaded.preferences is preferences.profile
    assert loaded.control_version == HEAD and store.asked == [("tenant-a", "home-a")]
    assert loaded.timezone == "Europe/Madrid"


async def test_preferences_are_read_with_the_service_authority_and_full_page() -> None:
    preferences = _Preferences()
    source = _source(preferences=preferences)
    await source.load(_identity(), ConsentReference(kind="plan", id="plan-a"))
    ((request, authority),) = preferences.calls
    assert request.limit == 100 and request.effective_only is True
    assert authority.tenant_id == "tenant-a" and authority.household_id == "home-a"
    assert authority.household_ids == ["home-a"]
    assert authority.roles == [PrincipalRole.SERVICE]
    assert not authority.operations, "the resident identity operations must not be reused"
    assert authority.principal_id != "resident-a"


async def test_rule_review_input_unwraps_the_stored_rule() -> None:
    rule = _c3_rule(NOW)
    loaded = await _source(rules=_Rules(rule)).load(
        _identity(), ConsentReference(kind="rule", id="rule-a")
    )
    assert loaded.target is rule


async def test_absent_targets_return_none() -> None:
    assert (
        await _source(plans=_Plans(None)).load(
            _identity(), ConsentReference(kind="plan", id="plan-x")
        )
        is None
    )
    assert (
        await _source(rules=_Rules(None)).load(
            _identity(), ConsentReference(kind="rule", id="rule-x")
        )
        is None
    )


async def test_a_rule_without_a_rule_repository_returns_none() -> None:
    assert (
        await _source(rules=None).load(_identity(), ConsentReference(kind="rule", id="rule-a"))
        is None
    )


@pytest.mark.parametrize("failing", ["plans", "preferences", "approval_store"])
async def test_errors_propagate_and_are_never_swallowed(failing: str) -> None:
    class _Boom(RuntimeError):
        pass

    if failing == "plans":
        source = _source(plans=_Plans(error=_Boom("x")))
    elif failing == "preferences":
        source = _source(preferences=_Preferences(error=_Boom("x")))
    else:

        class _Bad(_Store):
            def get_consent_control(self, tenant_id: str, household_id: str) -> Any:
                raise _Boom("x")

        source = _source(approval_store=_Bad())
    with pytest.raises(_Boom):
        await source.load(_identity(), ConsentReference(kind="plan", id="plan-a"))


async def test_nothing_is_cached_between_loads() -> None:
    registry = SimpleNamespace(devices=[_c3_device("light-a")])
    store = _Store()
    source = _source(registry=registry, approval_store=store)
    reference = ConsentReference(kind="plan", id="plan-a")
    first = await source.load(_identity(), reference)
    registry.devices = [_c3_device("light-a").model_copy(update={"name": "Otra"})]
    second = await source.load(_identity(), reference)
    assert first.devices[0].name != second.devices[0].name
    assert len(store.asked) == 2


async def test_observation_stamp_is_stable_while_the_bound_preference_state_is_unchanged() -> None:
    preferences = _Preferences()
    source = _source(preferences=preferences)
    reference = ConsentReference(kind="plan", id="plan-a")
    first = await source.load(_identity(), reference)
    preferences.profile = preferences.profile.model_copy(
        update={"observed_at": NOW + timedelta(seconds=5)}
    )
    again = await source.load(_identity(), reference)
    assert again.preferences.observed_at == first.preferences.observed_at
    preferences.profile = preferences.profile.model_copy(
        update={
            "version": PreferenceVersion(
                generation="00000000-0000-4000-8000-000000000002", revision=10
            ),
            "observed_at": NOW + timedelta(seconds=9),
        }
    )
    changed = await source.load(_identity(), reference)
    assert changed.preferences.observed_at == NOW + timedelta(seconds=9)


async def test_a_stale_slow_read_never_replaces_a_newer_pinned_observation() -> None:
    preferences = _Preferences()
    source = _source(preferences=preferences)
    reference = ConsentReference(kind="plan", id="plan-a")

    def profile(revision: int, seconds: int) -> Any:
        return preferences.profile.model_copy(
            update={
                "version": PreferenceVersion(
                    generation="00000000-0000-4000-8000-000000000002", revision=revision
                ),
                "observed_at": NOW + timedelta(seconds=seconds),
            }
        )

    for revision, seconds in ((9, 0), (10, 9), (9, 2)):  # the last read finishes late
        preferences.profile = profile(revision, seconds)
        await source.load(_identity(), reference)
    preferences.profile = profile(10, 12)
    current = await source.load(_identity(), reference)
    assert current.preferences.observed_at == NOW + timedelta(seconds=9)


async def test_devices_are_a_copy_not_the_live_registry_list() -> None:
    registry = SimpleNamespace(devices=[_c3_device("light-a")])
    loaded = await _source(registry=registry).load(
        _identity(), ConsentReference(kind="plan", id="plan-a")
    )
    registry.devices.append(_c3_device("light-b"))
    assert [device.id for device in loaded.devices] == ["light-a"]
