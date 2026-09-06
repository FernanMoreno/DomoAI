"""Scoped, secret-free household export and deletion operations."""

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.domain.privacy import (
    HouseholdDataPolicy,
    PrivacyCategory,
    PrivacyDeletion,
    PrivacyExport,
)
from domoai.runtime.state_store import StateStore


class PrivacyStore(Protocol):
    async def export_category(
        self, category: PrivacyCategory, household_id: str
    ) -> list[dict[str, Any]]: ...

    async def delete_category(self, category: PrivacyCategory, household_id: str) -> int: ...


class PrivacyService:
    """Apply a policy without exposing raw credentials or crossing households."""

    def __init__(
        self,
        store: PrivacyStore,
        *,
        audit: Callable[..., object] | None = None,
        page_limit: int = 512,
        state_store: StateStore | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.state_store = state_store
        if page_limit < 1 or page_limit > 1024:
            raise ValueError("privacy export page_limit must be between 1 and 1024")
        self.page_limit = page_limit
        self._cursor_secret = secrets.token_bytes(32)

    async def export(
        self,
        policy: HouseholdDataPolicy,
        requester: AuthorityContext,
        *,
        categories: Sequence[PrivacyCategory | str],
        cursor: str | None = None,
        limit: int | None = None,
    ) -> PrivacyExport:
        selected = self._authorize(policy, requester, categories, operation="export")
        page_limit = self.page_limit if limit is None else limit
        if page_limit < 1 or page_limit > 1024:
            raise ValueError("privacy export limit must be between 1 and 1024")
        offset, cursor_categories = self._decode_cursor(cursor, requester.household_id)
        if cursor_categories is not None and cursor_categories != [
            item.value for item in selected
        ]:
            raise ValueError("privacy export cursor does not match the requested categories")
        counts = [
            await self._count_category(category, requester.household_id)
            for category in selected
        ]
        total_count = sum(counts)
        if offset > total_count:
            raise ValueError("privacy export cursor is outside the available data")
        records: list[dict[str, object]] = []
        remaining = page_limit
        skipped = offset
        for category, category_count in zip(selected, counts, strict=True):
            if skipped >= category_count:
                skipped -= category_count
                continue
            chunk = await self._export_category_page(
                category,
                requester.household_id,
                offset=skipped,
                limit=remaining,
            )
            records.extend(_redact(record) for record in chunk)
            remaining -= len(chunk)
            skipped = 0
            if remaining == 0:
                break
        if offset == 0 and total_count <= page_limit:
            records.sort(key=_canonical_record)
        next_offset = offset + len(records)
        next_cursor = (
            self._encode_cursor(requester.household_id, selected, next_offset)
            if next_offset < total_count
            else None
        )
        return PrivacyExport(
            household_id=requester.household_id,
            categories=selected,
            records=records,
            record_count=len(records),
            total_record_count=total_count,
            next_cursor=next_cursor,
        )

    async def delete(
        self,
        policy: HouseholdDataPolicy,
        requester: AuthorityContext,
        *,
        categories: Sequence[PrivacyCategory | str],
        request_id: str,
    ) -> PrivacyDeletion:
        selected = self._authorize(policy, requester, categories, operation="delete")
        delete_categories = getattr(self.store, "delete_categories", None)
        if callable(delete_categories):
            deleted_counts = await delete_categories(selected, requester.household_id)
        else:
            deleted_counts = {
                category.value: await self.store.delete_category(category, requester.household_id)
                for category in selected
            }
        if self.state_store is not None and PrivacyCategory.STATE in selected:
            await self.state_store.forget_household(requester.household_id)
        result = PrivacyDeletion(
            request_id=request_id,
            household_id=requester.household_id,
            categories=selected,
            deleted_counts=deleted_counts,
            immutable_preserved=list(policy.immutable_categories),
        )
        if self.audit is not None:
            self.audit(
                event_type="privacy_data_deleted",
                actor=requester.principal_id,
                subject_id=request_id,
                payload={
                    "household_id": requester.household_id,
                    "categories": [category.value for category in selected],
                    "deleted_counts": deleted_counts,
                },
                authority=requester,
            )
        return result

    async def _count_category(self, category: PrivacyCategory, household_id: str) -> int:
        count_category = getattr(self.store, "count_category", None)
        if callable(count_category):
            return int(await count_category(category, household_id))
        return len(await self.store.export_category(category, household_id))

    async def _export_category_page(
        self,
        category: PrivacyCategory,
        household_id: str,
        *,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        export_page = getattr(self.store, "export_category_page", None)
        if callable(export_page):
            return cast(
                list[dict[str, Any]],
                await export_page(
                    category,
                    household_id,
                    offset=offset,
                    limit=limit,
                ),
            )
        return (await self.store.export_category(category, household_id))[offset : offset + limit]

    def _encode_cursor(
        self,
        household_id: str,
        categories: Sequence[PrivacyCategory],
        offset: int,
    ) -> str:
        payload = {
            "household_id": household_id,
            "categories": [category.value for category in categories],
            "offset": offset,
        }
        encoded = _urlsafe_json(payload)
        signature = hmac.new(self._cursor_secret, encoded, hashlib.sha256).hexdigest()
        return f"{encoded.decode('ascii')}.{signature}"

    def _decode_cursor(
        self, cursor: str | None, household_id: str
    ) -> tuple[int, list[str] | None]:
        if cursor is None:
            return 0, None
        try:
            encoded_text, signature = cursor.rsplit(".", 1)
            encoded = encoded_text.encode("ascii")
            expected = hmac.new(self._cursor_secret, encoded, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            if payload.get("household_id") != household_id:
                raise ValueError("wrong household")
            offset = payload.get("offset")
            categories = payload.get("categories")
            if not isinstance(offset, int) or offset < 0 or not isinstance(categories, list):
                raise ValueError("invalid cursor payload")
            if not all(isinstance(item, str) for item in categories):
                raise ValueError("invalid cursor categories")
            return offset, categories
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("invalid privacy export cursor") from error

    @staticmethod
    def _authorize(
        policy: HouseholdDataPolicy,
        requester: AuthorityContext,
        categories: Sequence[PrivacyCategory | str],
        *,
        operation: str,
    ) -> list[PrivacyCategory]:
        if (
            requester.tenant_id != policy.authority.tenant_id
            or requester.household_id != policy.authority.household_id
        ):
            raise PermissionError("privacy request is outside the authorized household")
        if not set(requester.roles) & {PrincipalRole.OWNER, PrincipalRole.SERVICE}:
            raise PermissionError("privacy request requires owner or trusted service role")
        selected = sorted(
            {
                item if isinstance(item, PrivacyCategory) else PrivacyCategory(item)
                for item in categories
            },
            key=lambda item: item.value,
        )
        if not selected:
            raise ValueError("privacy request requires at least one category")
        allowed = (
            policy.exportable_categories
            if operation == "export"
            else policy.deletable_categories
        )
        forbidden = [category for category in selected if category not in allowed]
        if forbidden:
            if any(category in policy.immutable_categories for category in forbidden):
                raise PermissionError("privacy category is immutable")
            raise PermissionError("privacy category is not allowed by policy")
        return selected


_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "password",
    "secret",
    "authorization",
    "credential",
    "api_key",
    "cookie",
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if not any(
                fragment in str(key).casefold().replace("-", "_")
                for fragment in _SENSITIVE_KEY_FRAGMENTS
            )
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _canonical_record(record: dict[str, object]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _urlsafe_json(payload: dict[str, object]) -> bytes:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


__all__ = ["PrivacyService", "PrivacyStore"]
