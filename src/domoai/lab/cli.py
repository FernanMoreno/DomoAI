"""Command-line entry point for the local virtual lab."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from domoai.lab.runner import (
    DEFAULT_UP_SERVICES,
    SERVICE_NAMES,
    LabConfig,
    LabRunner,
    LabRunnerError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domoai-lab", description="Operate the local DomoAI lab")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--compose-file", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--docker-bin", default=None)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    up = subparsers.add_parser(
        "up", help="start selected Compose services and optional WSL bridge"
    )
    up.add_argument(
        "--services",
        nargs="+",
        choices=sorted(SERVICE_NAMES),
        default=DEFAULT_UP_SERVICES,
    )

    status = subparsers.add_parser("status", help="show Compose and local service status")
    status.add_argument("--services", nargs="*", choices=sorted(SERVICE_NAMES), default=())

    down = subparsers.add_parser("down", help="stop the Compose lab")
    down.add_argument("--volumes", action="store_true", help="remove lab volumes explicitly")

    subparsers.add_parser("smoke", help="run deterministic fixture-only tests")
    return parser


def _repo_root(candidate: Path | None) -> Path:
    start = (candidate or Path.cwd()).resolve()
    candidates = (start, *start.parents)
    for root in candidates:
        if (root / "dev" / "lab" / "compose.yaml").is_file():
            return root
    raise LabRunnerError("could not locate dev/lab/compose.yaml from the requested repository root")


def _config(args: argparse.Namespace) -> LabConfig:
    root = _repo_root(args.repo_root)
    compose_file = args.compose_file
    if compose_file is not None and not compose_file.is_absolute():
        compose_file = root / compose_file
    env_file = args.env_file
    if env_file is not None and not env_file.is_absolute():
        env_file = root / env_file
    return LabConfig(
        repo_root=root,
        compose_file=compose_file,
        env_file=env_file,
        docker_executable=args.docker_bin or os.getenv("DOMOAI_DOCKER_BIN", "docker"),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runner = LabRunner(_config(args))
        if args.operation == "up":
            return runner.up(args.services)
        if args.operation == "status":
            return runner.status(args.services)
        if args.operation == "down":
            return runner.down(remove_volumes=args.volumes)
        if args.operation == "smoke":
            return runner.smoke()
        raise LabRunnerError(f"unsupported lab operation: {args.operation}")
    except LabRunnerError as error:
        print(f"domoai-lab: {error}", file=sys.stderr)
        return 2
