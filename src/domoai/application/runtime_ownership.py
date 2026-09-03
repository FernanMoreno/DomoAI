"""Application-owned lease for the one active physical runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from domoai.config.settings import Settings
from domoai.persistence.repositories import (
    RuntimeOwnershipConflict,
    RuntimeOwnershipRepository,
)
from domoai.persistence.sqlite import SQLiteAdvisoryLock


def runtime_config_digest(settings: Settings, *, adapter_id: str) -> str:
    """Hash non-secret deployment coordinates used by the active runtime."""

    payload = {
        "adapter_id": adapter_id,
        "database_path": str(settings.database_path),
        "deployment_id": settings.mcp_deployment_id,
        "mcp_host": settings.mcp_host,
        "mcp_port": settings.mcp_port,
        "mcp_path": settings.mcp_path,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class RuntimeOwnership:
    """A server-owned, explicitly released deployment lease."""

    repository: RuntimeOwnershipRepository
    deployment_id: str
    owner_id: str
    advisory_lock: SQLiteAdvisoryLock | None = None
    released: bool = False

    @classmethod
    async def acquire(
        cls,
        repository: RuntimeOwnershipRepository,
        settings: Settings,
        *,
        adapter_id: str,
    ) -> RuntimeOwnership:
        advisory_lock = repository.database.advisory_lock()
        try:
            await asyncio.to_thread(advisory_lock.acquire, blocking=False)
        except BlockingIOError as error:
            raise RuntimeOwnershipConflict(
                f"runtime ownership for {settings.mcp_deployment_id} is active"
            ) from error
        owner = cls(
            repository=repository,
            deployment_id=settings.mcp_deployment_id,
            owner_id=uuid4().hex,
            advisory_lock=advisory_lock,
        )
        try:
            await repository.acquire(
                deployment_id=owner.deployment_id,
                owner_id=owner.owner_id,
                config_digest=runtime_config_digest(settings, adapter_id=adapter_id),
            )
        except BaseException:
            advisory_lock.release()
            raise
        return owner

    async def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        try:
            return await self.repository.release(
                deployment_id=self.deployment_id,
                owner_id=self.owner_id,
            )
        finally:
            if self.advisory_lock is not None:
                self.advisory_lock.release()
