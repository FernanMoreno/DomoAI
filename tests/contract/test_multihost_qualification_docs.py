from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_multihost_runbook_exposes_attended_gate_and_jsonl_contract() -> None:
    runbook = (ROOT / "deploy/multihost/README.md").read_text(encoding="utf-8")
    contracts = (ROOT / "docs/contracts.md").read_text(encoding="utf-8")

    for required in (
        "qualify-multihost",
        "--confirm-physical-fencing",
        "DOMOAI_MULTI_HOST_PRODUCTION_ENABLED",
        "DOMOAI_MULTI_HOST_QUALIFICATION_EVIDENCE_PATH",
        "gateway_identity",
        "JSONL",
        "accepted",
        "observed_epoch",
    ):
        assert required in runbook
    assert "multihost-qualification-evidence.schema.json" in contracts
    assert "gateway-fencing-probe-request.schema.json" in contracts


def test_disposable_etcd_topology_is_explicitly_non_production() -> None:
    topology = ROOT / "deploy/multihost/qualification/compose.yaml"
    text = topology.read_text(encoding="utf-8")

    assert "etcd-1:" in text
    assert "etcd-2:" in text
    assert "etcd-3:" in text
    assert "NOT FOR PRODUCTION" in text


def test_disposable_topology_provides_strict_synchronous_patroni_writer() -> None:
    """Catch removal of the lab-only HA writer topology or its safety boundaries."""
    topology_root = ROOT / "deploy/multihost/qualification"
    compose_path = topology_root / "compose.yaml"
    compose_text = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)

    services = compose["services"]
    base_services = {
        "etcd-1",
        "etcd-2",
        "etcd-3",
        "postgres-1",
        "postgres-2",
        "postgres-3",
        "postgres-writer",
    }
    assert set(services) - {"host-a", "host-b"} == base_services
    assert {"host-a", "host-b"} <= set(services)
    assert "runner" not in services
    assert "NOT FOR PRODUCTION" in compose_text

    for service_name in base_services | {"host-a", "host-b"}:
        service = services[service_name]
        assert "ports" not in service
        assert service["labels"]["com.domoai.lab"] == "true"
        assert service["labels"]["com.domoai.not-for-production"] == "true"
    for host in ("host-a", "host-b"):
        assert services[host]["profiles"] == ["v2"]

    for node in ("postgres-1", "postgres-2", "postgres-3"):
        assert services[node]["build"] == "./patroni"
        assert services[node]["environment"]["PATRONI_NAME"] == node
        assert services[node]["environment"]["PATRONI_RESTAPI_LISTEN"] == "0.0.0.0:8008"
        assert services[node]["environment"]["PATRONI_POSTGRESQL_LISTEN"] == "0.0.0.0:5432"

    dockerfile = (topology_root / "patroni/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("# NOT FOR PRODUCTION")
    assert "FROM postgres:16." in dockerfile

    volumes = compose["volumes"]
    assert set(volumes) == {
        "etcd-1-data",
        "etcd-2-data",
        "etcd-3-data",
        "postgres-1-data",
        "postgres-2-data",
        "postgres-3-data",
        "lab-state",
    }
    for volume in volumes.values():
        assert not {"external", "name", "driver"} & volume.keys()

    patroni = (topology_root / "patroni/patroni.yml").read_text(encoding="utf-8")
    assert "etcd-1:2379,etcd-2:2379,etcd-3:2379" in patroni
    assert "synchronous_mode: true" in patroni
    assert "synchronous_mode_strict: true" in patroni

    haproxy = (topology_root / "haproxy.cfg").read_text(encoding="utf-8")
    assert "option httpchk GET /primary" in haproxy
    assert "http-check expect status 200" in haproxy
    for node in ("postgres-1", "postgres-2", "postgres-3"):
        assert f"server {node} {node}:5432 check port 8008" in haproxy
