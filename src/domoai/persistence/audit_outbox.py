"""Bounded delivery of durable critical-audit outbox entries."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from domoai.domain.models import AuditEvent
from domoai.persistence.repositories import AuditEventRepository


@dataclass(frozen=True)
class AuditOutboxDispatchResult:
    delivered: int = 0
    retried: int = 0


class AuditOutboxDispatcher:
    """Deliver pending entries at most once per successful acknowledgement.

    The repository remains the source of truth. A failed delivery increments
    its retry counter and leaves the entry pending, so a later pass can retry
    without duplicating the original audit event.
    """

    def __init__(
        self,
        repository: AuditEventRepository,
        deliver: Callable[[AuditEvent], Awaitable[Any] | Any],
    ) -> None:
        self.repository = repository
        self.deliver = deliver

    async def dispatch_once(self, *, limit: int = 100) -> AuditOutboxDispatchResult:
        delivered = 0
        retried = 0
        for event in await self.repository.list_pending_outbox(limit):
            try:
                result = self.deliver(event)
                if inspect.isawaitable(result):
                    await result
                if await self.repository.mark_outbox_delivered(event.id):
                    delivered += 1
            except Exception as error:
                if await self.repository.record_outbox_retry(event.id, str(error)):
                    retried += 1
        return AuditOutboxDispatchResult(delivered=delivered, retried=retried)
