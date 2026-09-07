import httpx
import pytest


@pytest.mark.asyncio
async def test_etcd_quorum_probe_requires_one_cluster_and_three_members() -> None:
    from domoai.application.multihost_qualification import EtcdHttpQuorumProbe

    async def handler(request: httpx.Request) -> httpx.Response:
        endpoint_id = request.url.host.split(".")[0].rsplit("-", 1)[-1]
        return httpx.Response(
            200,
            json={
                "header": {"cluster_id": "cluster-1", "member_id": f"member-{endpoint_id}"},
                "leader": "member-1",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = EtcdHttpQuorumProbe(
        ("https://etcd-1.test", "https://etcd-2.test", "https://etcd-3.test"),
        client=client,
    )

    observation = await probe.inspect()

    assert observation.cluster_id == "cluster-1"
    assert set(observation.member_ids) == {"member-1", "member-2", "member-3"}
    assert observation.leader_id == "member-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_etcd_quorum_probe_rejects_mixed_clusters() -> None:
    from domoai.application.multihost_qualification import EtcdHttpQuorumProbe

    async def handler(request: httpx.Request) -> httpx.Response:
        cluster = "cluster-a" if request.url.host == "etcd-1.test" else "cluster-b"
        return httpx.Response(
            200,
            json={
                "header": {"cluster_id": cluster, "member_id": request.url.host},
                "leader": "etcd-1.test",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = EtcdHttpQuorumProbe(
        ("https://etcd-1.test", "https://etcd-2.test", "https://etcd-3.test"),
        client=client,
    )

    with pytest.raises(RuntimeError, match="different clusters"):
        await probe.inspect()
    await client.aclose()


@pytest.mark.asyncio
async def test_postgres_ha_probe_reads_primary_and_synchronous_replica_state() -> None:
    from domoai.application.multihost_qualification import PsycopgPostgresHaProbe

    class Cursor:
        def execute(self, query: str):
            assert "pg_stat_replication" in query
            return self

        def fetchone(self):
            return (True, 1)

        def close(self) -> None:
            pass

    class Connection:
        def execute(self, query: str):
            return Cursor().execute(query)

        def close(self) -> None:
            pass

    probe = PsycopgPostgresHaProbe(
        "postgresql://example", connection_factory=lambda _: Connection()
    )

    observation = await probe.inspect()

    assert observation.is_primary is True
    assert observation.synchronous_replicas == 1
