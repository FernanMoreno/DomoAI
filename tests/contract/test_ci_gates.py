import stat
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_unit_ci_enforces_a_positive_coverage_floor() -> None:
    workflow = _workflow_text()
    unit_job = workflow.split("  test-unit:\n", 1)[1].split("  test-contract:\n", 1)[0]

    assert "--cov=domoai" in unit_job
    marker = "--cov-fail-under="
    assert marker in unit_job
    threshold = unit_job.split(marker, 1)[1].split()[0]
    assert threshold.isdigit()
    assert int(threshold) > 0


def test_mandatory_docker_job_fails_without_daemon_and_rejects_skips() -> None:
    workflow = _workflow_text()
    assert "  test-docker-composition:\n" in workflow
    docker_job = workflow.split("  test-docker-composition:\n", 1)[1].split(
        "  package:\n", 1
    )[0]

    assert "docker info" in docker_job
    assert "scripts/ci/run_mandatory_docker_composition.sh" in docker_job
    assert "junit-docker-composition.xml" in docker_job
    helper = ROOT / "scripts/ci/run_mandatory_docker_composition.sh"
    assert helper.is_file()
    assert helper.stat().st_mode & stat.S_IXUSR
    helper_text = helper.read_text(encoding="utf-8")
    assert "test_zigbee2mqtt_broker_composition.py" in helper_text
    assert "skipped" in helper_text
    assert "docker compose" in helper_text
    assert "caddy validate" in helper_text


def test_mandatory_docker_job_is_part_of_release_and_evidence_gates() -> None:
    workflow = _workflow_text()
    required = workflow.split("  required-release-gates:\n", 1)[1].split(
        "  # Closes P2.4", 1
    )[0]
    evidence = workflow.split("  evidence-summary:\n", 1)[1]

    assert "- test-docker-composition" in required
    assert "- test-docker-composition" in evidence
