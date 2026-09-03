from __future__ import annotations

import pytest

from domoai.adapters.knx.transport import InMemoryKnxTransport
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.adapters.modbus.transport import InMemoryModbusTransport
from domoai.adapters.zigbee2mqtt.transport import InMemoryMqttTransport
from domoai.runtime.execution_context import (
    ExecutionContext,
    current_execution_principal,
    execution_principal,
)


def _context() -> ExecutionContext:
    return ExecutionContext(
        agent_request_id="agent-transport-1",
        plan_id="plan-transport-1",
        execution_attempt_id="attempt-transport-1",
        adapter_request_id="adapter-transport-1",
    )


def test_execution_principal_is_scoped_and_has_a_safe_local_default() -> None:
    assert current_execution_principal() == "local"

    with execution_principal("agent-codex"):
        assert current_execution_principal() == "agent-codex"
        with execution_principal("agent-claude"):
            assert current_execution_principal() == "agent-claude"
        assert current_execution_principal() == "agent-codex"

    assert current_execution_principal() == "local"


@pytest.mark.asyncio
async def test_matter_transport_records_execution_context() -> None:
    transport = InMemoryMatterTransport(
        nodes=[], server_info={"schema_version": 13, "min_supported_schema_version": 13}
    )
    await transport.connect()

    await transport.request(
        "device_command", {"command": "on"}, execution_context=_context()
    )

    assert transport.requests[0].execution_context == _context()
    assert transport.requests[0].args == {"command": "on"}


@pytest.mark.asyncio
async def test_mqtt_transport_records_context_without_mutating_payload() -> None:
    transport = InMemoryMqttTransport()
    await transport.connect()
    payload = b'{"state":"ON"}'

    await transport.publish("zigbee2mqtt/device/set", payload, execution_context=_context())

    assert transport.published[0].execution_context == _context()
    assert transport.published[0].payload == payload


@pytest.mark.asyncio
async def test_knx_transport_records_context_without_mutating_value() -> None:
    transport = InMemoryKnxTransport()
    await transport.connect()

    await transport.write_group("1/2/3", "DPT-1", True, execution_context=_context())

    assert transport.writes[0].execution_context == _context()
    assert transport.writes[0].value is True


@pytest.mark.asyncio
async def test_modbus_transport_records_context_without_mutating_values() -> None:
    transport = InMemoryModbusTransport()
    await transport.connect()

    await transport.write(1, "coil", 7, (True,), execution_context=_context())

    assert transport.writes[0].execution_context == _context()
    assert transport.writes[0].values == (True,)
