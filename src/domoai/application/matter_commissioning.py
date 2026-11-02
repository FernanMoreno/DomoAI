"""Bounded Matter commissioning orchestration with explicit authority."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from domoai.domain.p1_completion import (
    CommissioningMethod,
    CommissioningState,
    MatterCommissioningSession,
)


class MatterCommissioningController(Protocol):
    async def commission(self, method: CommissioningMethod, code: str) -> Any: ...

    async def cleanup(self, session_id: str) -> None: ...


class InMemoryMatterCommissioningController:
    """Deterministic controller used by commissioning unit/integration tests."""

    def __init__(
        self,
        *,
        result: Any = 1,
        delay: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.delay = delay
        self.error = error
        self.commission_calls: list[tuple[CommissioningMethod, str]] = []
        self.cleanup_calls: list[str] = []

    async def commission(self, method: CommissioningMethod, code: str) -> Any:
        self.commission_calls.append((method, code))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result

    async def cleanup(self, session_id: str) -> None:
        self.cleanup_calls.append(session_id)


class MatterCommissioningService:
    """Own commissioning session state and never persist the pairing code."""

    def __init__(
        self,
        controller: MatterCommissioningController,
        *,
        timeout: float = 120.0,
        max_sessions: int = 32,
    ) -> None:
        if timeout <= 0 or max_sessions <= 0:
            raise ValueError("commissioning bounds must be positive")
        self.controller = controller
        self.timeout = timeout
        self.max_sessions = max_sessions
        self._sessions: dict[str, MatterCommissioningSession] = {}
        self._codes: dict[str, str] = {}
        self._active_by_connection: set[str] = set()
        self._cleanup_started: set[str] = set()

    async def start(
        self,
        connection_id: str,
        *,
        method: CommissioningMethod,
        code: str,
        can_commission: bool,
        prerequisites: Mapping[str, bool],
        confirmed: bool = False,
    ) -> MatterCommissioningSession:
        if not can_commission:
            raise PermissionError("Matter commissioning authority is required")
        self._validate_code(method, code)
        self._validate_prerequisites(prerequisites)
        if connection_id in self._active_by_connection:
            raise RuntimeError("Matter commissioning session is already active")
        if len(self._sessions) >= self.max_sessions:
            raise RuntimeError("Matter commissioning session capacity is exhausted")
        now = datetime.now(UTC)
        session = MatterCommissioningSession(
            session_id=(
                f"matter-commissioning:{len(self._sessions) + 1}:"
                f"{int(now.timestamp() * 1000)}"
            ),
            connection_id=connection_id,
            method=method,
            state=CommissioningState.AWAITING_CONFIRMATION,
            deadline=now + timedelta(seconds=self.timeout),
            confirmation_required=True,
        )
        self._sessions[session.session_id] = session
        self._codes[session.session_id] = code
        self._active_by_connection.add(connection_id)
        return await self.confirm(session.session_id) if confirmed else session

    async def confirm(self, session_id: str) -> MatterCommissioningSession:
        session = self._require(session_id)
        if session.state is not CommissioningState.AWAITING_CONFIRMATION:
            return session
        updated = session.model_copy(
            update={"state": CommissioningState.RUNNING, "confirmation_required": False}
        )
        self._sessions[session_id] = updated
        code = self._codes.get(session_id)
        if code is None:
            return await self._finish(session_id, CommissioningState.FAILED)
        try:
            await asyncio.wait_for(self.controller.commission(updated.method, code), self.timeout)
        except asyncio.CancelledError:
            await self._finish(session_id, CommissioningState.FAILED)
            raise
        except TimeoutError:
            return await self._finish(session_id, CommissioningState.EXPIRED)
        except (ConnectionError, OSError, ValueError, RuntimeError):
            return await self._finish(session_id, CommissioningState.FAILED)
        except Exception:
            return await self._finish(session_id, CommissioningState.FAILED)
        return await self._finish(session_id, CommissioningState.COMPLETED)

    async def cleanup(self, session_id: str) -> MatterCommissioningSession:
        session = self._require(session_id)
        if session.cleanup_done:
            return session
        await self._cleanup_once(session_id)
        updated = session.model_copy(update={"cleanup_done": True})
        self._sessions[session_id] = updated
        return updated

    def get(self, session_id: str) -> MatterCommissioningSession:
        return self._require(session_id)

    async def _finish(
        self, session_id: str, state: CommissioningState
    ) -> MatterCommissioningSession:
        session = self._require(session_id)
        cleanup_done = True
        try:
            await self._cleanup_once(session_id)
        except Exception:
            cleanup_done = False
        finally:
            self._codes.pop(session_id, None)
            self._active_by_connection.discard(session.connection_id)
        updated = session.model_copy(update={"state": state, "cleanup_done": cleanup_done})
        self._sessions[session_id] = updated
        return updated

    async def _cleanup_once(self, session_id: str) -> None:
        if session_id in self._cleanup_started:
            return
        self._cleanup_started.add(session_id)
        try:
            result = self.controller.cleanup(session_id)
            if inspect.isawaitable(result):
                await cast(Awaitable[None], result)
        finally:
            # A retry after a cleanup exception is safe and can still release
            # a resource if the controller's first attempt was interrupted.
            self._cleanup_started.discard(session_id)

    def _require(self, session_id: str) -> MatterCommissioningSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("Matter commissioning session was not found")
        return session

    @staticmethod
    def _validate_code(method: CommissioningMethod, code: str) -> None:
        if not isinstance(code, str) or len(code) > 256 or not code.strip():
            raise ValueError("Matter commissioning code is invalid")
        if method is CommissioningMethod.QR:
            if not code.startswith("MT:") or not re.fullmatch(r"MT:[A-Z0-9.\-]+", code):
                raise ValueError("Matter QR code is invalid")
        elif not re.fullmatch(r"\d{8,20}", code):
            raise ValueError("Matter manual code is invalid")

    @staticmethod
    def _validate_prerequisites(prerequisites: Mapping[str, bool]) -> None:
        if prerequisites.get("bluetooth_available") is not True:
            raise ValueError("Matter commissioning prerequisite bluetooth is unavailable")
        if not (
            prerequisites.get("wifi_credentials") is True
            or prerequisites.get("thread_credentials") is True
        ):
            raise ValueError("Matter commissioning prerequisite network credentials are missing")


__all__ = [
    "InMemoryMatterCommissioningController",
    "MatterCommissioningController",
    "MatterCommissioningService",
]
