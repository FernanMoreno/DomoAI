"""Bounded delivery of durable critical-audit outbox entries."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from domoai.domain.models import AuditEvent
from domoai.persistence.repositories import AuditEventRepository
from domoai.runtime.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

AuditOutboxDelivery = Callable[[AuditEvent], Awaitable[Any] | Any]


@dataclass(frozen=True)
class AuditOutboxDispatchResult:
    delivered: int = 0
    retried: int = 0


class AuditOutboxDispatcher:
    """Claim and deliver pending entries with at-least-once semantics.

    The repository remains the source of truth. A callback can run after the
    destination accepted an event but before the completion CAS succeeds, so
    ``event.id`` is the destination idempotency key for any retry.
    """

    def __init__(
        self,
        repository: AuditEventRepository,
        deliver: AuditOutboxDelivery,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.deliver = deliver
        self.owner_id = owner_id or f"audit-outbox-{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.clock = clock or SystemClock()
        if not self.owner_id.strip():
            raise ValueError("outbox dispatcher owner ID must be non-empty")
        if self.lease_seconds <= 0:
            raise ValueError("outbox dispatcher lease duration must be positive")

    async def dispatch_once(self, *, limit: int = 100) -> AuditOutboxDispatchResult:
        lease_id = uuid4().hex
        events = await self.repository.claim_pending_outbox(
            limit=limit,
            owner_id=self.owner_id,
            lease_id=lease_id,
            lease_seconds=self.lease_seconds,
            now=self.clock.now(),
        )
        delivered = 0
        retried = 0
        for event in events:
            try:
                result = self.deliver(event)
                if inspect.isawaitable(result):
                    await result
                if await self.repository.mark_outbox_delivered(
                    event.id,
                    owner_id=self.owner_id,
                    lease_id=lease_id,
                    delivered_at=self.clock.now(),
                ):
                    delivered += 1
            except Exception as error:
                if await self.repository.record_outbox_retry(
                    event.id,
                    str(error),
                    owner_id=self.owner_id,
                    lease_id=lease_id,
                ):
                    retried += 1
        return AuditOutboxDispatchResult(delivered=delivered, retried=retried)


def publish_audit_outbox_event(event: AuditEvent) -> None:
    """Safe default publisher for runtimes without an external exporter.

    Payloads are deliberately excluded. A configured external publisher may
    forward the already-redacted event, but must deduplicate on ``event.id``.
    """

    logger.info(
        "audit_outbox_delivered event_id=%s event_type=%s subject_id=%s",
        event.id,
        event.event_type,
        event.subject_id,
    )


__all__ = [
    "AuditOutboxDelivery",
    "AuditOutboxDispatchResult",
    "AuditOutboxDispatcher",
    "publish_audit_outbox_event",
]
