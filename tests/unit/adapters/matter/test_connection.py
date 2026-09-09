import pytest

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.connection import MatterConnectionProvider
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.connections import ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup
from tests.fixtures.matter_server import node_snapshots, server_info


def _provider(
    info: dict[str, object] | None = None,
) -> tuple[MatterConnectionProvider, InMemoryMatterTransport]:
    transport = InMemoryMatterTransport(
        nodes=node_snapshots(1), server_info=info or server_info()
    )
    return MatterConnectionProvider(transport_factory=lambda _url: transport), transport


@pytest.mark.asyncio
async def test_matter_provider_validates_server_and_builds_injected_identity() -> None:
    provider, _transport = _provider()
    candidates = await provider.discover()
    setup = ConnectionSetup(
        candidate=candidates[0],
        draft=ConnectionDraft(
            connector_id="matter",
            unique_id=candidates[0].candidate_id,
            endpoint=candidates[0].endpoint,
            options={},
        ),
    )

    result = await provider.validate(setup)
    adapter = await provider.build_adapter(setup, adapter_id="matter:living-room")

    assert result.valid is True
    assert isinstance(adapter, MatterServerAdapter)
    assert adapter.adapter_id == "matter:living-room"


@pytest.mark.asyncio
async def test_matter_provider_rejects_incompatible_schema() -> None:
    provider, _transport = _provider(server_info(schema_version=10, minimum_schema_version=1))
    candidates = await provider.discover()
    setup = ConnectionSetup(
        candidate=candidates[0],
        draft=ConnectionDraft(
            connector_id="matter",
            unique_id=candidates[0].candidate_id,
            endpoint=candidates[0].endpoint,
            options={},
        ),
    )

    result = await provider.validate(setup)

    assert result.valid is False
    assert any(item.code == "matter_schema_incompatible" for item in result.diagnostics)
