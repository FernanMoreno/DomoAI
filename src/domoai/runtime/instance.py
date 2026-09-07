"""Identity of one runtime process for safe operational correlation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    instance_id: str
    process_start_time: datetime

    def __post_init__(self) -> None:
        if not self.instance_id.strip() or len(self.instance_id) > 128:
            raise ValueError("instance_id must be between 1 and 128 characters")
        if self.process_start_time.tzinfo is None or self.process_start_time.utcoffset() is None:
            raise ValueError("process_start_time must be timezone-aware")
        object.__setattr__(self, "process_start_time", self.process_start_time.astimezone(UTC))

    @classmethod
    def create(
        cls, instance_id: str | None = None, *, clock: Clock | None = None
    ) -> InstanceIdentity:
        return cls(
            instance_id=instance_id or f"instance-{uuid4().hex}",
            process_start_time=(clock or SystemClock()).now(),
        )
