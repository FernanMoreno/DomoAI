"""Signed, non-actuating control intentions for future home federation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel


class FederationIntentStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    COMPLETED = "completed"


class FederationIntent(StrictModel):
    """A signed request envelope; it contains no bearer or raw commands."""

    schema_version: str = "v1"
    intent_id: str = Field(min_length=1, max_length=200)
    source_tenant_id: str = Field(min_length=1, max_length=200)
    source_household_id: str = Field(min_length=1, max_length=200)
    target_tenant_id: str = Field(min_length=1, max_length=200)
    target_household_id: str = Field(min_length=1, max_length=200)
    principal_id: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=100)
    plan_digest: str | None = Field(default=None, min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    issued_at: datetime
    expires_at: datetime
    signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lifetime(self) -> FederationIntent:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise ValueError("federation issued_at must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("federation expires_at must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("federation intent must expire after issuance")
        return self

    def signing_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return canonical.encode("utf-8")


def sign_federation_intent(intent: FederationIntent, shared_secret: bytes) -> FederationIntent:
    if not shared_secret:
        raise ValueError("federation shared secret is required")
    signature = hmac.new(shared_secret, intent.signing_payload(), hashlib.sha256).hexdigest()
    return intent.model_copy(update={"signature": signature})


def verify_federation_signature(intent: FederationIntent, shared_secret: bytes) -> bool:
    if not shared_secret:
        return False
    expected = hmac.new(shared_secret, intent.signing_payload(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, intent.signature)
