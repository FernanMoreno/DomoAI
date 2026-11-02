"""Resident admission and ephemeral sessions; never consent or grant issuance."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import math
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, NoReturn
from urllib.parse import urlparse

import anyio

from domoai.application.authority import AuthorityPolicy
from domoai.application.household_consent import build_consent_view
from domoai.config.settings import Settings
from domoai.domain.automation import AutomationRule
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.household_consent import (
    ConsentControlVersion,
    ConsentDecision,
    ConsentReference,
    ConsentView,
)
from domoai.domain.household_preferences import PreferenceProfile
from domoai.domain.models import AuthorityContext, Device, Plan, PrincipalRole
from domoai.management.auth import is_loopback_host
from domoai.mcp.auth import CredentialSnapshot, StaticBearerTokenVerifier
from domoai.runtime.approval_store import (
    ApprovalAssertion,
    ApprovalStore,
    ConsentDecisionRecord,
    OperatorPrincipal,
)
from domoai.runtime.clock import Clock, SystemClock

_REQUIRED_PEERS = frozenset({"mcp", "management", "management-static"})
_EXPLICIT_FIELDS = frozenset(
    {"client_id", "tenant_id", "household_ids", "roles", "scopes", "operations"}
)
_ERRORS = {
    ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE: (
        503,
        "No está disponible la confirmación del residente",
    ),
    ErrorCode.INSUFFICIENT_SCOPE: (403, "La solicitud del residente no está autorizada"),
    ErrorCode.OPERATOR_AUTHENTICATION_FAILED: (401, "La credencial del residente no es válida"),
}


class ResidentAdmissionError(DomainError):
    """Safe internal failure; HTTP translation belongs to the future host."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code, _ERRORS[code][1])

    @property
    def status_code(self) -> int:
        return _ERRORS[self.code][0]


@dataclass(frozen=True, slots=True, repr=False)
class InspectableCredentialPeer:
    peer_id: str
    verifier: StaticBearerTokenVerifier


@dataclass(frozen=True, slots=True, repr=False)
class UnknownCredentialPeer:
    peer_id: str


@dataclass(frozen=True, slots=True, repr=False)
class AbsentCredentialPeer:
    peer_id: str


CredentialPeer = InspectableCredentialPeer | UnknownCredentialPeer | AbsentCredentialPeer


@dataclass(frozen=True, slots=True, repr=False)
class ResidentPeerInventory:
    required_peer_ids: frozenset[str]
    peers: tuple[CredentialPeer, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ResidentAdmissionIdentity:
    principal_id: str
    tenant_id: str
    household_id: str
    household_ids: tuple[str, ...]
    roles: tuple[PrincipalRole, ...]
    scopes: tuple[str, ...]
    area_ids: tuple[str, ...]
    device_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    operations: tuple[str, ...]
    credential_fingerprint: str
    credential_revision: str
    credential_expires_at: datetime | None
    origin: str


class ResidentAdmission:
    """Pin a declared active inventory; its production completeness is a host obligation."""

    def __init__(
        self,
        *,
        settings: Settings,
        resident_verifier: StaticBearerTokenVerifier | None,
        peers: ResidentPeerInventory | None,
        authority_policy: AuthorityPolicy,
    ) -> None:
        self._closed = True
        self._origin = ""
        self._host = ""
        self._verifiers: tuple[StaticBearerTokenVerifier, ...] = ()
        self._snapshots: tuple[CredentialSnapshot, ...] = ()
        self._policy = AuthorityPolicy(
            tenant_id=authority_policy.tenant_id, household_id=authority_policy.household_id
        )
        try:
            if settings.resident_token_file is None or settings.resident_origin is None:
                return
            origin = settings.resident_origin
            if Settings.validate_resident_origin(origin) != origin:
                return
            parsed = urlparse(origin)
            if parsed.scheme == "http" and not (
                is_loopback_host(settings.mcp_host) and is_loopback_host(parsed.hostname or "")
            ):
                return
            if type(resident_verifier) is not StaticBearerTokenVerifier:
                return
            if (
                type(peers) is not ResidentPeerInventory
                or type(peers.required_peer_ids) is not frozenset
                or type(peers.peers) is not tuple
                or not _REQUIRED_PEERS.issubset(peers.required_peer_ids)
            ):
                return
            if any(type(name) is not str or not name for name in peers.required_peer_ids):
                return
            verifiers = [resident_verifier]
            identifiers: list[str] = []
            for peer in peers.peers:
                if type(peer) not in {InspectableCredentialPeer, AbsentCredentialPeer}:
                    return
                identifiers.append(peer.peer_id)
                if isinstance(peer, InspectableCredentialPeer):
                    if type(peer.verifier) is not StaticBearerTokenVerifier:
                        return
                    verifiers.append(peer.verifier)
            if (
                len(set(identifiers)) != len(identifiers)
                or set(identifiers) != peers.required_peer_ids
            ):
                return
            self._verifiers = tuple(verifiers)
            snapshots = self._read_snapshots()
            if not self._separated(snapshots):
                return
            self._snapshots = snapshots
            self._origin, self._host = origin, parsed.netloc
            self._closed = False
        except (AttributeError, TypeError, ValueError, OSError, OverflowError):
            self.close()

    def close(self) -> None:
        """Latch closed without changing active or pinned credential evidence."""
        self._closed = True

    def _unavailable(self) -> NoReturn:
        self.close()
        raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE) from None

    def _read_snapshots(self) -> tuple[CredentialSnapshot, ...]:
        return tuple(verifier.credential_snapshot() for verifier in self._verifiers)

    @staticmethod
    def _separated(snapshots: tuple[CredentialSnapshot, ...]) -> bool:
        for snapshot in snapshots:
            source = snapshot.source
            if (
                source is None
                or not source.canonical_path
                or not Path(source.canonical_path).is_absolute()
                or type(source.device_id) is not int
                or type(source.inode) is not int
                or source.device_id <= 0
                or source.inode <= 0
            ):
                return False
        resident = snapshots[0]
        resident_source = resident.source
        assert resident_source is not None
        hashes = {record.token_hash for record in resident.records}
        for peer in snapshots[1:]:
            source = peer.source
            assert source is not None
            same_canonical = source.canonical_path == resident_source.canonical_path
            same_physical = (source.device_id, source.inode) == (
                resident_source.device_id,
                resident_source.inode,
            )
            shared_hash = bool(hashes.intersection(record.token_hash for record in peer.records))
            if same_canonical or same_physical or shared_hash:
                return False
        return True

    def _require_current(self) -> None:
        if self._closed:
            self._unavailable()
        try:
            current = self._read_snapshots()
        except (AttributeError, TypeError, ValueError, OSError, OverflowError):
            self._unavailable()
        if current != self._snapshots or self._closed:
            self._unavailable()

    async def authenticate(
        self, authorization: str | None, *, origin: str | None, host: str | None
    ) -> ResidentAdmissionIdentity:
        self.check_origin(origin=origin, host=host)
        if type(authorization) is not str:
            raise ResidentAdmissionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
        scheme, separator, token = authorization.partition(" ")
        if (
            scheme.casefold() != "bearer"
            or separator != " "
            or not token
            or not token.isascii()
            or any(ord(char) <= 32 or ord(char) >= 127 or char == "," for char in token)
        ):
            raise ResidentAdmissionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
        try:
            result = await self._verifiers[0].verify_token(token)
        except (AttributeError, TypeError, ValueError, OSError, OverflowError):
            self._unavailable()
        # Unconditional: changes/close also win over a verifier returning None.
        self._require_current()
        if result is None:
            raise ResidentAdmissionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        snapshot = self._snapshots[0]
        matches = [
            record
            for record in snapshot.records
            if hmac.compare_digest(record.token_hash, fingerprint)
        ]
        if len(matches) != 1 or matches[0].client_id != result.client_id:
            self._unavailable()
        record = matches[0]
        if not (
            record.management_authority_explicit is True
            and _EXPLICIT_FIELDS.issubset(record.provided_fields)
            and "consent:human" in record.scopes
            and {PrincipalRole.OWNER, PrincipalRole.OPERATOR}.intersection(record.roles)
            and "review_consent" in record.operations
        ):
            raise ResidentAdmissionError(ErrorCode.INSUFFICIENT_SCOPE)
        authority = AuthorityContext(
            principal_id=record.client_id,
            tenant_id=record.tenant_id,
            household_id=record.household_ids[0],
            household_ids=list(record.household_ids),
            roles=list(record.roles),
            area_ids=list(record.area_ids),
            device_ids=list(record.device_ids),
            capabilities=list(record.capabilities),
            operations=list(record.operations),
        )
        try:
            self._policy.authorize(authority, operation="review_consent")
        except DomainError:
            raise ResidentAdmissionError(ErrorCode.INSUFFICIENT_SCOPE) from None
        return ResidentAdmissionIdentity(
            principal_id=record.client_id,
            tenant_id=record.tenant_id,
            household_id=self._policy.household_id,
            household_ids=record.household_ids,
            roles=record.roles,
            scopes=record.scopes,
            area_ids=record.area_ids,
            device_ids=record.device_ids,
            capabilities=record.capabilities,
            operations=record.operations,
            credential_fingerprint=fingerprint,
            credential_revision=snapshot.revision_digest,
            credential_expires_at=record.expires_at,
            origin=self._origin,
        )

    def ensure_available(self) -> None:
        self._require_current()

    def check_origin(self, *, origin: str | None, host: str | None) -> None:
        self._require_current()
        if origin != self._origin or host != self._host:
            raise ResidentAdmissionError(ErrorCode.INSUFFICIENT_SCOPE)

    def authorize_review(
        self,
        identity: ResidentAdmissionIdentity,
        *,
        target_household_id: str,
        device_ids: tuple[str, ...],
        capabilities: tuple[str, ...],
        area_ids: tuple[str | None, ...] = (),
        operation: str = "review_consent",
    ) -> None:
        """Apply the deployment policy to the exact devices/areas/capabilities under review.

        An area-restricted identity cannot review a device whose area is unknown.
        """
        if identity.area_ids and (not area_ids or None in area_ids):
            raise ResidentAdmissionError(ErrorCode.INSUFFICIENT_SCOPE)
        authority = self._authority(identity)
        for area_id in dict.fromkeys(area_ids) if area_ids else (None,):
            self._policy.authorize(
                authority,
                operation=operation,
                target_household_id=target_household_id,
                area_id=area_id,
                device_ids=device_ids,
                capabilities=capabilities,
            )

    @staticmethod
    def _authority(identity: ResidentAdmissionIdentity) -> AuthorityContext:
        return AuthorityContext(
            principal_id=identity.principal_id,
            tenant_id=identity.tenant_id,
            household_id=identity.household_id,
            household_ids=list(identity.household_ids),
            roles=list(identity.roles),
            area_ids=list(identity.area_ids),
            device_ids=list(identity.device_ids),
            capabilities=list(identity.capabilities),
            operations=list(identity.operations),
        )

    def authorize_decision(self, identity: ResidentAdmissionIdentity) -> None:
        """Deciding needs the explicit approve_consent operation, not just review."""
        self._authorize(identity, "approve_consent")

    def authorize_revoke(self, identity: ResidentAdmissionIdentity) -> None:
        """Revoking needs its own revoke_consent operation; approving does not imply it."""
        self._authorize(identity, "revoke_consent")

    def authorize_receipt(self, identity: ResidentAdmissionIdentity) -> None:
        """Reading a decision receipt is open to whoever may approve or revoke."""
        try:
            self.authorize_decision(identity)
        except ResidentAdmissionError:
            self.authorize_revoke(identity)

    def _authorize(self, identity: ResidentAdmissionIdentity, operation: str) -> None:
        try:
            self._policy.authorize(self._authority(identity), operation=operation)
        except DomainError:
            raise ResidentAdmissionError(ErrorCode.INSUFFICIENT_SCOPE) from None


@dataclass(frozen=True, slots=True, repr=False)
class ResidentSession:
    session_id: str
    identity: ResidentAdmissionIdentity
    issued_at: datetime
    expires_at: datetime


class ResidentSessionError(DomainError):
    def __init__(self, code: ErrorCode) -> None:
        message = (
            "Se alcanzó el límite de sesiones del residente"
            if code == ErrorCode.SAFETY_LIMIT_EXCEEDED
            else "La sesión del residente no es válida"
        )
        super().__init__(code, message)

    @property
    def status_code(self) -> int:
        return 429 if self.code == ErrorCode.SAFETY_LIMIT_EXCEEDED else 401


@dataclass(frozen=True, slots=True, repr=False)
class ConsentReviewInput:
    """Read-only facts a review source returns for one authorized target."""

    target: Plan | AutomationRule
    devices: Sequence[Device]
    preferences: PreferenceProfile
    control_version: ConsentControlVersion | dict[str, object]
    timezone: str


@dataclass(frozen=True, slots=True, repr=False)
class ResidentChallenge:
    challenge_id: str
    session_id: str
    purpose: str
    reference: ConsentReference
    review_digest: str
    expires_at: datetime
    # The instant the resident saw the target: later grants were never reviewed.
    reviewed_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class ResidentReview:
    view: dict[str, Any]
    review_digest: str
    challenge: ResidentChallenge


@dataclass(frozen=True, slots=True, repr=False)
class ResidentDecision:
    status: str
    review_digest: str
    approval_id: str | None
    expires_at: datetime | None
    reviewed_control_version: dict[str, Any] | None


@dataclass(frozen=True, slots=True, repr=False)
class ResidentRevocation:
    """A recorded revocation; ``revocation_pending`` never claims a receiver cutoff."""

    status: str
    approval_id: str
    revocation_id: str
    revoked_at: datetime
    control_version: dict[str, Any]


def _revocation_of(receipt: Any) -> ResidentRevocation:
    return ResidentRevocation(
        receipt.status,
        receipt.approval_id,
        receipt.revocation_id,
        receipt.revoked_at,
        receipt.control_version.model_dump(mode="json"),
    )


_GRANT_REQUEST_TTL = timedelta(minutes=5)


def format_utc(value: datetime) -> str:
    """The one wire format of an instant: UTC with a trailing Z."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decision_fingerprint(decision: ConsentDecision) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                "resident-decision/v1",
                decision.challenge_id,
                decision.action,
                decision.expected_review_digest,
            ]
        ).encode()
    ).hexdigest()


def _receipt_decision(stored: dict[str, Any]) -> ResidentDecision:
    return ResidentDecision(
        "approved",
        stored["review_digest"],
        stored["approval_id"],
        datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00")),
        stored["reviewed_control_version"],
    )


class ResidentReviewError(Exception):
    """Fixed, sanitized review failure with its HTTP status and body."""

    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.body = {"error": error, "message": message}


_CLARIFICATION_PREFIX = "clarification_required: "
_CHALLENGE_TTL_SECONDS = 120
_CHALLENGES_PER_SESSION = 4
_LOAD_TIMEOUT_SECONDS = 2.0


def _review_error(kind: int | str) -> ResidentReviewError:
    return ResidentReviewError(
        *{
            400: (400, "validation_error", "La solicitud de revisión no es válida"),
            404: (404, "not_found", "No se encontró el objetivo"),
            422: (422, "clarification_required", "La revisión requiere aclaración"),
            "challenge_invalid": (
                409,
                "challenge_invalid",
                "El desafío no es válido o expiró; revisa de nuevo",
            ),
            "decision_conflict": (
                409,
                "decision_conflict",
                "No se pudo registrar la decisión; revisa de nuevo",
            ),
            "decision_unsupported": (
                409,
                "decision_unsupported",
                "Este tipo de revisión aún no se puede aprobar",
            ),
        }[kind]
    )


def canonical_review_json(view: dict[str, Any]) -> bytes:
    """Single canonical form of a projected view; its SHA-256 is the review digest."""
    return json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _strict_json(raw: bytes) -> Any:
    """UTF-8 JSON without duplicate keys, BOM or non-finite numbers."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in items}) != len(items):
            raise ValueError("duplicate key")
        return dict(items)

    def constant(_name: str) -> Any:
        raise ValueError("non-finite number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def parse_decision(raw: bytes) -> ConsentDecision:
    """Strict `ConsentDecision` (approve, reject or revoke)."""
    try:
        body = _strict_json(raw)
        if type(body) is not dict:
            raise ValueError("body")
        return ConsentDecision.model_validate(body)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise _review_error(400) from None


def parse_review_reference(raw: bytes) -> ConsentReference:
    """Strict `{"reference": {"kind": "plan"|"rule", "id": str}}`; nothing else is accepted."""
    try:
        body = _strict_json(raw)
        if type(body) is not dict or set(body) != {"reference"}:
            raise ValueError("body")
        reference = body["reference"]
        if type(reference) is not dict or set(reference) != {"kind", "id"}:
            raise ValueError("reference")
        if reference["kind"] not in ("plan", "rule") or type(reference["id"]) is not str:
            raise ValueError("reference")
        return ConsentReference(kind=reference["kind"], id=reference["id"])
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise _review_error(400) from None


class ResidentConsentAdapter:
    """One host's bounded session state over a concrete, separately verified admission."""

    def __init__(
        self,
        *,
        admission: ResidentAdmission,
        clock: Clock | None = None,
        monotonic: Callable[[], float] | None = None,
        review_source: Any = None,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        if type(admission) is not ResidentAdmission:
            raise ValueError("admission must be a ResidentAdmission")
        if approval_store is not None and type(approval_store) is not ApprovalStore:
            raise ValueError("approval_store must be an ApprovalStore")
        self._approval_store = approval_store
        if review_source is not None and not inspect.iscoroutinefunction(
            getattr(type(review_source), "load", None)
        ):
            raise ValueError("review_source must provide an async load(identity, reference)")
        self._review_source = review_source
        self._challenges: dict[str, tuple[ResidentChallenge, float]] = {}
        self._admission = admission
        self._clock = clock if clock is not None else SystemClock()
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = RLock()
        self._closed = False
        self._bound = False
        self._sessions: dict[str, tuple[ResidentSession, float]] = {}
        self._last_time: tuple[datetime, float] | None = None

    def bind_host(self) -> None:
        with self._lock:
            if self._bound:
                raise ValueError("resident consent adapter already belongs to an app")
            self._bound = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._admission.close()
            self._sessions.clear()
            self._challenges.clear()

    def _unavailable(self) -> NoReturn:
        self.close()
        raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE) from None

    def ensure_available(self) -> None:
        with self._lock:
            if self._closed:
                self._unavailable()
            try:
                self._admission.ensure_available()
            except ResidentAdmissionError:
                self._unavailable()

    def check_origin(self, *, origin: str | None, host: str | None) -> None:
        with self._lock:
            self.ensure_available()
            try:
                self._admission.check_origin(origin=origin, host=host)
            except ResidentAdmissionError as error:
                if error.status_code == 503:
                    self.close()
                raise

    def _sample(self) -> tuple[datetime, float]:
        try:
            raw, tick = self._clock.now(), self._monotonic()
            if (
                not isinstance(raw, datetime)
                or raw.tzinfo is None
                or raw.utcoffset() is None
                or isinstance(tick, bool)
                or not isinstance(tick, (int, float))
                or not math.isfinite(tick)
            ):
                self._unavailable()
            normalized = raw.astimezone(UTC)
            now = datetime(
                normalized.year,
                normalized.month,
                normalized.day,
                normalized.hour,
                normalized.minute,
                normalized.second,
                normalized.microsecond,
                tzinfo=UTC,
            )
            if self._last_time is not None and (
                now < self._last_time[0] or tick < self._last_time[1]
            ):
                self._unavailable()
            self._last_time = now, float(tick)
            return self._last_time
        except (ValueError, TypeError, OSError, OverflowError):
            self._unavailable()

    def _prune(self, now: datetime, tick: float) -> int:
        expired = [
            key
            for key, (session, deadline) in self._sessions.items()
            if now >= session.expires_at or tick >= deadline
        ]
        for key in expired:
            del self._sessions[key]
        for challenge_id in [
            key
            for key, (challenge, deadline) in self._challenges.items()
            if challenge.session_id not in self._sessions
            or now >= challenge.expires_at
            or tick >= deadline
        ]:
            del self._challenges[challenge_id]
        return len(expired)

    def prune_expired(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            self.ensure_available()
            return self._prune(*self._sample())

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @property
    def challenge_count(self) -> int:
        with self._lock:
            return len(self._challenges)

    @property
    def challenges(self) -> tuple[ResidentChallenge, ...]:
        with self._lock:
            return tuple(challenge for challenge, _ in self._challenges.values())

    async def _authenticate(
        self,
        authorization: str | None,
        *,
        origin: str | None,
        host: str | None,
    ) -> ResidentAdmissionIdentity:
        self.ensure_available()
        try:
            return await self._admission.authenticate(authorization, origin=origin, host=host)
        except ResidentAdmissionError as error:
            if error.status_code == 503:
                self.close()
            raise

    async def create_session(
        self,
        authorization: str | None,
        *,
        origin: str | None,
        host: str | None,
    ) -> ResidentSession:
        identity = await self._authenticate(authorization, origin=origin, host=host)
        with self._lock:
            self.ensure_available()
            now, tick = self._sample()
            self._prune(now, tick)
            expires = now + timedelta(seconds=600)
            if identity.credential_expires_at is not None:
                expires = min(expires, identity.credential_expires_at)
            if expires <= now:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            if len(self._sessions) >= 64:
                raise ResidentSessionError(ErrorCode.SAFETY_LIMIT_EXCEEDED)
            for _ in range(3):
                try:
                    session_id = secrets.token_urlsafe(32)
                except (OSError, ValueError):
                    self._unavailable()
                if session_id not in self._sessions:
                    session = ResidentSession(session_id, identity, now, expires)
                    self._sessions[session_id] = session, tick + (expires - now).total_seconds()
                    return session
            self._unavailable()

    async def authenticate_session(
        self,
        session_id: str | None,
        authorization: str | None,
        *,
        origin: str | None,
        host: str | None,
    ) -> ResidentSession:
        identity = await self._authenticate(authorization, origin=origin, host=host)
        with self._lock:
            self.ensure_available()
            self._prune(*self._sample())
            entry = self._sessions.get(session_id) if type(session_id) is str else None
            if entry is None or entry[0].identity != identity:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            return entry[0]

    async def _load_review(
        self,
        source: Any,
        session: ResidentSession,
        reference: ConsentReference,
        operation: str = "review_consent",
    ) -> ConsentReviewInput:
        """Read-only lookup; every failure is the same generic not-found."""
        identity = session.identity
        try:
            with anyio.fail_after(_LOAD_TIMEOUT_SECONDS):
                loaded = await source.load(identity, reference)
            if type(loaded) is not ConsentReviewInput:
                raise _review_error(404)
            target = loaded.target
            rule = isinstance(target, AutomationRule)
            if ("rule" if rule else "plan") != reference.kind or target.id != reference.id:
                raise _review_error(404)
            if (target.authority.tenant_id, target.authority.household_id) != (
                identity.tenant_id,
                identity.household_id,
            ):
                raise _review_error(404)
            plan = target.plan_template if isinstance(target, AutomationRule) else target
            referenced = [command.device_id for command in plan.commands]
            if isinstance(target, AutomationRule):
                # The review also shows the rule's trigger and conditions.
                referenced.extend(
                    device_id
                    for device_id in (
                        getattr(target.trigger, "device_id", None),
                        *(getattr(item, "device_id", None) for item in target.conditions),
                    )
                    if device_id is not None
                )
            device_ids = tuple(dict.fromkeys(referenced))
            area_ids = tuple(
                device.area_id for device in loaded.devices if device.id in set(referenced)
            )
            capabilities = tuple(
                dict.fromkeys(
                    capability.name
                    for command in plan.commands
                    for device in loaded.devices
                    if device.id == command.device_id
                    for capability in device.capabilities
                    if command.command in capability.commands
                )
            )
            self._admission.authorize_review(
                identity,
                target_household_id=target.authority.household_id,
                device_ids=device_ids,
                capabilities=capabilities,
                area_ids=area_ids,
                operation=operation,
            )
        except Exception:  # noqa: BLE001 - one generic outcome, nothing leaks from the source
            # A credential change or close during the lookup wins over a source failure.
            self.ensure_available()
            raise _review_error(404) from None
        return loaded

    @staticmethod
    def _project(
        loaded: ConsentReviewInput, now: datetime
    ) -> tuple[ConsentView, dict[str, Any], str]:
        """The one projection and digest shared by a review and the decision that re-checks it."""
        try:
            view = build_consent_view(
                target=loaded.target,
                devices=loaded.devices,
                preferences=loaded.preferences,
                control_version=loaded.control_version,
                now=now,
                timezone=loaded.timezone,
            )
        except ValueError as error:
            if type(error) is ValueError and str(error).startswith(_CLARIFICATION_PREFIX):
                raise _review_error(422) from None
            raise
        payload = view.model_dump(mode="json")
        digest = "sha256:" + hashlib.sha256(canonical_review_json(payload)).hexdigest()
        return view, payload, digest

    def authorize_decision(self, session: ResidentSession) -> None:
        """Reading a decision receipt needs the approve or the revoke operation."""
        self.ensure_available()
        self._admission.authorize_receipt(session.identity)

    async def review(self, session: ResidentSession, reference: ConsentReference) -> ResidentReview:
        """Project the exact target and bind one short-lived challenge to its digest."""
        source = self._review_source
        if source is None:
            raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE)
        self.ensure_available()
        loaded = await self._load_review(source, session, reference)
        with self._lock:
            # No await below: the checks, the projection and the insert are one step.
            self.ensure_available()
            now, tick = self._sample()
            self._prune(now, tick)
            entry = self._sessions.get(session.session_id)
            if entry is None or entry[0] != session:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            view, payload, digest = self._project(loaded, now)
            expires = min(
                now + timedelta(seconds=_CHALLENGE_TTL_SECONDS),
                session.expires_at,
                view.expires_at.astimezone(UTC),
            )
            if expires <= now:
                raise _review_error(422)
            siblings = {
                key: challenge
                for key, (challenge, _deadline) in self._challenges.items()
                if challenge.session_id == session.session_id
            }
            replaced = [key for key, item in siblings.items() if item.reference == reference]
            if len(siblings) - len(replaced) >= _CHALLENGES_PER_SESSION:
                raise ResidentSessionError(ErrorCode.SAFETY_LIMIT_EXCEEDED)
            for _ in range(3):
                try:
                    challenge_id = secrets.token_urlsafe(32)
                except (OSError, ValueError):
                    self._unavailable()
                if challenge_id not in self._challenges:
                    for key in replaced:
                        del self._challenges[key]
                    challenge = ResidentChallenge(
                        challenge_id,
                        session.session_id,
                        "approve",
                        reference,
                        digest,
                        expires,
                        now,
                    )
                    self._challenges[challenge_id] = (
                        challenge,
                        tick + (expires - now).total_seconds(),
                    )
                    return ResidentReview(payload, digest, challenge)
            self._unavailable()

    async def decide(
        self, session: ResidentSession, decision: ConsentDecision
    ) -> ResidentDecision | ResidentRevocation:
        """Consume the challenge once, re-check the exact plan, then issue, revoke or stop."""
        store, source = self._approval_store, self._review_source
        if store is None or source is None:
            raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE)
        self.ensure_available()
        revoking = decision.action == "revoke"
        # Authorization precedes any state change, including the one-use challenge.
        if revoking:
            self._admission.authorize_revoke(session.identity)
        else:
            self._admission.authorize_decision(session.identity)
        # Phase A: validate and consume the one-use challenge; nothing below can restore it.
        with self._lock:
            self.ensure_available()
            now, tick = self._sample()
            self._prune(now, tick)
            entry = self._sessions.get(session.session_id)
            if entry is None or entry[0] != session:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            fingerprint = _decision_fingerprint(decision)
            identity = session.identity
            receipt = store.get_decision_receipt(
                identity.tenant_id,
                identity.household_id,
                identity.principal_id,
                decision.request_id,
            )
            if receipt is not None:
                # A retry of an already recorded approval: same bytes, nothing consumed or reissued.
                if receipt[0] != fingerprint:
                    raise _review_error("decision_conflict")
                return _receipt_decision(receipt[1])
            if revoking:
                try:
                    recorded = store.get_consent_receipt(
                        identity.tenant_id,
                        identity.household_id,
                        identity.principal_id,
                        decision.request_id,
                    )
                    if recorded is not None:
                        # The same body gets the stored receipt back; any other body conflicts.
                        return _revocation_of(
                            store.revoke_consent(
                                recorded.approval_id,
                                authority=self._admission._authority(identity),
                                request_id=decision.request_id,
                                request_fingerprint=fingerprint,
                            )
                        )
                except DomainError as error:
                    raise self._revoke_failure(error) from None
            held = self._challenges.get(decision.challenge_id)
            if held is None or held[0].session_id != session.session_id:
                raise _review_error("challenge_invalid")
            challenge = held[0]
            del self._challenges[decision.challenge_id]
            if not hmac.compare_digest(decision.expected_review_digest, challenge.review_digest):
                raise _review_error("challenge_invalid")
        if decision.action == "reject":
            return ResidentDecision("rejected", challenge.review_digest, None, None, None)
        if challenge.reference.kind != "plan":
            raise _review_error("decision_unsupported")
        # Phase B: the same read-only lookup and ACL as the review, for the decided operation.
        loaded = await self._load_review(
            source,
            session,
            challenge.reference,
            operation="revoke_consent" if revoking else "approve_consent",
        )
        # Phase C: projection, digest, assertion and issuance in one step without an await.
        with self._lock:
            self.ensure_available()
            now, tick = self._sample()
            self._prune(now, tick)
            entry = self._sessions.get(session.session_id)
            if entry is None or entry[0] != session:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            view, _payload, digest = self._project(loaded, now)
            if not hmac.compare_digest(digest, challenge.review_digest):
                raise _review_error("challenge_invalid")
            if revoking:
                return self._revoke(session, loaded.target, decision, fingerprint, challenge, now)
            expires = min(view.expires_at.astimezone(UTC), now + _GRANT_REQUEST_TTL)
            plan = loaded.target
            if expires <= now or not isinstance(plan, Plan) or plan.validation is None:
                raise _review_error("challenge_invalid")
            principal = OperatorPrincipal(
                session.identity.principal_id,
                "resident_browser",
                "resident-session:" + hashlib.sha256(session.session_id.encode()).hexdigest()[:32],
            )
            assertion = ApprovalAssertion(
                principal,
                hashlib.sha256(("resident-consent:" + challenge.challenge_id).encode()).hexdigest(),
                now,
                expires,
                plan_id=plan.id,
                validation_digest=plan.validation.digest,
            )
            try:
                grant = store.issue_authenticated(
                    plan,
                    principal=principal,
                    assertion=assertion,
                    decision=ConsentDecisionRecord(
                        session.identity.principal_id,
                        decision.request_id,
                        fingerprint,
                        digest,
                        view.bindings.control_version.model_dump(mode="json"),
                    ),
                )
            except DomainError as error:
                if error.code in {
                    ErrorCode.SOURCE_UNAVAILABLE,
                    ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE,
                }:
                    raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE) from None
                raise _review_error("decision_conflict") from None
            return ResidentDecision(
                "approved",
                digest,
                grant.approval_id,
                grant.expires_at,
                view.bindings.control_version.model_dump(mode="json"),
            )

    @staticmethod
    def _revoke_failure(error: DomainError) -> Exception:
        if error.code in {ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE}:
            return ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE)
        return _review_error("decision_conflict")

    def _revoke(
        self,
        session: ResidentSession,
        plan: Any,
        decision: ConsentDecision,
        fingerprint: str,
        challenge: ResidentChallenge,
        now: datetime,
    ) -> ResidentRevocation:
        """Revoke the one active approval of the reviewed plan; never guess among several."""
        store = self._approval_store
        assert store is not None  # guarded by decide()
        identity = session.identity
        try:
            approval_id = store.resolve_active_approval(
                identity.tenant_id,
                identity.household_id,
                plan.id,
                now=now,
                issued_not_after=challenge.reviewed_at,
            )
            if approval_id is None:
                raise _review_error("decision_conflict")
            receipt = store.revoke_consent(
                approval_id,
                authority=self._admission._authority(identity),
                request_id=decision.request_id,
                request_fingerprint=fingerprint,
            )
        except DomainError as error:
            raise self._revoke_failure(error) from None
        return _revocation_of(receipt)

    def receipt_for(
        self, session: ResidentSession, request_id: str
    ) -> ResidentDecision | ResidentRevocation | None:
        """The identity's own recorded decision for a request id; one answer for anything else."""
        store = self._approval_store
        if store is None:
            raise ResidentAdmissionError(ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE)
        self.ensure_available()
        self._admission.authorize_receipt(session.identity)
        with self._lock:
            self.ensure_available()
            now, tick = self._sample()
            self._prune(now, tick)
            entry = self._sessions.get(session.session_id)
            if entry is None or entry[0] != session:
                raise ResidentSessionError(ErrorCode.OPERATOR_AUTHENTICATION_FAILED)
            identity = session.identity
            receipt = store.get_decision_receipt(
                identity.tenant_id, identity.household_id, identity.principal_id, request_id
            )
            if receipt is not None:
                return _receipt_decision(receipt[1])
            try:
                revoked = store.get_consent_receipt(
                    identity.tenant_id, identity.household_id, identity.principal_id, request_id
                )
            except DomainError as error:
                if error.code is not ErrorCode.CONSENT_CONFLICT:
                    raise self._revoke_failure(error) from None
                return None  # an id that is not a revocation answers like any unknown id
        return None if revoked is None else _revocation_of(revoked)
