"""Command-line entry point for battery hardware-in-the-loop qualification.

    domoai-hil battery \\
        --settings-env production.env \\
        --hardware-id inverter-serial-1234 \\
        --firmware-version 2.4.1 \\
        --test-charge-kw 0.5 --test-discharge-kw 0.5 \\
        --attest native_scheduler_conflict="native scheduler disabled, verified via vendor app" \\
        --attest restart_no_replay="process restarted mid-sequence, no duplicate" \\
        --output evidence/battery-hil-2026-08-24.json

Runs the real check sequence against the adapter configured by `--settings-env`
(a dotenv-style file consumed the same way the MCP server reads its runtime
configuration) and writes `BatteryHILEvidence` built from what actually
happened, not a hand-typed attestation. See `domoai.hil.runner` for exactly
which checks are automated versus which require the `--attest` flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from domoai.application.runtime_factory import build_runtime
from domoai.config.battery_profile import load_dispatchable_battery_binding
from domoai.config.settings import Settings
from domoai.hil.runner import BatteryHILRunError, run_battery_hil


def _git_sha() -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _parse_attestations(pairs: list[str]) -> dict[str, str]:
    attestations: dict[str, str] = {}
    for pair in pairs:
        key, sep, note = pair.partition("=")
        if not sep or not key.strip() or not note.strip():
            raise ValueError(f"--attest must be check=note, got: {pair!r}")
        attestations[key.strip()] = note.strip()
    return attestations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domoai-hil", description="Hardware-in-the-loop qualification runner"
    )
    subparsers = parser.add_subparsers(dest="target", required=True)

    battery = subparsers.add_parser(
        "battery", help="run the battery dispatch HIL sequence against a live adapter"
    )
    battery.add_argument(
        "--settings-env",
        type=Path,
        default=None,
        help="dotenv file with the same DOMOAI_* variables the runtime reads",
    )
    battery.add_argument(
        "--battery-profile",
        type=Path,
        default=None,
        help="override the dispatchable battery profile path (defaults to settings)",
    )
    battery.add_argument("--hardware-id", required=True)
    battery.add_argument("--firmware-version", required=True)
    battery.add_argument("--test-charge-kw", type=float, required=True)
    battery.add_argument("--test-discharge-kw", type=float, required=True)
    battery.add_argument(
        "--attest",
        action="append",
        default=[],
        metavar="CHECK=NOTE",
        help="manual attestation for a check the runner cannot self-certify "
        "(required: native_scheduler_conflict, restart_no_replay)",
    )
    battery.add_argument("--output", type=Path, required=True)
    return parser


async def _run_battery(args: argparse.Namespace) -> int:
    if args.settings_env is not None:
        for line in args.settings_env.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    settings = Settings.from_environment()
    profile_path = args.battery_profile or settings.battery_dispatch_profile_path
    if profile_path is None:
        print(
            "error: no battery profile configured (--battery-profile or settings)",
            file=sys.stderr,
        )
        return 2
    binding = load_dispatchable_battery_binding(profile_path)

    try:
        attestations = _parse_attestations(args.attest)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    # The CLI-selected profile is the exact binding under test. Remove the
    # settings path so the composition root cannot silently build profile A
    # while the runner exercises profile B.
    runtime_settings = settings.model_copy(update={"battery_dispatch_profile_path": None})
    runtime = await build_runtime(
        runtime_settings,
        dispatchable_battery_binding=binding,
    )
    try:
        evidence = await run_battery_hil(
            runtime,
            binding=binding,
            test_charge_kw=args.test_charge_kw,
            test_discharge_kw=args.test_discharge_kw,
            hardware_id=args.hardware_id,
            firmware_version=args.firmware_version,
            test_software_version=_git_sha(),
            manual_attestations=attestations,
        )
    except BatteryHILRunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        await runtime.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"{evidence.status}: {args.output} (run_id={evidence.run_id})")
    return 0 if evidence.status == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.target == "battery":
        return asyncio.run(_run_battery(args))
    parser.error(f"unknown target: {args.target}")
    return 2  # pragma: no cover - argparse.error exits the process


if __name__ == "__main__":
    raise SystemExit(main())
