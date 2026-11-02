"""Opt-in client-attested confirmation; never infer consent from tool arguments."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Device, Plan

_RECENT_CODES = 1000


class ChatSummaryUnavailable(ValueError):
    """An effect cannot be represented faithfully in the confirmation."""


def _field(value: object) -> str:
    text = str(value)
    if len(text) > 120 or any(
        unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in text
    ):
        raise ChatSummaryUnavailable("Unsafe confirmation field")
    # Variable-length code delimiters make Markdown/HTML literal, including backticks.
    delimiter = "`"
    while delimiter in text:
        delimiter += "`"
    return f"{delimiter} {json.dumps(text, ensure_ascii=False)} {delimiter}"


def render_chat_summary(plan: Plan, devices: list[Device], now: datetime, timezone: str) -> str:
    """Render every effect, schedule and expiry; never truncate or omit an effect."""
    del now  # The rendered intent is independent of the advancing wall clock.
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ChatSummaryUnavailable("Confirmation timezone unavailable") from error
    if not 1 <= len(plan.commands) <= 10:
        raise ChatSummaryUnavailable("Confirmation needs one to ten effects")
    known = {device.id: device for device in devices}
    lines = ["Confirma los siguientes efectos:"]
    for command in plan.commands:
        device = known.get(command.device_id)
        if device is None:
            raise ChatSummaryUnavailable("Unknown confirmation device")
        capabilities = [
            item
            for item in device.capabilities
            if item.writable and command.command in item.commands
        ]
        if len(capabilities) != 1:
            raise ChatSummaryUnavailable("Ambiguous or unknown command capability")
        unit = command.unit if command.unit is not None else capabilities[0].unit
        lines.append(
            f"Dispositivo {_field(device.name)}; tipo {_field(device.type.value)}; "
            f"área {_field(device.area_id or 'sin área')}; "
            f"comando {_field(command.command)}; valor {_field(command.value)}; "
            f"unidad {_field(unit or 'sin unidad')}."
        )

    def instant(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ChatSummaryUnavailable("Naive confirmation timestamp")
        return value.astimezone(zone).isoformat()

    if plan.execution_window is not None:
        window = plan.execution_window
        lines.append(
            f"Ejecución programada: {instant(window.not_before)} – {instant(window.not_after)}."
        )
    elif plan.execute_at is not None:
        lines.append(f"Ejecución programada para {instant(plan.execute_at)}.")
    else:
        lines.append("Cuándo: ejecución inmediata.")
    lines.append(f"Zona horaria: {_field(timezone)}.")
    if plan.expires_at is not None:
        lines.append(f"Caducidad del plan: {instant(plan.expires_at)}.")
    if plan.validation is not None and plan.validation.valid_until is not None:
        lines.append(f"Caducidad de la validación: {instant(plan.validation.valid_until)}.")
    for decision in plan.policy_decisions:
        lines.append(f"Riesgo: {_field(decision.reason)}.")
    return "\n".join(lines)


def intent_digest(plan: Plan) -> str:
    """Bind undisplayed command fields and all authority/validation evidence too."""
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


def unavailable(reason: str = "unavailable") -> DomainError:
    return DomainError(
        ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE,
        "No se ha confirmado la acción por el cliente MCP",
        details={"reason": reason},
    )


class ConfirmApproval(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    aprobar: bool = Field(default=False, description="Aprobar los efectos mostrados")
    codigo: str = Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")


@dataclass
class ChatConfirmationGate:
    """One dialogue per household, with failure cooldown and nonreused codes."""

    _locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    _cooldowns: dict[tuple[str, str], float] = field(default_factory=dict)
    # Only the most recent codes are excluded: unbounded memory would exhaust the 9000-code
    # space after 9000 dialogues and disable chat confirmation until restart.
    _codes: deque[str] = field(default_factory=lambda: deque(maxlen=_RECENT_CODES))

    @asynccontextmanager
    async def hold(self, scope: tuple[str, str]) -> AsyncIterator[None]:
        lock = self._locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            raise unavailable("busy")
        if time.monotonic() < self._cooldowns.get(scope, 0):
            raise unavailable("cooldown")
        async with lock:
            yield

    async def confirm(
        self,
        ctx: Context[Any, Any] | None,
        message: str,
        *,
        scope: tuple[str, str],
        timeout: float,
    ) -> None:
        try:
            elicitation = ctx.session.client_params.capabilities.elicitation if ctx else None
            if elicitation is None or (elicitation.form is None and elicitation.url is not None):
                raise unavailable()
        except (AttributeError, ValueError) as error:
            raise unavailable() from error
        recent = set(self._codes)
        code = secrets.choice(
            [f"{value:04d}" for value in range(1000, 10000) if f"{value:04d}" not in recent]
        )
        self._codes.append(code)
        try:
            assert ctx is not None
            with anyio.fail_after(timeout):
                result = await ctx.elicit(
                    message=f"Código de verificación: {code}\n{message}", schema=ConfirmApproval
                )
        except TimeoutError:
            raise unavailable("timeout") from None
        except Exception:
            raise unavailable() from None
        if result.action == "cancel":
            answer = "cancelled"
        elif (
            result.action == "accept"
            and result.data.aprobar is True
            and hmac.compare_digest(result.data.codigo.encode("utf-8"), code.encode("ascii"))
        ):
            return
        else:
            answer = "declined"
        self._cooldowns[scope] = time.monotonic() + 60
        raise unavailable(answer)
