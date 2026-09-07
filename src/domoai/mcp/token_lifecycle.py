"""Durable, atomic bearer-token rotation for the configured MCP token file."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from domoai.domain.models import PrincipalRole
from domoai.mcp.auth import ClientTokenDocument, ClientTokenRecord


@dataclass(frozen=True)
class TokenRotationResult:
    """The raw bearer is deliberately available only to the admin caller."""

    client_id: str
    token: str
    created_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"TokenRotationResult(client_id={self.client_id!r}, token='[REDACTED]', "
            f"created_at={self.created_at!r}, expires_at={self.expires_at!r})"
        )


class TokenFileManager:
    """Manage a deployment-owned token document without persisting bearers."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rotate(
        self,
        client_id: str,
        *,
        scopes: list[str] | None = None,
        ttl: timedelta = timedelta(days=30),
        tenant_id: str = "default",
        household_ids: list[str] | None = None,
        roles: list[PrincipalRole] | None = None,
        area_ids: list[str] | None = None,
        device_ids: list[str] | None = None,
        capabilities: list[str] | None = None,
        operations: list[str] | None = None,
    ) -> TokenRotationResult:
        if not client_id.strip() or ttl <= timedelta(0):
            raise ValueError("token client_id and positive ttl are required")
        now = datetime.now(UTC)
        expires_at = now + ttl
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        existing = self._read()
        records = [record for record in existing.clients if record.client_id != client_id]
        records.append(
            ClientTokenRecord(
                client_id=client_id,
                token_hash=token_hash,
                scopes=list(scopes or []),
                created_at=now,
                expires_at=expires_at,
                tenant_id=tenant_id,
                household_ids=list(household_ids or ["default"]),
                roles=list(roles or []),
                area_ids=list(area_ids or []),
                device_ids=list(device_ids or []),
                capabilities=list(capabilities or []),
                operations=list(operations or []),
            )
        )
        self._write(ClientTokenDocument(clients=records))
        return TokenRotationResult(client_id, raw_token, now, expires_at)

    def revoke(self, client_id: str) -> bool:
        records = list(self._read().clients)
        for index, record in enumerate(records):
            if record.client_id != client_id:
                continue
            if record.revoked_at is not None or not record.enabled:
                return False
            records[index] = record.model_copy(
                update={"enabled": False, "revoked_at": datetime.now(UTC)}
            )
            self._write(ClientTokenDocument(clients=records))
            return True
        return False

    def records(self) -> tuple[ClientTokenRecord, ...]:
        return tuple(self._read().clients)

    def _read(self) -> ClientTokenDocument:
        if self.path.is_symlink() or not self.path.is_file():
            return ClientTokenDocument(clients=[])
        try:
            return ClientTokenDocument.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("invalid MCP client token file") from error

    def _write(self, document: ClientTokenDocument) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("MCP client token file cannot be a symlink")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document.model_dump(mode="json"), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
