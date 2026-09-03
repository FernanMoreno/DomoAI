from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_knx_gateway_config_has_single_upstream_and_downstream_tunnel() -> None:
    config = (ROOT / "dev/lab/knx-gateway/knxd-wsl.conf.in").read_text()

    assert "ip-address=@KV_HOST@" in config
    assert "dest-port=@KV_PORT@" in config
    assert "driver=ipt" in config
    assert "src-port=@UPSTREAM_SOURCE_PORT@" in config
    assert "nat=true" not in config
    assert "server=ets_router" in config
    assert "port=@GATEWAY_PORT@" in config
    assert "client-addrs=" in config
    assert "filters=log,single,retry-filter" in config


def test_compose_declares_knx_gateway_udp_port() -> None:
    compose = (ROOT / "dev/lab/compose.yaml").read_text()
    service = compose[compose.index("  knx-gateway:") :]

    assert 'profiles: ["knxdocker"]' in service
    assert '"172.26.80.1:3672:3672/udp"' in service
    assert '"3673:3673/udp"' in service
    assert "healthcheck:" in service


def test_gateway_healthcheck_reads_local_udp_endpoint() -> None:
    script = (ROOT / "dev/lab/knx-gateway/healthcheck.sh").read_text()

    assert "awk '{print $4}'" in script


def test_battery_bridge_defaults_to_gateway_port() -> None:
    source = (ROOT / "dev/lab/battery/knx_bridge.py").read_text()

    assert 'os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3672")' in source
    assert 'os.getenv("DOMOAI_KNX_ROUTE_BACK", "0")' in source
