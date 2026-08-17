"""Safe error envelopes for the agent-facing MCP surface."""

from __future__ import annotations

from pydantic import ValidationError

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import ErrorDetail
from domoai.optimizer.providers import EnergyProviderError


def error_envelope(error: Exception) -> dict[str, object]:
    if isinstance(error, DomainError):
        detail = error.as_detail()
    elif isinstance(error, EnergyProviderError):
        diagnostic = error.diagnostic
        detail = ErrorDetail(
            code=diagnostic.code,
            message=diagnostic.message,
            retryable=diagnostic.retryable,
            details={"provider_id": diagnostic.provider_id, **diagnostic.details},
        )
    elif isinstance(error, ValidationError):
        detail = ErrorDetail(
            code=ErrorCode.VALIDATION_ERROR,
            message="Input does not satisfy the v1 contract",
            details={
                "fields": [
                    {"location": list(item.get("loc", ())), "type": item.get("type", "")}
                    for item in error.errors()
                ]
            },
        )
    else:
        detail = ErrorDetail(
            code=ErrorCode.VALIDATION_ERROR,
            message="Request could not be processed",
        )
    return {"error": detail.model_dump(mode="json")}
