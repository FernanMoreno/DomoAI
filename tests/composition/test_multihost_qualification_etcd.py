from __future__ import annotations

import pytest
import pytest_asyncio

from domoai.application.multihost_qualification import EtcdHttpQuorumProbe

pytest.importorskip("testcontainers", reason="testcontainers is a dev-only dependency")

from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.wait_strategies import LogMessageWaitStrategy  # noqa: E402


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.composition,
    pytest.mark.skipif(
        not _docker_available(), reason="Docker daemon not reachable; skipping real-etcd test"
    ),
]


@pytest_asyncio.fixture
async def etcd_endpoint() -> str:
    container = (
        DockerContainer("quay.io/coreos/etcd:v3.5.17")
        .with_exposed_ports(2379)
        .with_command(
            "etcd --name infra0 --data-dir /etcd-data "
            "--advertise-client-urls http://0.0.0.0:2379 "
            "--listen-client-urls http://0.0.0.0:2379 "
            "--initial-advertise-peer-urls http://0.0.0.0:2380 "
            "--listen-peer-urls http://0.0.0.0:2380 "
            "--initial-cluster infra0=http://0.0.0.0:2380 "
            "--initial-cluster-state new --initial-cluster-token domoai-qualification-test"
        )
        .waiting_for(
            LogMessageWaitStrategy("ready to serve client requests").with_startup_timeout(60)
        )
    )
    container.start()
    try:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(2379)}"
    finally:
        container.stop()


@pytest.mark.asyncio
async def test_real_single_etcd_cannot_be_mistaken_for_a_three_member_quorum(
    etcd_endpoint: str,
) -> None:
    probe = EtcdHttpQuorumProbe((etcd_endpoint, etcd_endpoint, etcd_endpoint))

    observation = await probe.inspect()

    assert len(observation.member_ids) == 3
    assert len(set(observation.member_ids)) == 1
    await probe.aclose()
