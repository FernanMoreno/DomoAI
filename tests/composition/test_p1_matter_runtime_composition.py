import pytest

from domoai.adapters.matter.connection import MatterConnectionProvider
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.connections import ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from tests.fixtures.matter_server import node_snapshots, server_info


@pytest.mark.composition
@pytest.mark.asyncio
async def test_matter_onboarding_builds_and_manages_one_runtime_identity() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    provider = MatterConnectionProvider(lambda _url: transport)
    candidate = (await provider.discover())[0]
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="matter",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
        ),
    )
    adapter = await provider.build_adapter(setup, adapter_id="matter:managed")
    runtime = ManagedAdapterSet()

    await runtime.attach(adapter)
    snapshot = await adapter.discover()

    assert snapshot.source_entities
    assert runtime.adapters == (adapter,)
    assert await runtime.detach("matter:managed") is True
