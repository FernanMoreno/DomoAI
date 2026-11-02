"""Stable identities for persisted connection instances and runtime sources."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from domoai.domain.models import SourceRef


@dataclass(frozen=True)
class ConnectionRuntimeIdentity:
    """Keep provider family, durable profile ID and runtime ID distinct."""

    connector_id: str
    connection_id: UUID

    @property
    def adapter_id(self) -> str:
        return runtime_adapter_id(self.connection_id)

    def source_ref(self, external_id: str) -> SourceRef:
        return SourceRef(adapter_id=self.adapter_id, external_id=external_id)


def runtime_adapter_id(connection_id: UUID) -> str:
    """Return a stable, secret-independent child adapter ID."""

    return f"connection:{connection_id}"


__all__ = ["ConnectionRuntimeIdentity", "runtime_adapter_id"]
