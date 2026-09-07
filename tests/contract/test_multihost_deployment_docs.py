from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_multihost_runbook_requires_real_quorum_tls_and_fencing() -> None:
    runbook = (ROOT / "deploy/multihost/README.md").read_text(encoding="utf-8")

    for required in ("quorum de 3 o 5", "mTLS", "fencing_epoch", "active-passive"):
        assert required in runbook
    assert "active-active" in runbook.lower()


def test_single_host_example_does_not_enable_multihost() -> None:
    env_example = (ROOT / "deploy/gateway.env.example").read_text(encoding="utf-8")

    assert "# DOMOAI_MULTI_HOST_ENABLED=false" in env_example
    assert "DOMOAI_MULTI_HOST_ENABLED=true" not in env_example
