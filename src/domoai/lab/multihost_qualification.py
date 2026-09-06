"""Lab-only entry point for the disposable multi-host qualification exercise."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator
from domoai.application.multihost_qualification import (
    EtcdHttpQuorumProbe,
    MultiHostQualificationRunner,
    PsycopgPostgresHaProbe,
)
from domoai.domain.coordination import LeaseScope
from domoai.hil.multihost import JsonlFencingGatewayBridge


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the disposable DomoAI multi-host lab")
    parser.add_argument("--etcd-endpoint", action="append", required=True)
    parser.add_argument("--postgres-dsn", required=True)
    parser.add_argument("--bridge-path", required=True)
    parser.add_argument("--bridge-state-file", required=True)
    parser.add_argument("--evidence-path", required=True)
    parser.add_argument("--gateway-identity", default="domoai-lab-gateway")
    args = parser.parse_args()
    if len(args.etcd_endpoint) != 3 or len(set(args.etcd_endpoint)) != 3:
        parser.error("exactly three distinct --etcd-endpoint values are required")
    return args


def _write_evidence(path: Path, payload: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


async def _run(args: argparse.Namespace) -> int:
    quorum_probe = EtcdHttpQuorumProbe(args.etcd_endpoint)
    coordinator = EtcdHttpLeaseCoordinator(args.etcd_endpoint)
    runner = MultiHostQualificationRunner(
        coordinator=coordinator,
        quorum_probe=quorum_probe,
        postgres_probe=PsycopgPostgresHaProbe(args.postgres_dsn),
        gateway=JsonlFencingGatewayBridge(
            (sys.executable, args.bridge_path, "--state-file", args.bridge_state_file)
        ),
        qualification_environment="lab",
    )
    try:
        evidence = await runner.run(
            scope=LeaseScope(
                tenant_id="lab-tenant",
                household_id="lab-household",
                deployment_id="docker-qualification",
            ),
            owner_id="lab-runner",
            gateway_identity=args.gateway_identity,
            safe_command="lab-safe-noop",
            ttl_seconds=30,
            evidence_ttl=timedelta(minutes=15),
        )
        _write_evidence(Path(args.evidence_path), evidence.model_dump_json())
        return 0 if evidence.status == "passed" else 1
    finally:
        await quorum_probe.aclose()
        await coordinator.aclose()


def main() -> int:
    args = _arguments()
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
