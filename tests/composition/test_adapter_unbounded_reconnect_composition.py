"""CompositeAdapter never gives up on a child adapter permanently.

Closes P1.3 and P1.4 from the 2026-08-23 re-audit of commit 61439f3:

- P1.3: the old ``pump()`` reconnect loop capped retries at
  ``max_reconnect_attempts`` (default 3). Once exhausted, that child's pump
  task exited for good -- if the adapter came back an hour later, the
  CompositeAdapter never noticed, even though sibling adapters kept
  streaming fine through the same composite. This proves a child that fails
  reconnection far more than the old cap still eventually reconnects.
- P1.4: a graceful stream end (the underlying transport's ``async for``
  simply returning, e.g. a closed websocket) was only retried once
  (``reconnect_on_stream_end`` capped at ``stream_end_reconnects >= 1``).
  This proves repeated graceful stream ends are retried just like repeated
  connection errors, unboundedly.
"""

from __future__ import annotations

import asyncio

import pytest

from domoai.domain.models import StateChangedEvent
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


class FailsConnectNTimesAdapter(RecordingAdapter):
    """Stream fails once; the following ``connect()`` calls fail
    ``failures_before_success`` times before finally succeeding."""

    def __init__(self, *, failures_before_success: int) -> None:
        super().__init__("matter", source_snapshot(adapter_id="matter"))
        self.failures_before_success = failures_before_success
        self.connect_attempts = 0
        self._stream_failed_once = False

    async def connect(self) -> None:
        self.connect_attempts += 1
        # The very first call is the composite's initial join (composite.connect()
        # calls every adapter's connect() once up front); that one must succeed
        # or this adapter never joins `active` and no pump task is ever created
        # for it at all. Only the *reconnect-loop* calls that follow the stream
        # failure are the ones scripted to fail.
        if self.connect_attempts == 1:
            self.connected = True
            return
        if self.connect_attempts <= 1 + self.failures_before_success:
            raise ConnectionError(f"matter still unreachable (attempt {self.connect_attempts})")
        self.connected = True

    def subscribe_events(self):
        if not self._stream_failed_once:
            self._stream_failed_once = True

            async def failed_stream():
                raise ConnectionError("matter stream lost")
                yield  # pragma: no cover

            return failed_stream()
        self.events = [StateChangedEvent(payload={"recovered": True})]
        return super().subscribe_events()


class LongLivedStreamAdapter(RecordingAdapter):
    """Yields its fixed events, then blocks like a real live connection.

    ``RecordingAdapter.subscribe_events`` yields its buffered events and
    then returns, which is a fixture-only artifact -- real subscribe_events
    implementations loop until the underlying connection actually drops.
    Under ``reconnect_on_stream_end=True`` the finite fixture's graceful
    return would itself be (correctly) treated as an unexpected stream end
    and endlessly reconnected, which is not what these tests are about.
    """

    async def subscribe_events(self):
        for event in self.events:
            yield event
        await asyncio.Event().wait()


class EndsStreamGracefullyNTimesAdapter(RecordingAdapter):
    """Every ``subscribe_events()`` call returns an empty (gracefully ended)
    stream ``graceful_ends_before_recovery`` times, then finally yields."""

    def __init__(self, *, graceful_ends_before_recovery: int) -> None:
        super().__init__("matter", source_snapshot(adapter_id="matter"))
        self.graceful_ends_before_recovery = graceful_ends_before_recovery
        self.subscribe_attempts = 0

    async def subscribe_events(self):
        self.subscribe_attempts += 1
        if self.subscribe_attempts <= self.graceful_ends_before_recovery:
            return
        yield StateChangedEvent(payload={"recovered": True})
        # Once truly recovered this behaves like a real live connection
        # (see LongLivedStreamAdapter): it must not gracefully end again,
        # or reconnect_on_stream_end would -- correctly -- treat that as
        # another disconnect and loop forever past the scripted recovery.
        await asyncio.Event().wait()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_child_adapter_reconnects_past_the_old_bounded_attempt_cap() -> None:
    # The pre-fix implementation gave up permanently after 3 attempts by
    # default; 5 failures before success would previously have killed this
    # child's pump for good.
    failing = FailsConnectNTimesAdapter(failures_before_success=5)
    healthy = LongLivedStreamAdapter("ha", source_snapshot(adapter_id="ha"))
    healthy.events = [StateChangedEvent(payload={"healthy": True})]
    registry = DeviceRegistry()
    composite = CompositeAdapter(
        [failing, healthy],
        registry=registry,
        reconnect_initial_delay=0.01,
        reconnect_max_delay=0.01,
    )
    await composite.connect()
    await composite.discover()

    stream = composite.subscribe_events()
    collected = []
    async for event in stream:
        collected.append(event)
        if any(getattr(event, "payload", {}).get("recovered") for event in collected):
            break
        if len(collected) > 200:  # safety net against an infinite test hang
            pytest.fail("matter never recovered from repeated connect failures")

    assert failing.connect_attempts == 7  # 1 initial join + 5 failures + 1 success
    assert failing.connected is True
    health = await composite.health()
    statuses = {item.adapter_id: item.connected for item in health.components}
    assert statuses["matter"] is True
    assert statuses["ha"] is True


@pytest.mark.composition
@pytest.mark.asyncio
async def test_child_adapter_reconnects_past_the_old_single_graceful_end_cap() -> None:
    # The pre-fix implementation only retried a graceful stream end once
    # (stream_end_reconnects >= 1); 3 graceful ends would previously have
    # killed this child's pump for good on the second one.
    ending = EndsStreamGracefullyNTimesAdapter(graceful_ends_before_recovery=3)
    healthy = LongLivedStreamAdapter("ha", source_snapshot(adapter_id="ha"))
    healthy.events = [StateChangedEvent(payload={"healthy": True})]
    registry = DeviceRegistry()
    composite = CompositeAdapter(
        [ending, healthy],
        registry=registry,
        reconnect_initial_delay=0.01,
        reconnect_max_delay=0.01,
        reconnect_on_stream_end=True,
    )
    await composite.connect()
    await composite.discover()

    stream = composite.subscribe_events()
    collected = []
    async for event in stream:
        collected.append(event)
        if any(getattr(event, "payload", {}).get("recovered") for event in collected):
            break
        if len(collected) > 50:  # safety net against an infinite test hang
            pytest.fail("adapter never recovered from repeated graceful stream ends")

    assert ending.subscribe_attempts == 4  # 3 graceful ends + 1 recovery
    health = await composite.health()
    statuses = {item.adapter_id: item.connected for item in health.components}
    assert statuses["matter"] is True
