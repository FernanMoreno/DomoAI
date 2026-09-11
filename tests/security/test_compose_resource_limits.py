from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_LIMITS = {
    "mqtt": {"cpus": "0.25", "mem_limit": "128m", "pids_limit": 128, "nofile": (1024, 2048)},
    "homeassistant": {
        "cpus": "2.0",
        "mem_limit": "1g",
        "pids_limit": 512,
        "nofile": (4096, 8192),
    },
    "gateway": {"cpus": "1.0", "mem_limit": "1g", "pids_limit": 256, "nofile": (4096, 8192)},
    "proxy": {"cpus": "0.5", "mem_limit": "128m", "pids_limit": 128, "nofile": (2048, 4096)},
}


def test_every_deploy_service_has_a_finite_resource_budget() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == set(_EXPECTED_LIMITS)
    for service_name, expected in _EXPECTED_LIMITS.items():
        service = compose["services"][service_name]
        assert service["cpus"] == expected["cpus"]
        assert service["mem_limit"] == expected["mem_limit"]
        assert service["pids_limit"] == expected["pids_limit"]
        assert service["ulimits"]["nofile"] == {
            "soft": expected["nofile"][0],
            "hard": expected["nofile"][1],
        }


def test_resource_limits_preserve_lifecycle_and_application_boundaries() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert all(services[name]["restart"] == "unless-stopped" for name in services)
    assert services["gateway"]["depends_on"]["mqtt"]["condition"] == "service_healthy"
    assert services["gateway"]["depends_on"]["homeassistant"]["condition"] == "service_healthy"
    assert services["mqtt"]["healthcheck"]["retries"] == 6
    assert services["homeassistant"]["healthcheck"]["retries"] == 12
    assert services["gateway"]["healthcheck"]["retries"] == 6
    assert services["proxy"]["healthcheck"]["retries"] == 6
