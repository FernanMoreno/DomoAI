"""Read-only comparison of a candidate adapter against a production adapter."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from domoai.domain.models import StrictModel
from domoai.runtime.ports import AdapterPort

_UNAVAILABLE_ERRORS = (ConnectionError, OSError, TimeoutError)

_Classification = Literal["only_production", "only_candidate", "matches", "disagrees"]


class EntityComparison(StrictModel):
    entity_id: str = Field(min_length=1)
    classification: _Classification


class ShadowComparisonResult(StrictModel):
    production_unavailable: bool = False
    candidate_unavailable: bool = False
    entities: list[EntityComparison] = Field(default_factory=list)


def _entities_by_id(entities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(entity["entity_id"]): entity for entity in entities}


class ShadowComparator:
    """Compares two adapters' observations, never issuing a command to either.

    Entities are correlated by their raw ``entity_id`` as reported by each
    adapter's own ``discover()`` snapshot, not via ``DeviceRegistry.apply_snapshot``
    fusion: that fusion only merges contributions from different adapter_ids
    when the source entity carries an explicit ``canonical_id`` (see
    ``SourceIdentity.identity_key``, which otherwise namespaces by adapter_id),
    so two independently-observed adapters describing the same physical
    device would almost never fuse automatically. The raw ``entity_id`` is
    the identifier the real underlying system (e.g. Home Assistant) itself
    assigns, so it is the correlation key both sides are actually expected
    to agree on when observing the same real system.
    """

    async def compare(
        self, production: AdapterPort, candidate: AdapterPort
    ) -> ShadowComparisonResult:
        try:
            production_snapshot = await production.discover()
        except _UNAVAILABLE_ERRORS:
            return ShadowComparisonResult(production_unavailable=True)

        try:
            candidate_snapshot = await candidate.discover()
        except _UNAVAILABLE_ERRORS:
            return ShadowComparisonResult(candidate_unavailable=True)

        production_entities = _entities_by_id(production_snapshot.source_entities)
        candidate_entities = _entities_by_id(candidate_snapshot.source_entities)

        entities: list[EntityComparison] = []
        for entity_id in sorted(set(production_entities) | set(candidate_entities)):
            in_production = entity_id in production_entities
            in_candidate = entity_id in candidate_entities
            classification: _Classification
            if in_production and not in_candidate:
                classification = "only_production"
            elif in_candidate and not in_production:
                classification = "only_candidate"
            elif (
                production_entities[entity_id].get("semantic_type")
                != candidate_entities[entity_id].get("semantic_type")
            ):
                classification = "disagrees"
            else:
                classification = "matches"
            entities.append(EntityComparison(entity_id=entity_id, classification=classification))

        return ShadowComparisonResult(entities=entities)
