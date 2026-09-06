"""Freshness-aware in-memory state store."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from domoai.domain.models import (
    SourceCursor,
    SourceOrderingPolicy,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


@dataclass(frozen=True)
class StateStoreMetadata:
    """Durable revision/version counters used by validated plan dependencies."""

    inventory_revision: int
    version_counter: int
    state_versions: dict[tuple[str, str], int]
    inventory_fingerprint: str | None = None
    source_cursors: dict[tuple[str, str], SourceCursor] = field(default_factory=dict)
    source_ordering_policies: dict[tuple[str, str], SourceOrderingPolicy] = field(
        default_factory=dict
    )
    resync_required: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class _StateCandidate:
    snapshots: dict[tuple[str, str], StateSnapshot]
    source_snapshots: dict[tuple[str, str, str, str], StateSnapshot]
    inventory_revision: int
    inventory_fingerprint: str | None
    state_versions: dict[tuple[str, str], int]
    version_counter: int
    startup_reconfirmation: dict[tuple[str, str], tuple[object, StateStatus]]
    source_cursors: dict[tuple[str, str], SourceCursor]
    source_ordering_policies: dict[tuple[str, str], SourceOrderingPolicy]
    resync_required: dict[tuple[str, str], str]


class RuntimeStatePersistencePort(Protocol):
    async def persist(
        self, snapshots: Sequence[StateSnapshot], metadata: StateStoreMetadata
    ) -> None: ...

    async def delete(self, device_id: str, metadata: StateStoreMetadata) -> None: ...

    async def delete_capability(
        self, device_id: str, capability: str, metadata: StateStoreMetadata
    ) -> None: ...


class StateStore:
    _UNORDERED_STREAM_ID = "unordered"

    def __init__(
        self,
        stale_after: timedelta = timedelta(minutes=5),
        *,
        clock: Clock | None = None,
        operational_metrics: RuntimeOperationalMetrics | None = None,
    ) -> None:
        self.stale_after = stale_after
        self.clock = clock or SystemClock()
        self.operational_metrics = operational_metrics
        self._snapshots: dict[tuple[str, str], StateSnapshot] = {}
        self._source_snapshots: dict[tuple[str, str, str, str], StateSnapshot] = {}
        self._revision = 0
        self._state_versions: dict[tuple[str, str], int] = {}
        self._version_counter = 0
        self._inventory_fingerprint: str | None = None
        self._startup_reconfirmation: dict[tuple[str, str], tuple[object, StateStatus]] = {}
        self._source_cursors: dict[tuple[str, str], SourceCursor] = {}
        self._source_ordering_policies: dict[tuple[str, str], SourceOrderingPolicy] = {}
        self._resync_required: dict[tuple[str, str], str] = {}
        self._durability_status = "committed"
        self._persistence: RuntimeStatePersistencePort | None = None
        self._mutation_lock = asyncio.Lock()
        self._pending_metadata_before: tuple[int, str | None] | None = None

    def bind_persistence(self, persistence: RuntimeStatePersistencePort) -> None:
        """Attach the one durable writer used by every mutation path."""

        self._persistence = persistence

    @property
    def persistence_bound(self) -> bool:
        return self._persistence is not None

    def begin_revision(self) -> None:
        self._remember_metadata_before_mutation()
        self._revision += 1

    @property
    def runtime_revision(self) -> str:
        return f"rev-{self._revision}"

    def state_version(self, device_id: str, capability: str) -> int:
        return self._state_versions.get((device_id, capability), 0)

    def restore_metadata(self, metadata: StateStoreMetadata) -> None:
        """Restore durable counters before persisted snapshots are loaded."""

        self._revision = max(0, metadata.inventory_revision)
        self._version_counter = max(
            metadata.version_counter,
            max(metadata.state_versions.values(), default=0),
        )
        self._state_versions = {
            key: version for key, version in metadata.state_versions.items() if version >= 0
        }
        self._inventory_fingerprint = metadata.inventory_fingerprint
        self._source_cursors = dict(metadata.source_cursors)
        self._source_ordering_policies = {
            key: SourceOrderingPolicy(policy)
            for key, policy in metadata.source_ordering_policies.items()
        }
        self._resync_required = dict(metadata.resync_required)

    @property
    def inventory_fingerprint(self) -> str | None:
        return self._inventory_fingerprint

    def record_inventory_fingerprint(self, fingerprint: str) -> None:
        self._remember_metadata_before_mutation()
        self._inventory_fingerprint = fingerprint

    def _remember_metadata_before_mutation(self) -> None:
        if self._pending_metadata_before is None:
            self._pending_metadata_before = (self._revision, self._inventory_fingerprint)

    def confirm_metadata_persisted(self) -> None:
        """Mark a metadata write performed by an external repository committed."""

        self._pending_metadata_before = None

    def rollback_metadata_mutation(self) -> None:
        """Restore metadata changed since the last durable confirmation."""

        if self._pending_metadata_before is None:
            return
        self._revision, self._inventory_fingerprint = self._pending_metadata_before
        self._pending_metadata_before = None

    def export_metadata(self) -> StateStoreMetadata:
        return StateStoreMetadata(
            inventory_revision=self._revision,
            version_counter=self._version_counter,
            state_versions=dict(self._state_versions),
            inventory_fingerprint=self._inventory_fingerprint,
            source_cursors=dict(self._source_cursors),
            source_ordering_policies=dict(self._source_ordering_policies),
            resync_required=dict(self._resync_required),
        )

    @property
    def durability_status(self) -> str:
        return self._durability_status

    def ordering_report(self) -> dict[str, object]:
        return {
            "source_cursors": dict(self._source_cursors),
            "source_ordering_policies": dict(self._source_ordering_policies),
            "resync_required": dict(self._resync_required),
        }

    def _candidate(self) -> _StateCandidate:
        return _StateCandidate(
            snapshots=dict(self._snapshots),
            source_snapshots=dict(self._source_snapshots),
            inventory_revision=self._revision,
            inventory_fingerprint=self._inventory_fingerprint,
            state_versions=dict(self._state_versions),
            version_counter=self._version_counter,
            startup_reconfirmation=dict(self._startup_reconfirmation),
            source_cursors=dict(self._source_cursors),
            source_ordering_policies=dict(self._source_ordering_policies),
            resync_required=dict(self._resync_required),
        )

    def _metadata_for(self, candidate: _StateCandidate) -> StateStoreMetadata:
        return StateStoreMetadata(
            inventory_revision=candidate.inventory_revision,
            version_counter=candidate.version_counter,
            state_versions=dict(candidate.state_versions),
            inventory_fingerprint=candidate.inventory_fingerprint,
            source_cursors=dict(candidate.source_cursors),
            source_ordering_policies=dict(candidate.source_ordering_policies),
            resync_required=dict(candidate.resync_required),
        )

    def _install_candidate(self, candidate: _StateCandidate) -> None:
        self._snapshots = candidate.snapshots
        self._source_snapshots = candidate.source_snapshots
        self._revision = candidate.inventory_revision
        self._inventory_fingerprint = candidate.inventory_fingerprint
        self._state_versions = candidate.state_versions
        self._version_counter = candidate.version_counter
        self._startup_reconfirmation = candidate.startup_reconfirmation
        self._source_cursors = candidate.source_cursors
        self._source_ordering_policies = candidate.source_ordering_policies
        self._resync_required = candidate.resync_required

    async def _commit_candidate(
        self,
        candidate: _StateCandidate,
        snapshots: Sequence[StateSnapshot],
    ) -> None:
        try:
            interrupted = await self._await_durable_operation(
                self._persist(snapshots, self._metadata_for(candidate))
            )
        except BaseException:
            self.rollback_metadata_mutation()
            raise
        self._install_candidate(candidate)
        self.confirm_metadata_persisted()
        self._durability_status = "committed"
        if interrupted:
            raise asyncio.CancelledError

    async def _commit_delete_candidate(
        self,
        candidate: _StateCandidate,
        *,
        device_id: str,
        capability: str | None = None,
    ) -> None:
        async def persist_delete() -> None:
            if self._persistence is not None:
                if capability is None:
                    await self._persistence.delete(device_id, self._metadata_for(candidate))
                else:
                    delete_capability = getattr(self._persistence, "delete_capability", None)
                    if callable(delete_capability):
                        await delete_capability(
                            device_id,
                            capability,
                            self._metadata_for(candidate),
                        )

        try:
            interrupted = await self._await_durable_operation(persist_delete())
        except BaseException:
            self.rollback_metadata_mutation()
            raise
        self._install_candidate(candidate)
        self.confirm_metadata_persisted()
        self._durability_status = "committed"
        if interrupted:
            raise asyncio.CancelledError

    async def _await_durable_operation(self, operation: Awaitable[None]) -> bool:
        """Drain a durable write before honoring caller cancellation.

        The storage boundary cannot cancel its SQLite worker. Shielding the
        operation here lets us install the in-memory candidate after a write
        that really committed, while still returning cancellation to callers.
        """

        task = asyncio.ensure_future(operation)
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
            except BaseException:
                self._durability_status = "degraded"
                raise
        try:
            task.result()
        except BaseException:
            self._durability_status = "degraded"
            raise
        return interrupted

    def load_persisted(self, snapshots: list[StateSnapshot]) -> None:
        """Restore last-known state from persistence, forced to stale."""

        for snapshot in snapshots:
            stale = snapshot.model_copy(update={"status": StateStatus.STALE})
            source_key = (
                stale.device_id,
                stale.capability,
                stale.source_ref.adapter_id,
                stale.source_ref.external_id,
            )
            self._source_snapshots[source_key] = stale
            key = (stale.device_id, stale.capability)
            if key not in self._state_versions:
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
            self._startup_reconfirmation[key] = (snapshot.value, snapshot.status)
            self._snapshots[key] = stale

    async def save(self, snapshot: StateSnapshot) -> None:
        await self.save_many([snapshot])

    async def save_many(self, snapshots: Sequence[StateSnapshot]) -> None:
        async with self._mutation_lock:
            await self._save_many_unlocked(snapshots)

    async def _save_many_unlocked(self, snapshots: Sequence[StateSnapshot]) -> None:
        normalized = [self._normalize_snapshot(snapshot) for snapshot in snapshots]
        candidate = self._candidate()
        if not normalized:
            await self._commit_candidate(candidate, ())
            return

        grouped: dict[tuple[str, str], list[StateSnapshot]] = {}
        cursor_for_stream: dict[tuple[str, str], SourceCursor] = {}
        for snapshot in normalized:
            cursor = snapshot.source_cursor
            if cursor is None:
                policy_key = (
                    snapshot.source_ref.adapter_id,
                    self._UNORDERED_STREAM_ID,
                )
                candidate.source_ordering_policies[policy_key] = SourceOrderingPolicy.UNORDERED
                continue
            stream_key = (cursor.source_id, cursor.stream_id)
            grouped.setdefault(stream_key, []).append(snapshot)
            previous_cursor = cursor_for_stream.get(stream_key)
            if previous_cursor is not None and (
                previous_cursor.epoch != cursor.epoch or previous_cursor.sequence != cursor.sequence
            ):
                raise ValueError("a state batch must use one cursor per source stream")
            cursor_for_stream[stream_key] = cursor

        rejected = False
        for stream_key, cursor in cursor_for_stream.items():
            decision = self._ordered_cursor_decision(
                stream_key,
                cursor,
                candidate.source_cursors,
                candidate.resync_required,
            )
            if decision == "reject":
                if self.operational_metrics is not None:
                    self.operational_metrics.record_source_cursor("gap")
                rejected = True
                self._mark_stream_resync_required(
                    candidate,
                    stream_key,
                    "epoch_changed"
                    if candidate.source_cursors.get(stream_key) is not None
                    and candidate.source_cursors[stream_key].epoch != cursor.epoch
                    else "sequence_gap",
                )
            elif decision == "ignore":
                if self.operational_metrics is not None:
                    self.operational_metrics.record_source_cursor("replay")
                grouped.pop(stream_key, None)
            else:
                if cursor.resync and self.operational_metrics is not None:
                    self.operational_metrics.record_source_cursor("resync")
                candidate.source_ordering_policies[stream_key] = SourceOrderingPolicy.ORDERED

        if rejected:
            affected_keys = {
                (source_snapshot.device_id, source_snapshot.capability)
                for stream_key in cursor_for_stream
                for source_snapshot in candidate.source_snapshots.values()
                if source_snapshot.source_cursor is not None
                and (
                    source_snapshot.source_cursor.source_id,
                    source_snapshot.source_cursor.stream_id,
                )
                == stream_key
            }
            affected = [
                candidate.snapshots[key] for key in affected_keys if key in candidate.snapshots
            ]
            await self._commit_candidate(candidate, affected)
            return

        accepted = [
            snapshot
            for snapshot in normalized
            if snapshot.source_cursor is None
            or (
                snapshot.source_cursor.source_id,
                snapshot.source_cursor.stream_id,
            )
            in grouped
        ]
        accepted = [
            snapshot
            for snapshot in accepted
            if snapshot.source_cursor is not None
            or not self._is_older_unordered_observation(snapshot, candidate)
        ]
        if not accepted:
            return
        for stream_key, cursor in cursor_for_stream.items():
            if stream_key in grouped:
                candidate.source_cursors[stream_key] = cursor
                if cursor.resync:
                    candidate.resync_required.pop(stream_key, None)

        changed_snapshots: list[StateSnapshot] = []
        for snapshot in accepted:
            key = (snapshot.device_id, snapshot.capability)
            source_key = (
                snapshot.device_id,
                snapshot.capability,
                snapshot.source_ref.adapter_id,
                snapshot.source_ref.external_id,
            )
            previous = candidate.snapshots.get(key)
            candidate.source_snapshots[source_key] = snapshot
            resolved = self._resolve_sources(
                snapshot.device_id,
                snapshot.capability,
                candidate.source_snapshots,
            )
            startup_value = candidate.startup_reconfirmation.pop(key, None)
            if startup_value is not None:
                changed = (resolved.value, resolved.status) != startup_value
            else:
                changed = previous is None or (resolved.value, resolved.status) != (
                    previous.value,
                    previous.status,
                )
            if changed:
                candidate.version_counter += 1
                candidate.state_versions[key] = candidate.version_counter
            candidate.snapshots[key] = resolved
            changed_snapshots.append(resolved)
        await self._commit_candidate(candidate, changed_snapshots)

    @staticmethod
    def _is_older_unordered_observation(
        snapshot: StateSnapshot, candidate: _StateCandidate
    ) -> bool:
        source_key = (
            snapshot.device_id,
            snapshot.capability,
            snapshot.source_ref.adapter_id,
            snapshot.source_ref.external_id,
        )
        previous = candidate.source_snapshots.get(source_key)
        if previous is None:
            return False
        if snapshot.status is not StateStatus.CURRENT:
            # A source can report a newly observed failure with the same
            # receipt time as its last value (the deterministic plant does
            # this).  The receipt clock still orders failure reports, while
            # an older failure must not regress a newer current observation.
            return snapshot.received_at < previous.received_at
        return (snapshot.received_at, snapshot.observed_at) < (
            previous.received_at,
            previous.observed_at,
        )

    @staticmethod
    def _normalize_snapshot(snapshot: StateSnapshot) -> StateSnapshot:
        payload = snapshot.model_dump(mode="python")
        if snapshot.status in {StateStatus.INVALID, StateStatus.UNAVAILABLE}:
            payload["value"] = None
        return StateSnapshot.model_validate(payload)

    def _ordered_cursor_decision(
        self,
        stream_key: tuple[str, str],
        cursor: SourceCursor,
        source_cursors: dict[tuple[str, str], SourceCursor],
        resync_required: dict[tuple[str, str], str],
    ) -> str:
        previous = source_cursors.get(stream_key)
        if stream_key in resync_required and not cursor.resync:
            return "ignore"
        if cursor.resync:
            return "accept"
        if previous is None:
            return "accept"
        if cursor.epoch != previous.epoch:
            return "reject"
        if cursor.sequence <= previous.sequence:
            return "ignore"
        if cursor.sequence != previous.sequence + 1:
            return "reject"
        return "accept"

    def _mark_stream_resync_required(
        self,
        candidate: _StateCandidate,
        stream_key: tuple[str, str],
        reason: str,
    ) -> None:
        candidate.resync_required[stream_key] = reason
        affected_keys: set[tuple[str, str]] = set()
        for source_key, source_snapshot in list(candidate.source_snapshots.items()):
            cursor = source_snapshot.source_cursor
            if cursor is None or (cursor.source_id, cursor.stream_id) != stream_key:
                continue
            invalid = source_snapshot.model_copy(
                update={"status": StateStatus.INVALID, "value": None}
            )
            candidate.source_snapshots[source_key] = invalid
            affected_keys.add(source_key[:2])
        for key in affected_keys:
            current = candidate.snapshots.get(key)
            if current is None:
                continue
            candidate.snapshots[key] = current.model_copy(
                update={"status": StateStatus.INVALID, "value": None}
            )
            candidate.version_counter += 1
            candidate.state_versions[key] = candidate.version_counter

    def _resolve_sources(
        self,
        device_id: str,
        capability: str,
        source_snapshots: dict[tuple[str, str, str, str], StateSnapshot] | None = None,
    ) -> StateSnapshot:
        source_snapshots = source_snapshots or self._source_snapshots
        observations = [
            observation
            for (
                source_device,
                source_capability,
                _adapter,
                _external,
            ), observation in source_snapshots.items()
            if source_device == device_id and source_capability == capability
        ]
        if not observations:
            raise KeyError(f"missing source observation for {device_id}/{capability}")
        invalid = [item for item in observations if item.status is StateStatus.INVALID]
        if invalid:
            return max(invalid, key=lambda item: item.received_at)
        current = [item for item in observations if item.status is StateStatus.CURRENT]
        if len(current) >= 2 and len({repr(item.value) for item in current}) > 1:
            latest = max(current, key=lambda item: item.received_at)
            return latest.model_copy(update={"value": None, "status": StateStatus.INVALID})
        candidates = current or observations
        return max(candidates, key=lambda item: item.received_at)

    async def delete(self, device_id: str) -> None:
        async with self._mutation_lock:
            candidate = self._candidate()
            for key in [key for key in candidate.snapshots if key[0] == device_id]:
                del candidate.snapshots[key]
                candidate.state_versions.pop(key, None)
                candidate.startup_reconfirmation.pop(key, None)
            for source_key in [
                source_key
                for source_key in candidate.source_snapshots
                if source_key[0] == device_id
            ]:
                del candidate.source_snapshots[source_key]
            await self._commit_delete_candidate(candidate, device_id=device_id)

    async def forget_household(self, household_id: str) -> int:
        """Evict cached state after a privacy deletion commits in storage."""

        async with self._mutation_lock:
            keys: list[tuple[str, str]]
            source_keys: list[tuple[str, str, str, str]]
            if household_id == "default":
                keys = list(self._snapshots)
                source_keys = list(self._source_snapshots)
            else:
                keys = [
                    key
                    for key, snapshot in self._snapshots.items()
                    if snapshot.authority.household_id in {household_id, "default"}
                ]
                source_keys = [
                    key
                    for key, snapshot in self._source_snapshots.items()
                    if snapshot.authority.household_id in {household_id, "default"}
                ]
            for key in keys:
                self._snapshots.pop(key, None)
                self._state_versions.pop(key, None)
                self._startup_reconfirmation.pop(key, None)
            for source_key in source_keys:
                self._source_snapshots.pop(source_key, None)
            if keys or source_keys:
                self._revision += 1
            self._source_cursors.clear()
            self._source_ordering_policies.clear()
            self._resync_required.clear()
            self._inventory_fingerprint = None
            return len(keys)

    async def delete_capability(self, device_id: str, capability: str) -> bool:
        """Remove state no longer advertised by the authoritative inventory."""

        async with self._mutation_lock:
            key = (device_id, capability)
            existed = key in self._snapshots or any(
                source_key[:2] == key for source_key in self._source_snapshots
            )
            if not existed:
                return False
            candidate = self._candidate()
            candidate.snapshots.pop(key, None)
            candidate.state_versions.pop(key, None)
            candidate.startup_reconfirmation.pop(key, None)
            for source_key in [
                source_key
                for source_key in candidate.source_snapshots
                if source_key[:2] == (device_id, capability)
            ]:
                del candidate.source_snapshots[source_key]
            await self._commit_delete_candidate(
                candidate,
                device_id=device_id,
                capability=capability,
            )
            return True

    async def prune_capabilities(self, device_id: str, capabilities: set[str]) -> list[str]:
        """Delete cached capabilities removed from a live source mapping."""

        removed = [
            capability
            for current_device, capability in self._snapshots
            if current_device == device_id and capability not in capabilities
        ]
        for capability in removed:
            await self.delete_capability(device_id, capability)
        return removed

    def peek(self, device_id: str, capability: str) -> StateSnapshot | None:
        """Return the cached snapshot without performing I/O or refreshing it."""

        return self._snapshots.get((device_id, capability))

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None:
        return self._snapshots.get((device_id, capability))

    async def all(self) -> list[StateSnapshot]:
        return list(self._snapshots.values())

    def effective_status(self, snapshot: StateSnapshot, now: datetime | None = None) -> StateStatus:
        """Return the server-owned status at the instant of the query.

        ``observed_at`` remains source provenance.  ``received_at`` is the
        latest runtime confirmation of that value and is therefore the clock
        used for JIT freshness.  This lets an active Home Assistant read
        confirm an unchanged value without rewriting its source observation
        time, while cached adapters remain stale when their last receipt ages.
        """

        if snapshot.status is not StateStatus.CURRENT:
            return snapshot.status
        current_time = now or self.clock.now()
        if snapshot.observed_at > current_time or snapshot.received_at > current_time:
            return StateStatus.INVALID
        if current_time - snapshot.received_at > self.stale_after:
            return StateStatus.STALE
        return StateStatus.CURRENT

    def effective_snapshot(
        self, snapshot: StateSnapshot, now: datetime | None = None
    ) -> StateSnapshot:
        """Project JIT freshness without mutating the persisted snapshot."""

        status = self.effective_status(snapshot, now)
        if status is StateStatus.INVALID and snapshot.status is StateStatus.CURRENT:
            return snapshot.model_copy(update={"status": status, "value": None})
        if status is snapshot.status:
            return snapshot
        return snapshot.model_copy(update={"status": status})

    def freshness_report(
        self,
        *,
        optional_sources: frozenset[tuple[str, str]] = frozenset(),
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return the canonical freshness view used by health and readiness."""

        current_time = now or self.clock.now()
        projected = [
            self.effective_snapshot(item, current_time) for item in self._snapshots.values()
        ]
        required = [
            item
            for item in projected
            if (item.source_ref.adapter_id, item.source_ref.external_id) not in optional_sources
        ]
        optional = [
            item
            for item in projected
            if (item.source_ref.adapter_id, item.source_ref.external_id) in optional_sources
        ]
        statuses = {item.status for item in required}
        if not required:
            status = "unknown"
        elif StateStatus.INVALID in statuses or StateStatus.UNAVAILABLE in statuses:
            status = "degraded"
        elif StateStatus.STALE in statuses:
            status = "stale"
        else:
            status = "current"
        reason_codes: list[str] = []
        if StateStatus.INVALID in statuses:
            reason_codes.append("state_invalid")
        if StateStatus.UNAVAILABLE in statuses:
            reason_codes.append("state_unavailable")
        if StateStatus.STALE in statuses:
            reason_codes.append("state_stale")
        optional_statuses = {item.status for item in optional}
        if StateStatus.INVALID in optional_statuses:
            reason_codes.append("optional_state_invalid")
        if StateStatus.UNAVAILABLE in optional_statuses:
            reason_codes.append("optional_state_unavailable")
        if StateStatus.STALE in optional_statuses:
            reason_codes.append("optional_state_stale")
        ages = [max(0.0, (current_time - item.received_at).total_seconds()) for item in required]
        return {
            "status": status,
            "max_age_seconds": max(ages, default=None),
            "stale_after_seconds": self.stale_after.total_seconds(),
            "required_snapshot_count": len(required),
            "optional_snapshot_count": len(optional),
            "reason_codes": reason_codes,
        }

    def max_state_age_seconds(self, now: datetime | None = None) -> float | None:
        """Return the oldest cached observation age for health reporting."""

        if not self._snapshots:
            return None
        current_time = now or self.clock.now()
        return max(
            0.0,
            max(
                (current_time - snapshot.received_at).total_seconds()
                for snapshot in self._snapshots.values()
            ),
        )

    async def mark_stale(self, now: datetime | None = None) -> list[StateSnapshot]:
        async with self._mutation_lock:
            current_time = now or self.clock.now()
            candidate = self._candidate()
            stale: list[StateSnapshot] = []
            for key, snapshot in list(candidate.snapshots.items()):
                if (
                    snapshot.status is StateStatus.CURRENT
                    and self.effective_status(snapshot, current_time) is StateStatus.STALE
                ):
                    updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                    candidate.snapshots[key] = updated
                    candidate.version_counter += 1
                    candidate.state_versions[key] = candidate.version_counter
                    for source_key, source_snapshot in list(candidate.source_snapshots.items()):
                        if (
                            source_key[:2] == key
                            and source_snapshot.source_ref == snapshot.source_ref
                        ):
                            candidate.source_snapshots[source_key] = updated
                    stale.append(updated)
            await self._commit_candidate(candidate, stale)
            return stale

    async def mark_source_unavailable(
        self, adapter_id: str, external_id: str | None = None
    ) -> list[StateSnapshot]:
        """Degrade observations owned by a source or one source entity.

        Provider availability events can describe either an entire transport
        (for example, a KNX tunnel) or one entity (for example, a Zigbee
        device).  Keeping the optional entity boundary here prevents a
        single device outage from degrading every healthy observation from
        the same adapter.
        """

        async with self._mutation_lock:
            changed: list[StateSnapshot] = []
            candidate = self._candidate()
            affected: set[tuple[str, str]] = set()
            for source_key, snapshot in list(candidate.source_snapshots.items()):
                if (
                    source_key[2] != adapter_id
                    or (external_id is not None and source_key[3] != external_id)
                    or snapshot.status is StateStatus.UNAVAILABLE
                ):
                    continue
                candidate.source_snapshots[source_key] = snapshot.model_copy(
                    update={"status": StateStatus.UNAVAILABLE, "value": None}
                )
                affected.add(source_key[:2])
            for key in affected:
                resolved = self._resolve_sources(*key, candidate.source_snapshots)
                candidate.snapshots[key] = resolved
                candidate.version_counter += 1
                candidate.state_versions[key] = candidate.version_counter
                changed.append(resolved)
            await self._commit_candidate(candidate, changed)
            return changed

    async def mark_all_stale(self) -> list[StateSnapshot]:
        """Mark every current cached value stale after source loss."""

        async with self._mutation_lock:
            candidate = self._candidate()
            stale: list[StateSnapshot] = []
            for key, snapshot in list(candidate.snapshots.items()):
                if snapshot.status is StateStatus.CURRENT:
                    updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                    candidate.snapshots[key] = updated
                    for source_key, source_snapshot in list(candidate.source_snapshots.items()):
                        if source_key[:2] == key and source_snapshot.status is StateStatus.CURRENT:
                            candidate.source_snapshots[source_key] = source_snapshot.model_copy(
                                update={"status": StateStatus.STALE}
                            )
                    candidate.version_counter += 1
                    candidate.state_versions[key] = candidate.version_counter
                    stale.append(updated)
            await self._commit_candidate(candidate, stale)
            return stale

    async def persist_metadata(self) -> None:
        """Flush revision-only changes such as inventory fingerprints."""

        async with self._mutation_lock:
            if self._persistence is not None:
                try:
                    interrupted = await self._await_durable_operation(
                        self._persistence.persist((), self.export_metadata())
                    )
                except BaseException:
                    self.rollback_metadata_mutation()
                    raise
                self.confirm_metadata_persisted()
                self._durability_status = "committed"
                if interrupted:
                    raise asyncio.CancelledError

    async def _persist(
        self,
        snapshots: Sequence[StateSnapshot],
        metadata: StateStoreMetadata | None = None,
    ) -> None:
        if self._persistence is not None:
            await self._persistence.persist(snapshots, metadata or self.export_metadata())
