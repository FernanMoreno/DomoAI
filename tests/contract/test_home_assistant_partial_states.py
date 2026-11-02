from __future__ import annotations

from typing import Any

import pytest

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.domain.models import AdapterDiagnosticEvent
from domoai.domain.provider import MeasurementQuality
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient


def entity(state: object = "on", brightness: object = 128, **extra: Any) -> dict[str, Any]:
    return {
        "entity_id": "light.shared",
        "state": state,
        "attributes": {"brightness": brightness},
        **extra,
    }


def event(state: object) -> dict[str, Any]:
    return {"event": {"data": {"entity_id": "light.shared", "new_state": state}}}


@pytest.mark.parametrize(
    "bad", [None, [], "token=never-publish", {}, {"entity_id": "light.broken", "attributes": []}]
)
async def test_malformed_batch_preserves_valid_neighbor_and_safe_diagnostic(bad: Any) -> None:
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([bad, entity()]))
    snapshot = await provider.snapshot()
    assert [(s["capability"], s["value"]) for s in snapshot.source_states] == [
        ("power", True),
        ("brightness", 50),
    ]
    assert len(snapshot.unsupported_sources) == 1
    diagnostic = snapshot.unsupported_sources[0]
    assert diagnostic["failure"] is False
    assert "token=" not in str(diagnostic)
    assert "source_unavailable" not in str(diagnostic)
    assert len(str(diagnostic)) < 600


@pytest.mark.parametrize(
    "bad", [True, float("nan"), float("inf"), 1e308, 10**400, "token=never-publish", []]
)
async def test_bad_brightness_preserves_power_and_measurement_quality(bad: Any) -> None:
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([entity(brightness=bad)]))
    snapshot = await provider.snapshot()
    power, brightness = snapshot.source_states
    assert (power["value"], power["status"]) == (True, "current")
    assert (brightness["value"], brightness["status"]) == (None, "invalid")
    assert len(snapshot.unsupported_sources) == 1
    assert "token=" not in str(snapshot.unsupported_sources)
    measurements = await provider.get_measurements()
    assert [(m.metric, m.quality) for m in measurements] == [
        ("power", MeasurementQuality.GOOD),
        ("brightness", MeasurementQuality.INVALID),
    ]


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
async def test_unavailable_nulls_all_dependent_values_in_snapshot_and_measurements(
    state: str,
) -> None:
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([entity(state)]))
    snapshot = await provider.snapshot()
    assert all(s["value"] is None and s["status"] == "unavailable" for s in snapshot.source_states)
    measurements = await provider.get_measurements()
    assert all(
        m.value == "unavailable" and m.quality is MeasurementQuality.UNAVAILABLE
        for m in measurements
    )


@pytest.mark.parametrize(
    "state, expected, status",
    [
        ("off", False, "current"),
        ("on", True, "current"),
        ("nonsense", None, "invalid"),
        ("0", None, "invalid"),
        (False, None, "invalid"),
        ([], None, "invalid"),
    ],
)
async def test_only_recognized_power_state_becomes_current(
    state: Any, expected: Any, status: str
) -> None:
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([entity(state)]))
    power = (await provider.snapshot()).source_states[0]
    assert (power["value"], power["status"]) == (expected, status)


@pytest.mark.parametrize(
    "metadata",
    [
        {"supported_features": ["brightness"]},
        {"attributes": {"supported_features": 1}},
        {"attributes": {"supported_color_modes": ["brightness"]}},
    ],
)
async def test_metadata_support_survives_missing_brightness(metadata: dict[str, Any]) -> None:
    provider = HomeAssistantProvider(
        FakeHomeAssistantProviderClient(
            [{"entity_id": "light.shared", "state": "off", "attributes": {}, **metadata}]
        )
    )
    states = (await provider.snapshot()).source_states
    assert [(s["capability"], s["value"], s["status"]) for s in states] == [
        ("power", False, "current"),
        ("brightness", None, "unavailable"),
    ]


async def test_prior_support_survives_refresh_but_is_not_invented_for_other_entities() -> None:
    client = FakeHomeAssistantProviderClient([entity()])
    provider = HomeAssistantProvider(client)
    await provider.snapshot()
    client.states = [
        entity("off", attributes={}),
        {"entity_id": "light.simple", "state": "off", "supported_features": None},
    ]
    snapshot = await provider.snapshot()
    assert [(s["entity_id"], s["capability"], s["value"]) for s in snapshot.source_states] == [
        ("light.shared", "power", False),
        ("light.shared", "brightness", None),
        ("light.simple", "power", False),
    ]


@pytest.mark.parametrize(
    "bad", [None, [], {"event": []}, event([]), event({"state": "on", "attributes": []})]
)
async def test_malformed_event_does_not_end_either_stream_or_publish_raw_payload(bad: Any) -> None:
    events = [bad, event(entity("on"))]
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([entity()], events=events))
    await provider.snapshot()
    measurements = [m async for m in provider.subscribe()]
    assert any(m.metric == "brightness" and m.value == 50 for m in measurements)
    bridge = HomeAssistantProviderAdapter(
        HomeAssistantProvider(FakeHomeAssistantProviderClient([entity()], events=events)),
        adapter_id="ha:house-a",
    )
    await bridge.discover()
    runtime_events = [e async for e in bridge.subscribe_events()]
    assert all("raw" not in e.payload for e in runtime_events)
    assert any(
        isinstance(e, AdapterDiagnosticEvent) and e.code == "invalid_state" for e in runtime_events
    )
    states = [s for e in runtime_events if e.kind == "state_changed" for s in e.payload["states"]]
    assert states[-1]["value"] == 50
    assert all(s["source_ref"]["adapter_id"] == "ha:house-a" for s in states)


async def test_provider_stream_recovers_without_stale_availability_or_stale_attributes() -> None:
    provider = HomeAssistantProvider(
        FakeHomeAssistantProviderClient(
            [entity("unavailable")], events=[event(entity("on")), event({"state": "off"})]
        )
    )
    await provider.snapshot()
    measurements = [m async for m in provider.subscribe()]
    assert [(m.metric, m.value, m.quality.value) for m in measurements] == [
        ("power", True, "good"),
        ("brightness", 50, "good"),
        ("power", False, "good"),
        ("brightness", "unavailable", "unavailable"),
    ]


async def test_real_http_batch_preserves_malformed_elements_for_local_diagnostics() -> None:
    import httpx

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    class HttpClientWithoutRegistry(HomeAssistantClient):
        async def fetch_entity_registry(self) -> list[dict[str, Any]]:
            return []

        async def fetch_device_registry(self) -> list[dict[str, Any]]:
            return []

    client = HttpClientWithoutRegistry(
        "http://ha.test",
        "secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[None, entity()])),
    )
    try:
        snapshot = await HomeAssistantProvider(client).snapshot()
        assert [s["value"] for s in snapshot.source_states] == [True, 50]
        assert len(snapshot.unsupported_sources) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_http_global_errors_propagate_from_snapshot(status: int) -> None:
    import httpx

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    client = HomeAssistantClient(
        "http://ha.test",
        "secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await HomeAssistantProvider(client).snapshot()
    finally:
        await client.close()


async def test_http_non_list_is_still_a_global_error() -> None:
    import httpx

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    client = HomeAssistantClient(
        "http://ha.test",
        "secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"bad": "container"})
        ),
    )
    try:
        with pytest.raises(ValueError, match="non-list"):
            await HomeAssistantProvider(client).snapshot()
    finally:
        await client.close()


class ScriptedSocket:
    def __init__(self, messages: list[object]) -> None:
        self.messages = iter([{"type": "auth_required"}, {"type": "auth_ok"}, *messages])
        self.closed = False

    async def __aenter__(self) -> ScriptedSocket:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def send(self, message: str) -> None:
        pass

    async def recv(self) -> str:
        import json

        message = next(self.messages)
        if isinstance(message, Exception):
            raise message
        return json.dumps(message)


@pytest.mark.parametrize("runtime", [False, True])
async def test_reconnected_websocket_normalizes_invalid_then_valid_without_reread(
    monkeypatch: pytest.MonkeyPatch,
    runtime: bool,
) -> None:
    import websockets

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    first = ScriptedSocket(
        [[], {"type": "event", **event(entity(brightness=True))}, OSError("socket dropped")]
    )
    second = ScriptedSocket([{"type": "event", **event(entity(brightness=255))}])
    sockets = iter([first, second])
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: next(sockets))
    client = HomeAssistantClient("http://ha.test", "secret-token")
    provider = HomeAssistantProvider(client)
    stream = (
        HomeAssistantProviderAdapter(provider).subscribe_events()
        if runtime
        else provider.subscribe()
    )
    results = []
    try:
        async for item in stream:
            results.append(item)
            if runtime:
                if item.kind == "state_changed" and item.payload["states"][-1]["value"] == 100:
                    break
            elif item.metric == "brightness" and item.value == 100:
                break
    finally:
        await stream.aclose()
        await client.close()
    if runtime:
        states = [e.payload["states"] for e in results if e.kind == "state_changed"]
        assert states[0][-1]["status"] == "invalid"
        assert states[1][-1]["status"] == "current"
        assert any(isinstance(e, AdapterDiagnosticEvent) for e in results)
    else:
        brightness = [m for m in results if m.metric == "brightness"]
        assert [m.quality for m in brightness] == [
            MeasurementQuality.INVALID,
            MeasurementQuality.GOOD,
        ]
    assert first.closed


@pytest.mark.parametrize("error_type", [OSError, PermissionError, TimeoutError])
@pytest.mark.parametrize("runtime", [False, True])
async def test_websocket_global_failure_propagates_after_client_retry(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    runtime: bool,
) -> None:
    import websockets

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    sockets = [ScriptedSocket([error_type("global failure")]) for _ in range(2)]
    pending = iter(sockets)
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: next(pending))
    client = HomeAssistantClient("http://ha.test", "secret-token")
    provider = HomeAssistantProvider(client)
    stream = (
        HomeAssistantProviderAdapter(provider).subscribe_events()
        if runtime
        else provider.subscribe()
    )
    try:
        with pytest.raises(error_type, match="global failure"):
            await anext(stream)
    finally:
        await stream.aclose()
        await client.close()
    assert all(socket.closed for socket in sockets)


@pytest.mark.parametrize(
    "state, available, expected",
    [
        ({"power": False}, True, [(False, "current"), (None, "unavailable")]),
        ({"power": False, "brightness": None}, True, [(False, "current"), (None, "unavailable")]),
        ({"power": True, "brightness": float("inf")}, True, [(True, "current"), (None, "invalid")]),
        ({"power": True, "brightness": 50}, False, [(None, "unavailable"), (None, "unavailable")]),
    ],
)
async def test_legacy_normalized_state_uses_same_partial_quality(
    state: dict[str, Any],
    available: bool,
    expected: list[tuple[Any, str]],
) -> None:
    provider = HomeAssistantProvider(
        FakeHomeAssistantProviderClient(
            [entity(state, supported_features=["brightness"], available=available)]
        )
    )
    snapshot = await provider.snapshot()
    assert [(s["value"], s["status"]) for s in snapshot.source_states] == expected


async def test_websocket_auth_rejection_remains_global(monkeypatch: pytest.MonkeyPatch) -> None:
    import websockets

    from domoai.adapters.home_assistant.client import HomeAssistantClient

    sockets = [ScriptedSocket([]) for _ in range(2)]
    for socket in sockets:
        socket.messages = iter([{"type": "auth_required"}, {"type": "auth_invalid"}])
    pending = iter(sockets)
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: next(pending))
    client = HomeAssistantClient("http://ha.test", "secret-token")
    try:
        with pytest.raises(PermissionError, match="authentication failed"):
            await anext(
                HomeAssistantProviderAdapter(HomeAssistantProvider(client)).subscribe_events()
            )
    finally:
        await client.close()
    assert all(socket.closed for socket in sockets)


@pytest.mark.parametrize(
    "metadata",
    [
        {"area_id": 7},
        {"area_id": []},
        {"area_id": {}},
        {"name": []},
        {"device_id": {}},
        {"domain": 7},
        {"manufacturer": 7},
        {"model": []},
        {"canonical_id": {}},
        {"local_canonical_id": 7},
        {"available": "true"},
        {"attributes": {"friendly_name": ["token=never-publish"]}},
    ],
)
async def test_invalid_entity_metadata_is_isolated_in_snapshot_and_both_streams(
    metadata: dict[str, Any],
) -> None:
    bad = entity(entity_id="light.bad", **metadata)
    good = entity(entity_id="light.good")
    events = [
        {"event": {"data": {"entity_id": "light.bad", "new_state": bad}}},
        {"event": {"data": {"entity_id": "light.good", "new_state": good}}},
        {
            "event": {
                "data": {
                    "entity_id": "light.bad",
                    "new_state": entity(
                        entity_id="light.bad", area_id="living_room", brightness=255
                    ),
                }
            }
        },
    ]
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient([bad, good], events=events))
    snapshot = await provider.snapshot()
    assert {s["entity_id"] for s in snapshot.source_states} == {"light.good"}
    assert len(snapshot.unsupported_sources) == 1
    assert snapshot.unsupported_sources[0]["failure"] is False
    measurements = [m async for m in provider.subscribe()]
    assert [(m.source_ref.external_id, m.value) for m in measurements] == [
        ("light.good", True),
        ("light.good", 50),
        ("light.bad", True),
        ("light.bad", 100),
    ]
    bridge = HomeAssistantProviderAdapter(
        HomeAssistantProvider(FakeHomeAssistantProviderClient([good], events=events))
    )
    await bridge.discover()
    received = [e async for e in bridge.subscribe_events()]
    assert len(received) == 3
    assert isinstance(received[0], AdapterDiagnosticEvent)
    assert received[0].code == "invalid_state"
    assert "token=" not in received[0].model_dump_json()
    assert received[1].payload["states"][-1]["value"] == 50
    assert received[2].payload["states"][-1]["value"] == 100


@pytest.mark.parametrize("recovery", ["snapshot", "provider_stream", "runtime_stream"])
async def test_known_brightness_survives_rejected_snapshot_without_retaining_old_state_or_routes(
    recovery: str,
) -> None:
    from domoai.domain.models import ExecutionStatus
    from tests.contract.test_home_assistant_provider import _command

    good_neighbor = entity(entity_id="light.neighbor")
    client = FakeHomeAssistantProviderClient([entity(), good_neighbor])
    provider = HomeAssistantProvider(client)
    bridge = HomeAssistantProviderAdapter(provider)
    await bridge.connect()
    initial = await bridge.discover()
    assert any(s["entity_id"] == "light.shared" and s["value"] == 50 for s in initial.source_states)
    client.states = [entity(attributes=[]), good_neighbor]
    rejected = await bridge.discover()
    assert {s["entity_id"] for s in rejected.source_states} == {"light.neighbor"}
    ack = await provider.execute(_command("light.shared", "set_brightness", "rejected", value=25))
    assert ack.status is ExecutionStatus.REJECTED
    assert client.service_calls == []

    partial = entity("off", attributes={})
    if recovery == "snapshot":
        client.states = [partial, good_neighbor]
        recovered = await bridge.discover()
        values = [
            (s["capability"], s["value"], s["status"])
            for s in recovered.source_states
            if s["entity_id"] == "light.shared"
        ]
        assert values == [("power", False, "current"), ("brightness", None, "unavailable")]
    elif recovery == "provider_stream":
        client.events = [event(partial)]
        values = [(m.metric, m.value, m.quality.value) async for m in provider.subscribe()]
        assert values == [("power", False, "good"), ("brightness", "unavailable", "unavailable")]
    else:
        client.events = [event(partial)]
        received = [e async for e in bridge.subscribe_events()]
        assert len(received) == 1
        values = [(s["capability"], s["value"], s["status"]) for s in received[0].payload["states"]]
        assert values == [("power", False, "current"), ("brightness", None, "unavailable")]

    # Only capability evidence survives. A later observation supplies a new value.
    client.events = [event(entity("on", brightness=255))]
    measurements = [m async for m in provider.subscribe()]
    assert [(m.metric, m.value, m.quality.value) for m in measurements] == [
        ("power", True, "good"),
        ("brightness", 100, "good"),
    ]
    separate = HomeAssistantProvider(FakeHomeAssistantProviderClient([partial]))
    assert [s["capability"] for s in (await separate.snapshot()).source_states] == ["power"]
