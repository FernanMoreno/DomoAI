"""Attended JSONL bridge for final-hop gateway fencing qualification."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from uuid import uuid4

from pydantic import ValidationError

from domoai.domain.coordination import FencingToken
from domoai.domain.multihost_qualification import (
    GatewayFencingProbeRequest,
    GatewayFencingProbeResult,
)


class GatewayFencingProbeError(RuntimeError):
    """The external bridge could not prove final-hop epoch enforcement."""


class JsonlFencingGatewayBridge:
    """Invoke an operator-owned fencing probe executable without a shell."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 10.0) -> None:
        if not command or any(not item.strip() for item in command):
            raise ValueError("gateway bridge command must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("gateway bridge timeout must be positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    async def probe(
        self, token: FencingToken, *, safe_command: str
    ) -> GatewayFencingProbeResult:
        request = GatewayFencingProbeRequest(
            probe_id=uuid4(),
            scope=token.scope,
            fencing_epoch=token.epoch,
            lease_id=token.lease_id,
            safe_command=safe_command,
        )
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # asyncio guarantees both pipes for the configured subprocess streams.
            if process.stdin is None or process.stdout is None:  # pragma: no cover
                raise GatewayFencingProbeError("gateway bridge pipes are unavailable")
            process.stdin.write(
                (json.dumps(request.model_dump(mode="json"), separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )
            await asyncio.wait_for(process.stdin.drain(), timeout=self.timeout_seconds)
            process.stdin.close()
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout_seconds)
            return_code = await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise GatewayFencingProbeError("gateway bridge timed out") from error
        if return_code != 0:
            raise GatewayFencingProbeError("gateway bridge exited unsuccessfully")
        if not raw:
            raise GatewayFencingProbeError("gateway bridge returned no response")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GatewayFencingProbeError("gateway bridge returned invalid JSON") from error
        try:
            result = GatewayFencingProbeResult.model_validate(payload)
        except ValidationError as error:
            raise GatewayFencingProbeError("gateway bridge returned an invalid response") from error
        if result.probe_id != request.probe_id:
            raise GatewayFencingProbeError(
                "gateway bridge response probe_id does not match request"
            )
        return result


__all__ = ["GatewayFencingProbeError", "JsonlFencingGatewayBridge"]
