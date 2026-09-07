#!/usr/bin/env python3
"""Stateful, disposable JSONL fencing bridge for the Docker qualification lab."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from domoai.domain.multihost_qualification import (
    GatewayFencingProbeRequest,
    GatewayFencingProbeResult,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DomoAI disposable lab fencing bridge")
    parser.add_argument(
        "--state-file",
        default=os.environ.get("DOMOAI_LAB_FENCING_STATE_FILE"),
        help="lab-local path for the highest accepted epoch per lease scope",
    )
    args = parser.parse_args()
    if not args.state_file:
        parser.error("--state-file or DOMOAI_LAB_FENCING_STATE_FILE is required")
    return args


def _read_request() -> GatewayFencingProbeRequest:
    line = sys.stdin.buffer.readline()
    if not line or sys.stdin.buffer.readline():
        raise ValueError("expected exactly one JSONL request")
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request is not valid JSON") from error
    try:
        return GatewayFencingProbeRequest.model_validate(payload)
    except ValidationError as error:
        raise ValueError("request does not match the fencing probe contract") from error


def _load_state(state_file: Path) -> dict[str, int]:
    if not state_file.exists():
        return {}
    if state_file.is_symlink() or not state_file.is_file():
        raise ValueError("state file is not a regular file")
    try:
        payload: Any = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("state file is invalid") from error
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, int) or value < 1
        for key, value in payload.items()
    ):
        raise ValueError("state file is invalid")
    return payload


def _write_state(state_file: Path, state: dict[str, int]) -> None:
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=state_file.parent, prefix=f".{state_file.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, state_file)
        parent_descriptor = os.open(state_file.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _scope_key(request: GatewayFencingProbeRequest) -> str:
    return json.dumps(request.scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _respond(request: GatewayFencingProbeRequest, state_file: Path) -> GatewayFencingProbeResult:
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = state_file.with_name(f".{state_file.name}.lock")
    with lock_file.open("a+", encoding="utf-8") as lock_handle:
        os.chmod(lock_file, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_file)
        key = _scope_key(request)
        observed_epoch = state.get(key, 0)
        accepted = request.fencing_epoch > observed_epoch
        if accepted:
            observed_epoch = request.fencing_epoch
            state[key] = observed_epoch
            _write_state(state_file, state)
        return GatewayFencingProbeResult(
            probe_id=request.probe_id,
            accepted=accepted,
            observed_epoch=observed_epoch,
            reason=None if accepted else "stale_or_replayed_epoch",
        )


def main() -> int:
    args = _parse_args()
    try:
        request = _read_request()
        result = _respond(request, Path(args.state_file))
    except (OSError, ValueError):
        print("lab fencing bridge rejected invalid input", file=sys.stderr)
        return 2
    print(json.dumps(result.model_dump(mode="json"), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
