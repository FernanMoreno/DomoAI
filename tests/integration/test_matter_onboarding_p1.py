import pytest

from domoai.adapters.matter.connection import MatterConnectionProvider
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.connections import ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup
from tests.fixtures.matter_server import node_snapshots, server_info


@pytest.mark.asyncio
async def test_matter_provider_can_build_two_instances_without_identity_collision() -> None:
    transports = [
        InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info()),
        InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info()),
    ]
    provider = MatterConnectionProvider(
        transport_factory=lambda url: transports[0 if "one" in url else 1]
    )

    adapters = []
    for suffix in ("one", "two"):
        candidate = (await provider.discover(endpoint=f"ws://matter-{suffix}"))[0]
        setup = ConnectionSetup(
            candidate=candidate,
            draft=ConnectionDraft(
                connector_id="matter",
                unique_id=candidate.candidate_id,
                endpoint=candidate.endpoint,
                options={},
            ),
        )
        adapters.append(await provider.build_adapter(setup, adapter_id=f"matter:{suffix}"))

    assert [adapter.adapter_id for adapter in adapters] == ["matter:one", "matter:two"]
